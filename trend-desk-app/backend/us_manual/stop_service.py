"""H5 双源自动止损建议、人工复核与不可变仓位预览。"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import Session

from backend import config
from backend.db import (
    UsSizingPreviewSnapshot,
    UsStopReview,
    UsStopSuggestion,
    UsWindDataCache,
    UsWindRequestAudit,
)
from backend.us_manual import repository
from backend.us_manual.bitget_public import fetch_public_candles, fetch_public_quote
from backend.us_manual.contracts import (
    US_MANUAL_RULES_VERSION,
    US_MANUAL_SCOPE,
    US_STOP_ALGORITHM_VERSION,
    US_STOP_DEVIATION_THRESHOLD,
    UsManualError,
    canonical_json,
    decimal_text,
    parse_decimal,
    serialize,
    sha256,
)
from backend.us_manual.rules import sizing_preview
from backend.us_manual.stop_rules import (
    bitget_regular_session_anchor,
    buffered_stop,
    custom_review_stop,
    dual_source_decision,
    select_wind_anchor,
    validate_bitget_daily,
)
from backend.us_manual.wind_mcp import WindMcpClient


NY = ZoneInfo("America/New_York")
STOP_READY_STATUSES = {"auto_ready", "manual_resolved"}


def stop_contract_hash() -> str:
    return sha256({
        "rules_version": US_MANUAL_RULES_VERSION,
        "algorithm_version": US_STOP_ALGORITHM_VERSION,
        "wind": {
            "lookback_calendar_days": 120,
            "effective_trading_days": 60,
            "adjustment": "forward",
            "stock_tool": "stock_data.get_stock_kline",
            "etf_tool": "fund_data.get_fund_kline",
            "identity": {
                "stock_tool": "stock_data.get_stock_basicinfo",
                "etf_tool": "fund_data.get_fund_info",
                "input": "naked_ticker_via_wind_ner",
                "unique_standard_code_required": True,
                "suffix_guessing": False,
                "us_etf_kline_locator": "exact_name_from_unique_identity_row",
            },
            "ep3_k": 2,
        },
        "bitget": {
            "public_only": True,
            "category": "SPOT",
            "type": "market",
            "intervals": ["1D", "1H"],
            "session_timezone": "America/New_York",
            "regular_session": ["09:30", "16:00"],
        },
        "deviation_threshold": decimal_text(US_STOP_DEVIATION_THRESHOLD),
        "buffer": "floor_to_price_precision_then_minus_one_tick",
    })


def _mode(value: str | None = None) -> str:
    result = (value or config.US_MANUAL_STOP_MODE).strip().lower()
    if result not in {"off", "shadow", "active"}:
        raise UsManualError("stop_mode_invalid", "止损模式必须为 off、shadow 或 active", 500)
    return result


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_quote_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsManualError("quote_contract_error", "Bitget 公开报价时间无效") from exc
    return _aware_utc(parsed)


def _archive_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(canonical_json(serialize(payload)) + "\n", encoding="utf-8")
    temporary.replace(path)


def _candidate_ready(session: Session, candidate_id: int, *, allow_collecting: bool = False):
    candidate = repository.get_candidate(session, candidate_id)
    run = repository.get_run(session, candidate.run_id)
    latest = repository.latest_run(session, scope=US_MANUAL_SCOPE)
    if latest is None or latest.run_id != run.run_id:
        raise UsManualError(
            "historical_candidate_read_only",
            "该候选不属于当前活动 H5 运行；历史 H1–H4 候选只读，请从当前清单重新选择",
            409,
        )
    if run.rules_version != US_MANUAL_RULES_VERSION:
        raise UsManualError("historical_candidate_read_only", "历史 H1–H4 候选不能刷新 H5 止损", 409)
    allowed_statuses = {"ready", "ready_degraded"}
    if allow_collecting:
        allowed_statuses.add("collecting")
    if run.status not in allowed_statuses:
        raise UsManualError("run_not_stop_ready", "当前 H5 运行尚未完成筛选", 409)
    if (candidate.asset_type not in {"stock", "etf"} or not candidate.gate_passed
            or candidate.screen_status != "ready" or not candidate.venue_instrument):
        raise UsManualError("candidate_not_plan_ready", "候选尚未通过 H5 选股纪律", 422)
    return candidate, run


class StopSuggestionService:
    def __init__(
        self,
        *,
        data_root: Path | None = None,
        mode: str | None = None,
        wind_client_factory: Callable[[], WindMcpClient] = WindMcpClient,
        quote_fetcher: Callable[[str], dict[str, Any]] = fetch_public_quote,
        candles_fetcher: Callable[..., list[dict[str, Any]]] = fetch_public_candles,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.data_root = data_root or config.DATA
        self.mode = _mode(mode)
        self.wind_client_factory = wind_client_factory
        self.quote_fetcher = quote_fetcher
        self.candles_fetcher = candles_fetcher
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return _aware_utc(self.now_factory())

    @property
    def root(self) -> Path:
        return self.data_root / "research" / "us_manual_h5" / "stop_evidence"

    def _wind_bundle(self, session: Session, *, candidate: Any, signal_date: date) -> dict[str, Any]:
        begin = signal_date - timedelta(days=120)
        contract = stop_contract_hash()
        cache_key = sha256({
            "asset_type": candidate.asset_type,
            "ticker_symbol": candidate.ticker_symbol,
            "signal_date": signal_date.isoformat(),
            "begin_date": begin.isoformat(),
            "end_date": signal_date.isoformat(),
            "contract_hash": contract,
        })
        cached = repository.get_wind_cache(session, cache_key)
        if cached is not None:
            repository.save_wind_audit(session, UsWindRequestAudit(
                candidate_id=candidate.candidate_id,
                cache_key=cache_key,
                server_type="cache",
                tool_name="identity_and_daily_bundle",
                request_sha256=cache_key,
                cache_hit=True,
                status="success",
                response_sha256=cached.response_sha256,
            ))
            return dict(cached.payload_json)

        client = self.wind_client_factory()
        try:
            identity = client.resolve_symbol(
                asset_type=candidate.asset_type,
                ticker_symbol=candidate.ticker_symbol,
                ticker_name=candidate.ticker_name,
            )
            repository.save_wind_audit(session, UsWindRequestAudit(
                candidate_id=candidate.candidate_id,
                cache_key=cache_key,
                server_type=identity["server_type"],
                tool_name=identity["tool_name"],
                request_sha256=identity["question_hash"],
                cache_hit=False,
                status="success",
                response_sha256=identity["response_sha256"],
            ))
        except UsManualError as exc:
            repository.save_wind_audit(session, UsWindRequestAudit(
                candidate_id=candidate.candidate_id,
                cache_key=cache_key,
                server_type="stock_data" if candidate.asset_type == "stock" else "fund_data",
                tool_name="get_stock_basicinfo" if candidate.asset_type == "stock" else "get_fund_info",
                request_sha256=sha256({"ticker": candidate.ticker_symbol, "name": candidate.ticker_name}),
                cache_hit=False,
                status="error",
                error_code=exc.code,
                error_message=exc.message,
            ))
            raise
        try:
            daily = client.daily_bars(
                asset_type=candidate.asset_type,
                wind_symbol=identity["kline_locator"],
                begin_date=begin,
                end_date=signal_date,
            )
            repository.save_wind_audit(session, UsWindRequestAudit(
                candidate_id=candidate.candidate_id,
                cache_key=cache_key,
                server_type=daily["server_type"],
                tool_name=daily["tool_name"],
                request_sha256=sha256({
                    "windcode": daily["query_symbol"], "begin_date": begin,
                    "end_date": signal_date, "period": "10", "aftime": "0", "issusp": "0",
                }),
                cache_hit=False,
                status="success",
                response_sha256=daily["response_sha256"],
            ))
        except UsManualError as exc:
            repository.save_wind_audit(session, UsWindRequestAudit(
                candidate_id=candidate.candidate_id,
                cache_key=cache_key,
                server_type="stock_data" if candidate.asset_type == "stock" else "fund_data",
                tool_name="get_stock_kline" if candidate.asset_type == "stock" else "get_fund_kline",
                request_sha256=sha256({
                    "windcode": identity["kline_locator"], "begin": begin, "end": signal_date,
                }),
                cache_hit=False,
                status="error",
                error_code=exc.code,
                error_message=exc.message,
            ))
            raise
        bundle = {
            "wind_symbol": identity["wind_symbol"],
            "kline_locator": identity["kline_locator"],
            "kline_locator_source": identity["kline_locator_source"],
            "identity": identity["identity"],
            "identity_raw_response": identity["raw_response"],
            "identity_response_sha256": identity["response_sha256"],
            "daily_bars": daily["bars"],
            "daily_raw_response": daily["raw_response"],
            "daily_response_sha256": daily["response_sha256"],
            "adjustment": daily["adjustment"],
            "begin_date": begin,
            "end_date": signal_date,
        }
        archive_path = self.root / signal_date.isoformat() / "wind-cache" / f"{cache_key}.json"
        _archive_json(archive_path, bundle)
        cache = UsWindDataCache(
            cache_key=cache_key,
            asset_type=candidate.asset_type,
            ticker_symbol=candidate.ticker_symbol,
            signal_date=signal_date.isoformat(),
            begin_date=begin.isoformat(),
            end_date=signal_date.isoformat(),
            contract_hash=contract,
            wind_symbol=identity["wind_symbol"],
            payload_json=serialize(bundle),
            response_sha256=sha256({
                "identity": identity["response_sha256"], "daily": daily["response_sha256"],
            }),
            archive_path=str(archive_path),
        )
        saved = repository.save_wind_cache(session, cache)
        return dict(saved.payload_json)

    def _quote(self, session: Session, candidate: Any) -> dict[str, Any]:
        quote = self.quote_fetcher(str(candidate.venue_instrument))
        price = parse_decimal(quote.get("reference_price"), field="Bitget reference price", positive=True)
        quoted_at = _parse_quote_time(quote.get("quoted_at"))
        age = self._now() - quoted_at
        if age < timedelta(minutes=-2) or age > timedelta(
            minutes=max(1, config.US_MANUAL_STOP_QUOTE_MAX_AGE_MINUTES)
        ):
            raise UsManualError(
                "bitget_quote_stale",
                "Bitget 公共报价已过期，不能用于双源止损与仓位计算",
                409,
                {"quoted_at": quoted_at.isoformat(), "age_seconds": int(age.total_seconds())},
            )
        candidate.reference_price_usdt = price
        candidate.reference_price_at = quoted_at.astimezone(timezone.utc).replace(tzinfo=None)
        candidate.reference_price_source = "bitget_public_quote"
        candidate.quote_status = "available"
        repository.save_candidates(session, [candidate])
        return {**quote, "price": price, "quoted_at_datetime": quoted_at}

    def _save_blocked(self, session: Session, *, candidate: Any, run: Any,
                      error: UsManualError, seed: dict[str, Any]) -> UsStopSuggestion:
        input_hash = sha256({**seed, "error_code": error.code, "contract_hash": stop_contract_hash()})
        existing = repository.stop_suggestion_by_input(
            session, candidate_id=candidate.candidate_id, input_hash=input_hash,
        )
        if existing is not None:
            return existing
        suggestion_id = f"us-stop-{run.as_of_date.replace('-', '')}-{uuid4().hex[:12]}"
        evidence = serialize({
            "seed": seed,
            "error": error.as_payload(),
            "single_source_fallback": False,
            "manual_execution_only": True,
        })
        archive_path = self.root / run.as_of_date / str(candidate.candidate_id) / f"{suggestion_id}.json"
        _archive_json(archive_path, {
            "schema_version": 1,
            "suggestion_id": suggestion_id,
            "contract_hash": stop_contract_hash(),
            "seed": seed,
            "error": error.as_payload(),
            "evidence": evidence,
        })
        suggestion = UsStopSuggestion(
            suggestion_id=suggestion_id,
            candidate_id=candidate.candidate_id,
            run_id=run.run_id,
            status="blocked",
            source_mode=self.mode,
            signal_date=run.as_of_date,
            asset_type=candidate.asset_type,
            algorithm_version=US_STOP_ALGORITHM_VERSION,
            algorithm_sha256=sha256({"version": US_STOP_ALGORITHM_VERSION}),
            contract_hash=stop_contract_hash(),
            input_hash=input_hash,
            bitget_symbol=str(candidate.venue_instrument),
            deviation_threshold=US_STOP_DEVIATION_THRESHOLD,
            bitget_quote_usdt=candidate.reference_price_usdt,
            bitget_quote_at=candidate.reference_price_at,
            archive_path=str(archive_path),
            evidence_json=evidence,
            error_code=error.code,
            error_message=error.message,
        )
        return repository.save_stop_suggestion(session, suggestion)

    def refresh(self, session: Session, *, candidate_id: int,
                allow_collecting: bool = False) -> dict[str, Any]:
        raise UsManualError(
            "h5_stop_suggestion_retired",
            "H5 Wind 双源止损已退役；历史证据只读，H6 不需要 WIND_API_KEY",
            410,
        )
        # Historical implementation intentionally retained below for reading old
        # evidence and migration archaeology; this public mutation path is closed.
        if allow_collecting:
            # The collector already owns the run lease that covers its paid and
            # stop-evidence stages; acquiring a second owner would deadlock it.
            return self._refresh_unleased(
                session, candidate_id=candidate_id, allow_collecting=True,
            )
        if self.mode == "off":
            return self._refresh_unleased(session, candidate_id=candidate_id)
        _, run = _candidate_ready(session, candidate_id)
        owner = f"stop-{uuid4().hex}"
        now = self._now().replace(tzinfo=None)
        expires = now + timedelta(minutes=max(1, config.US_MANUAL_LEASE_MINUTES))
        if not repository.acquire_run_lease(
            session, run_id=run.run_id, owner=owner, now=now, expires_at=expires,
        ):
            raise UsManualError(
                "stop_refresh_in_progress",
                "当前 H5 运行正在采集或刷新双源止损，请稍后重试",
                409,
            )
        try:
            return self._refresh_unleased(session, candidate_id=candidate_id)
        finally:
            try:
                repository.release_run_lease(session, run_id=run.run_id, owner=owner)
            except Exception:
                # A short expiry keeps a failed release recoverable. Do not hide
                # an already-computed immutable suggestion behind cleanup noise.
                session.rollback()

    def _refresh_unleased(self, session: Session, *, candidate_id: int,
                          allow_collecting: bool = False) -> dict[str, Any]:
        if self.mode == "off":
            raise UsManualError(
                "stop_feature_off",
                "H5 双源止损当前为 off；开发/测试默认关闭，生产应先配置 shadow",
                409,
            )
        candidate, run = _candidate_ready(session, candidate_id, allow_collecting=allow_collecting)
        seed: dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "run_id": run.run_id,
            "signal_date": run.as_of_date,
            "ticker_symbol": candidate.ticker_symbol,
            "asset_type": candidate.asset_type,
            "bitget_symbol": candidate.venue_instrument,
            "source_mode": self.mode,
        }
        try:
            quote = self._quote(session, candidate)
            seed["quote"] = {
                "price": decimal_text(quote["price"]),
                "quoted_at": quote["quoted_at_datetime"].isoformat(),
            }
            signal = date.fromisoformat(run.as_of_date)
            wind = self._wind_bundle(session, candidate=candidate, signal_date=signal)
            wind_anchor = select_wind_anchor(
                wind["daily_bars"], signal_date=signal, bitget_quote=quote["price"],
            )
            anchor_date = date.fromisoformat(str(wind_anchor["anchor_date"])[:10])

            daily_start = datetime.combine(signal - timedelta(days=35), time.min, tzinfo=NY).astimezone(timezone.utc)
            daily_end = datetime.combine(signal + timedelta(days=2), time.max, tzinfo=NY).astimezone(timezone.utc)
            daily_rows = self.candles_fetcher(
                str(candidate.venue_instrument), interval="1D",
                start_time=daily_start, end_time=daily_end,
            )
            daily_check = validate_bitget_daily(daily_rows)

            hourly_start = datetime.combine(anchor_date, time(8, 0), tzinfo=NY).astimezone(timezone.utc)
            hourly_end = datetime.combine(anchor_date, time(17, 0), tzinfo=NY).astimezone(timezone.utc)
            hourly_rows = self.candles_fetcher(
                str(candidate.venue_instrument), interval="1H",
                start_time=hourly_start, end_time=hourly_end,
            )
            hourly_anchor = bitget_regular_session_anchor(hourly_rows, anchor_date=anchor_date)
            decision = dual_source_decision(
                wind_anchor=wind_anchor,
                bitget_anchor_low=hourly_anchor["anchor_low"],
                bitget_quote=quote["price"],
                price_precision=(candidate.venue_metadata_json or {}).get("price_precision"),
                threshold=US_STOP_DEVIATION_THRESHOLD,
            )
            daily_hash = sha256(serialize(daily_rows))
            hourly_hash = sha256(serialize(hourly_rows))
            seed.update({
                "wind_response_sha256": wind["daily_response_sha256"],
                "bitget_daily_sha256": daily_hash,
                "bitget_hourly_sha256": hourly_hash,
                "contract_hash": stop_contract_hash(),
            })
            input_hash = sha256(seed)
            existing = repository.stop_suggestion_by_input(
                session, candidate_id=candidate.candidate_id, input_hash=input_hash,
            )
            if existing is not None:
                return self.payload(session, existing, full=True)
            suggestion_id = f"us-stop-{run.as_of_date.replace('-', '')}-{uuid4().hex[:12]}"
            evidence = {
                "wind": {
                    "symbol": wind["wind_symbol"],
                    "kline_locator": wind["kline_locator"],
                    "kline_locator_source": wind["kline_locator_source"],
                    "adjustment": wind["adjustment"],
                    "begin_date": wind["begin_date"],
                    "end_date": wind["end_date"],
                    "identity": wind["identity"],
                    "anchor": wind_anchor,
                    "response_sha256": wind["daily_response_sha256"],
                },
                "bitget": {
                    "symbol": candidate.venue_instrument,
                    "quote": seed["quote"],
                    "daily_validation": daily_check,
                    "hourly_anchor": hourly_anchor,
                    "daily_response_sha256": daily_hash,
                    "hourly_response_sha256": hourly_hash,
                    "public_only": True,
                },
                "decision": decision,
                "single_source_fallback": False,
                "manual_execution_only": True,
            }
            archive_path = self.root / run.as_of_date / str(candidate.candidate_id) / f"{suggestion_id}.json"
            archive_payload = {
                "schema_version": 1,
                "suggestion_id": suggestion_id,
                "contract_hash": stop_contract_hash(),
                "seed": seed,
                "wind_raw": {
                    "identity": wind["identity_raw_response"],
                    "daily": wind["daily_raw_response"],
                },
                "bitget_raw": {"daily": daily_rows, "hourly": hourly_rows, "quote": quote},
                "evidence": evidence,
            }
            _archive_json(archive_path, archive_payload)
            suggestion = UsStopSuggestion(
                suggestion_id=suggestion_id,
                candidate_id=candidate.candidate_id,
                run_id=run.run_id,
                status=decision["status"],
                source_mode=self.mode,
                signal_date=run.as_of_date,
                asset_type=candidate.asset_type,
                algorithm_version=US_STOP_ALGORITHM_VERSION,
                algorithm_sha256=sha256({"version": US_STOP_ALGORITHM_VERSION}),
                contract_hash=stop_contract_hash(),
                input_hash=input_hash,
                wind_symbol=wind["wind_symbol"],
                bitget_symbol=str(candidate.venue_instrument),
                anchor_type=wind_anchor["anchor_type"],
                anchor_date=anchor_date.isoformat(),
                wind_anchor_low_usd=wind_anchor["anchor_low"],
                wind_signal_close_usd=wind_anchor["signal_close"],
                wind_ratio=wind_anchor["wind_ratio"],
                wind_mapped_stop_usdt=decision["wind_mapped_stop"],
                bitget_anchor_low_usdt=decision["bitget_anchor_low"],
                bitget_quote_usdt=decision["bitget_quote"],
                bitget_quote_at=candidate.reference_price_at,
                deviation_ratio=decision["deviation_ratio"],
                deviation_threshold=decision["deviation_threshold"],
                price_tick_usdt=decision["price_tick"],
                suggested_stop_usdt=decision["suggested_stop"],
                wind_response_sha256=wind["daily_response_sha256"],
                bitget_daily_sha256=daily_hash,
                bitget_hourly_sha256=hourly_hash,
                archive_path=str(archive_path),
                evidence_json=serialize(evidence),
            )
            saved = repository.save_stop_suggestion(session, suggestion)
            return self.payload(session, saved, full=True)
        except UsManualError as exc:
            blocked = self._save_blocked(
                session, candidate=candidate, run=run, error=exc, seed=seed,
            )
            return self.payload(session, blocked, full=True)

    def payload(self, session: Session, suggestion: UsStopSuggestion, *, full: bool = False) -> dict[str, Any]:
        review = repository.stop_review_for_suggestion(session, suggestion.suggestion_id)
        effective_status = "manual_resolved" if review is not None else suggestion.status
        final_stop = (
            review.final_stop_usdt if review is not None
            else suggestion.suggested_stop_usdt if suggestion.status == "auto_ready"
            else None
        )
        final_source = (
            review.final_source if review is not None
            else "auto_wind_structure_bitget_execution" if suggestion.status == "auto_ready"
            else None
        )
        payload = repository.model_payload(suggestion)
        if suggestion.bitget_quote_at is not None:
            payload["bitget_quote_at"] = _aware_utc(suggestion.bitget_quote_at).isoformat()
        payload.update({
            "effective_status": effective_status,
            "final_stop_usdt": decimal_text(final_stop),
            "final_source": final_source,
            "review": repository.model_payload(review) if review is not None else None,
            "planning_enabled": (
                self.mode == "active"
                and suggestion.source_mode == "active"
                and effective_status in STOP_READY_STATUSES
            ),
        })
        if not full:
            payload.pop("evidence_json", None)
        return payload

    def candidate_payload(self, session: Session, candidate: Any) -> dict[str, Any]:
        payload = repository.model_payload(candidate)
        suggestion = repository.latest_stop_suggestion(session, candidate.candidate_id)
        payload["stop_suggestion"] = self.payload(session, suggestion) if suggestion is not None else None
        payload["stop_status"] = (
            payload["stop_suggestion"]["effective_status"] if suggestion is not None else "pending"
        )
        return payload

    def review(self, session: Session, *, suggestion_id: str,
               payload: dict[str, Any]) -> dict[str, Any]:
        raise UsManualError(
            "h5_stop_review_retired", "H5 止损复核已退役，历史定稿只读", 410,
        )
        if self.mode == "off":
            raise UsManualError("stop_feature_off", "H5 双源止损当前为 off", 409)
        if self.mode == "shadow":
            raise UsManualError(
                "stop_shadow_read_only",
                "H5 shadow 阶段只采集与展示证据，不允许人工定稿",
                409,
            )
        allowed = {"resolution", "custom_price_usdt", "reason", "idempotency_key"}
        unexpected = set(payload) - allowed
        if unexpected:
            raise UsManualError(
                "unexpected_stop_review_fields", "人工复核请求包含不允许字段", 422,
                {"unexpected_fields": sorted(unexpected)},
            )
        suggestion = repository.get_stop_suggestion(session, suggestion_id)
        latest = repository.latest_stop_suggestion(session, suggestion.candidate_id)
        if latest is None or latest.suggestion_id != suggestion_id:
            raise UsManualError("stop_suggestion_stale", "该止损建议已不是候选的最新版本", 409)
        if suggestion.status != "review_required":
            raise UsManualError("stop_review_not_allowed", "只有双源有效但偏差超限的建议允许人工定稿", 409)
        key = str(payload.get("idempotency_key") or "").strip()
        if not key or len(key) > 160:
            raise UsManualError("idempotency_key_required", "人工复核必须提供不超过 160 字的幂等键", 422)
        replay = repository.stop_review_by_idempotency(session, key)
        if replay is not None:
            if replay.suggestion_id != suggestion_id:
                raise UsManualError("idempotency_conflict", "该幂等键已用于另一条止损建议", 409)
            return self.payload(session, suggestion, full=True)
        if repository.stop_review_for_suggestion(session, suggestion_id) is not None:
            raise UsManualError("stop_review_already_resolved", "该建议已经人工定稿，不能覆盖", 409)
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < 4 or len(reason) > 500:
            raise UsManualError("stop_review_reason_required", "人工复核理由须为 4–500 字", 422)
        precision = (repository.get_candidate(session, suggestion.candidate_id).venue_metadata_json or {}).get(
            "price_precision"
        )
        resolution = str(payload.get("resolution") or "")
        if resolution == "wind_mapped":
            final = buffered_stop(suggestion.wind_mapped_stop_usdt, price_precision=precision)
            source = "manual_dual_source_review_wind_mapped"
            custom = None
        elif resolution == "bitget_anchor":
            final = buffered_stop(suggestion.bitget_anchor_low_usdt, price_precision=precision)
            source = "manual_dual_source_review_bitget_anchor"
            custom = None
        elif resolution == "custom":
            custom = custom_review_stop(payload.get("custom_price_usdt"), price_precision=precision)
            final = custom
            source = "manual_dual_source_review_custom"
        else:
            raise UsManualError(
                "stop_review_resolution_invalid",
                "复核选择必须为 wind_mapped、bitget_anchor 或 custom",
                422,
            )
        if suggestion.bitget_quote_usdt is None or final >= suggestion.bitget_quote_usdt:
            raise UsManualError("stop_not_below_quote", "人工定稿止损必须低于建议快照中的 Bitget 报价", 422)
        review = UsStopReview(
            review_id=f"us-stop-review-{uuid4().hex[:14]}",
            suggestion_id=suggestion_id,
            idempotency_key=key,
            resolution=resolution,
            custom_price_usdt=custom,
            final_stop_usdt=final,
            final_source=source,
            reason=reason,
            evidence_json={
                "wind_mapped_stop_usdt": decimal_text(suggestion.wind_mapped_stop_usdt),
                "bitget_anchor_low_usdt": decimal_text(suggestion.bitget_anchor_low_usdt),
                "bitget_quote_usdt": decimal_text(suggestion.bitget_quote_usdt),
                "deviation_ratio": decimal_text(suggestion.deviation_ratio),
                "manual_execution_only": True,
            },
        )
        repository.save_stop_review(session, review)
        return self.payload(session, suggestion, full=True)

    def sizing(self, session: Session, *, candidate_id: int,
               suggestion_id: str) -> dict[str, Any]:
        raise UsManualError(
            "h5_sizing_retired", "H5 单候选止损仓位已退役；H6 使用多候选推荐分配", 410,
        )
        if self.mode != "active":
            raise UsManualError(
                "stop_not_active", "H5 双源止损尚未切换为 active；shadow 仅收集证据", 409,
            )
        candidate, _ = _candidate_ready(session, candidate_id)
        suggestion = repository.get_stop_suggestion(session, suggestion_id)
        latest = repository.latest_stop_suggestion(session, candidate_id)
        if suggestion.candidate_id != candidate_id:
            raise UsManualError("stop_candidate_mismatch", "止损建议不属于该候选", 422)
        if latest is None or latest.suggestion_id != suggestion_id:
            raise UsManualError("stop_suggestion_stale", "报价或止损证据已刷新，请重新生成仓位预览", 409)
        if suggestion.source_mode != "active":
            raise UsManualError("stop_shadow_read_only", "shadow 阶段建议只读；切换 active 后需重新刷新", 409)
        review = repository.stop_review_for_suggestion(session, suggestion_id)
        if suggestion.status == "auto_ready":
            stop = suggestion.suggested_stop_usdt
            source = "auto_wind_structure_bitget_execution"
            review_id = None
        elif suggestion.status == "review_required" and review is not None:
            stop = review.final_stop_usdt
            source = review.final_source
            review_id = review.review_id
        elif suggestion.status == "review_required":
            raise UsManualError("stop_manual_review_required", "双源偏差超过 1.5%，请先完成人工复核", 409)
        else:
            raise UsManualError("stop_suggestion_blocked", suggestion.error_message or "双源止损证据不完整", 409)
        if stop is None:
            raise UsManualError("stop_suggestion_blocked", "止损建议缺少最终价格", 409)
        if (candidate.reference_price_usdt != suggestion.bitget_quote_usdt
                or candidate.reference_price_at != suggestion.bitget_quote_at):
            raise UsManualError("stop_suggestion_stale", "候选报价已变化，请刷新双源止损建议", 409)
        from backend.us_manual.ledger import account_state
        state = account_state(session)
        preview = sizing_preview(
            entry_price=suggestion.bitget_quote_usdt,
            stop_price=stop,
            quantity_precision=(candidate.venue_metadata_json or {}).get("quantity_precision"),
            min_trade_usdt=(candidate.venue_metadata_json or {}).get("min_trade_usdt"),
            open_position_count=state["position_count"],
            open_notional_usdt=state["open_cost_usdt"],
        )
        preview_id = f"us-size-{uuid4().hex[:16]}"
        snapshot_payload = {
            **preview,
            "candidate_id": candidate_id,
            "stop_suggestion_id": suggestion_id,
            "stop_review_id": review_id,
            "stop_source": source,
            "anchor_type": suggestion.anchor_type,
            "anchor_date": suggestion.anchor_date,
            "algorithm_version": suggestion.algorithm_version,
            "deviation_ratio": decimal_text(suggestion.deviation_ratio),
            "reference_price_at": (
                _aware_utc(suggestion.bitget_quote_at).isoformat()
                if suggestion.bitget_quote_at is not None else None
            ),
        }
        snapshot = UsSizingPreviewSnapshot(
            sizing_preview_id=preview_id,
            candidate_id=candidate_id,
            stop_suggestion_id=suggestion_id,
            stop_review_id=review_id,
            input_hash=sha256(snapshot_payload),
            entry_reference_price_usdt=suggestion.bitget_quote_usdt,
            stop_price_usdt=stop,
            stop_source=source,
            snapshot_json=serialize(snapshot_payload),
        )
        repository.save_sizing_preview(session, snapshot)
        return {**serialize(snapshot_payload), "sizing_preview_id": preview_id}
