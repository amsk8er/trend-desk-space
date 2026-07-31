"""美股手工执行台的归档、受控扫描与候选补充编排。"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlmodel import Session

from backend import config
from backend.db import UsCandidateSnapshot, UsDailyRun, UsUniverseArchive
from backend.trend_animals.billing import billing_map, estimate_snapshot_cost
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError
from backend.trend_animals.service import ledger_delta, ledger_mark
from backend.trend_animals.us_micro_live import (
    bitget_us_etf_observation_universe,
    bitget_us_stock_universe,
    build_signal_scan_plan,
    classify_signal_rows,
)
from backend.us_manual import repository
from backend.us_manual.account_ocr import batch_payload as account_ocr_payload
from backend.us_manual.allocation import capacity_snapshot
from backend.us_manual.bitget_public import fetch_public_quote
from backend.us_manual.collection import UsH6Collector
from backend.us_manual.contracts import (
    LEGACY_BASE_SIGNAL_FIELDS as BASE_SIGNAL_FIELDS,
    MANUAL_ONLY_NOTICE,
    US_MANUAL_H2_SCOPE,
    US_MANUAL_H3_SCOPE,
    US_MANUAL_H4_SCOPE,
    US_MANUAL_H5_SCOPE,
    US_MANUAL_RULES_VERSION,
    US_MANUAL_LEGACY_RULES_VERSION,
    US_MANUAL_LEGACY_SCOPE,
    US_MANUAL_SCOPE,
    UsManualError,
    as_int,
    canonical_json,
    decimal_text,
    parse_decimal,
    serialize,
    sha256,
    utc_now,
    utc_now_text,
)
from backend.us_manual.enrichment import (
    apply_enrichment,
    enrichment_field_hash,
    resolve_enrichment_fields,
    validate_snapshot_rows,
)
from backend.us_manual.etf_benchmarks import EtfBenchmarkService
from backend.us_manual.ledger import account_state
from backend.us_manual.rules import policy_payload, rank_ready_candidates
from backend.us_manual.risk_anchor import RiskAnchorService, next_us_session
from backend.us_manual.stop_service import StopSuggestionService


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsManualError("archive_missing", f"缺少{label}归档", 404, {"path": str(path)}) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UsManualError("archive_corrupt", f"{label}归档无法读取", 409, {"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise UsManualError("archive_corrupt", f"{label}归档不是 JSON 对象", 409, {"path": str(path)})
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    temp.replace(path)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _us_status(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise UsManualError("api_contract_error", "趋势动物更新状态不是数组")
    matches = [row for row in rows if isinstance(row, dict) and row.get("asset") == "美股"]
    if len(matches) != 1 or not matches[0].get("asOfDate"):
        raise UsManualError("api_contract_error", "趋势动物更新状态中美股根节点不唯一或缺少数据日")
    return matches[0]


def _changed_fields(rows: Any) -> list[dict[str, Any]]:
    return [
        {key: row.get(key) for key in ("date", "change")}
        for row in (rows if isinstance(rows, list) else [])[:3]
        if isinstance(row, dict)
    ]


class UsManualService:
    """所有副作用都经由这里发生，CLI 和 HTTP 共用相同缓存/费用闸门。"""

    def __init__(self, *, data_root: Path | None = None,
                 client_factory: Callable[[], TrendAnimalsClient] = TrendAnimalsClient,
                 instruments_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
                 quote_fetcher: Callable[[str], dict[str, Any]] | None = None,
                 risk_anchor_service_factory: Callable[[], Any] | None = None,
                 etf_service_factory: Callable[[], Any] | None = None,
                 stop_service_factory: Callable[[], StopSuggestionService] | None = None,
                 now_factory: Callable[[], Any] | None = None):
        self.data_root = data_root or config.DATA
        self.client_factory = client_factory
        self.instruments_fetcher = instruments_fetcher
        self.quote_fetcher = quote_fetcher
        self.risk_anchor_service_factory = risk_anchor_service_factory
        self.etf_service_factory = etf_service_factory
        self.stop_service_factory = stop_service_factory
        self.now_factory = now_factory

    def _h6(self) -> UsH6Collector:
        kwargs: dict[str, Any] = {
            "data_root": self.data_root,
            "client_factory": self.client_factory,
        }
        if self.instruments_fetcher is not None:
            kwargs["instruments_fetcher"] = self.instruments_fetcher
        if self.quote_fetcher is not None:
            kwargs["quote_fetcher"] = self.quote_fetcher
        if self.risk_anchor_service_factory is not None:
            kwargs["risk_anchor_service_factory"] = self.risk_anchor_service_factory
        elif self.stop_service_factory is not None:
            # Compatibility for older injected test factories.
            kwargs["stop_service_factory"] = self.stop_service_factory
        if self.etf_service_factory is not None:
            kwargs["etf_service_factory"] = self.etf_service_factory
        if self.now_factory is not None:
            kwargs["now_factory"] = self.now_factory
        return UsH6Collector(**kwargs)

    # Compatibility for scheduler/tests that still use the historical helper name.
    def _h5(self) -> UsH6Collector:
        return self._h6()

    def _risk_anchor_service(self) -> Any:
        if self.risk_anchor_service_factory is not None:
            return self.risk_anchor_service_factory()
        kwargs: dict[str, Any] = {"data_root": self.data_root}
        if self.quote_fetcher is not None:
            kwargs["quote_fetcher"] = self.quote_fetcher
        if self.now_factory is not None:
            kwargs["now_factory"] = self.now_factory
        return RiskAnchorService(**kwargs)

    def _etf_benchmark_service(self) -> Any:
        if self.etf_service_factory is not None:
            return self.etf_service_factory()
        return EtfBenchmarkService(data_root=self.data_root)

    def _stop_service(self) -> StopSuggestionService:
        if self.stop_service_factory is not None:
            return self.stop_service_factory()
        kwargs: dict[str, Any] = {"data_root": self.data_root}
        if self.quote_fetcher is not None:
            kwargs["quote_fetcher"] = self.quote_fetcher
        if self.now_factory is not None:
            kwargs["now_factory"] = self.now_factory
        return StopSuggestionService(**kwargs)

    @property
    def _coverage_root(self) -> Path:
        return self.data_root / "research" / "trend_animals" / "us_coverage"

    @property
    def _venue_root(self) -> Path:
        return self.data_root / "research" / "venues" / "us_xstocks"

    @property
    def _scan_root(self) -> Path:
        return self.data_root / "research" / "trend_animals" / "us_micro_live"

    def capabilities(self) -> dict[str, Any]:
        result = {
            "enabled": bool(config.US_MANUAL_DESK_ENABLED),
            "manual_only": True,
            "automated_trading": False,
            "automated_collection": True,
            "scheduler_enabled": bool(config.US_MANUAL_SCHEDULER_ENABLED),
            "private_bitget_access": False,
            "bitget_public_market_only": True,
            "order_api_enabled": False,
            "etf_execution_enabled": True,
            "etf_mode": "trade_pool",
            "notice": MANUAL_ONLY_NOTICE,
            "rules_version": US_MANUAL_RULES_VERSION,
            "h6_mode": config.US_MANUAL_H6_MODE,
            "risk_anchor_source": "bitget_public_1d_1h_quote",
            "risk_anchor_is_exit_stop": False,
            "real_exit_source": "trend_animals_temperature_danger_boiling_champagne",
            "wind_required": False,
            "max_distinct_tickers": 20,
        }
        return result

    @staticmethod
    def _environment_payload(environment: Any | None) -> dict[str, Any] | None:
        if environment is None:
            return None
        payload = repository.model_payload(environment)
        raw = environment.raw_json if isinstance(environment.raw_json, dict) else {}
        labels = raw.get("tickerLabels")
        if isinstance(labels, str):
            labels = [value.strip() for value in re.split(r"[,，、|]", labels) if value.strip()]
        elif not isinstance(labels, list):
            labels = []
        payload.update({
            "market_strength_local": raw.get("trendStrengthLocalCurr"),
            "market_phase": raw.get("trendPhaseCurr"),
            "market_labels": labels,
            "days_since_trend_entry": as_int(raw.get("daysSinceTrendEntry")),
        })
        return payload

    def _find_archive_paths(self) -> tuple[Path, Path, Path | None]:
        seeds = sorted(self._coverage_root.glob("*/current_universe_seed.json"), reverse=True)
        if not seeds:
            raise UsManualError(
                "universe_archive_missing",
                "尚无趋势动物美股覆盖归档；先在数据审计区完成一次受控覆盖归档。",
                404,
            )
        for seed_path in seeds:
            seed = _read_json(seed_path, label="趋势动物覆盖")
            as_of_date = str(seed.get("as_of_date") or "")
            if not as_of_date:
                continue
            pools = sorted(self._venue_root.glob("*/public_xstock_pool.json"), reverse=True)
            for pool_path in pools:
                pool = _read_json(pool_path, label="Bitget 公开交集")
                if str(pool.get("trend_animals_as_of_date") or "") == as_of_date:
                    manifest_path = seed_path.parent / "manifest.json"
                    return seed_path, pool_path, manifest_path if manifest_path.exists() else None
        raise UsManualError(
            "venue_pool_missing",
            "存在趋势动物覆盖归档，但找不到同一数据日的 Bitget 公开严格交集。",
            409,
        )

    def archive_bundle(self) -> dict[str, Any]:
        seed_path, pool_path, manifest_path = self._find_archive_paths()
        seed = _read_json(seed_path, label="趋势动物覆盖")
        pool = _read_json(pool_path, label="Bitget 公开交集")
        if not isinstance(seed.get("instruments"), list) or not isinstance(pool.get("candidates"), list):
            raise UsManualError("archive_corrupt", "美股覆盖或 Bitget 交集缺少品种数组")
        as_of_date = str(seed.get("as_of_date") or "")
        if not as_of_date or pool.get("trend_animals_as_of_date") != as_of_date:
            raise UsManualError("archive_date_conflict", "覆盖归档与 Bitget 交集的数据日不一致")
        seed_hash = sha256(seed)
        pool_hash = sha256(pool)
        stock_rows = bitget_us_stock_universe(pool)
        etf_rows = bitget_us_etf_observation_universe(pool)
        manifest = _read_json(manifest_path, label="趋势动物覆盖") if manifest_path else None
        return {
            "seed_path": seed_path,
            "pool_path": pool_path,
            "manifest_path": manifest_path,
            "seed": seed,
            "pool": pool,
            "manifest": manifest,
            "as_of_date": as_of_date,
            "seed_sha256": seed_hash,
            "intersection_sha256": pool_hash,
            "stocks": stock_rows,
            "etfs": etf_rows,
        }

    def ensure_universe_archive(self, session: Session) -> tuple[UsUniverseArchive, dict[str, Any]]:
        bundle = self.archive_bundle()
        archive_id = f"us-universe-{bundle['as_of_date'].replace('-', '')}-{bundle['intersection_sha256'][:12]}"
        archive = UsUniverseArchive(
            archive_id=archive_id,
            trend_animals_as_of_date=bundle["as_of_date"],
            coverage_manifest_path=str(bundle["manifest_path"]) if bundle["manifest_path"] else None,
            venue_manifest_path=str(bundle["pool_path"].parent / "manifest.json"),
            seed_sha256=bundle["seed_sha256"],
            intersection_sha256=bundle["intersection_sha256"],
            stock_count=len(bundle["stocks"]),
            etf_observation_count=len(bundle["etfs"]),
            metadata_json={
                "seed_path": str(bundle["seed_path"]),
                "pool_path": str(bundle["pool_path"]),
                "seed_instrument_count": bundle["seed"].get("instrument_count"),
                "public_candidate_count": bundle["pool"].get("public_candidate_count"),
                "dual_venue_candidate_count": bundle["pool"].get("dual_venue_candidate_count"),
                "manifest_status": (bundle["manifest"] or {}).get("status"),
                "etf_execution_mode": "trade_pool",
            },
        )
        return repository.save_universe_archive(session, archive), bundle

    def _candidate_from_screen_row(self, *, run_id: str, row: dict[str, Any]) -> UsCandidateSnapshot:
        venue = row.get("venue") if isinstance(row.get("venue"), dict) else {}
        tm_id = as_int(row.get("tmId"))
        if tm_id is None:
            raise UsManualError("archive_corrupt", "基础扫描归档中存在无效 tmId")
        asset_type = "etf_observation" if row.get("root_asset") == "美国ETF" else "stock"
        reasons = [str(row.get("screen_reason") or "unknown")]
        return UsCandidateSnapshot(
            run_id=run_id,
            tm_id=tm_id,
            ticker_symbol=str(row.get("tickerSymbol") or "").upper(),
            ticker_name=row.get("tickerName"),
            asset_type=asset_type,
            venue_instrument=venue.get("venue_instrument"),
            venue_metadata_json=venue,
            temperature_prev=row.get("temperature_prev"),
            temperature_curr=row.get("temperature_curr"),
            right_side_calendar_days=as_int(row.get("days_since_trend_entry")),
            right_side_age_bucket=row.get("right_side_age_bucket"),
            warm_to_hot=bool(row.get("warm_to_hot")),
            screen_status=str(row.get("screen_status") or "observe"),
            primary_reason=reasons[0],
            all_reasons=reasons,
            raw_fields=row,
            raw_sha256=sha256(row),
        )

    def _funnel_from_screen(self, screen: dict[str, Any]) -> dict[str, Any]:
        candidates = screen.get("all_candidates") if isinstance(screen.get("all_candidates"), list) else []
        warm_to_hot = sum(1 for row in candidates if isinstance(row, dict) and row.get("warm_to_hot"))
        eligible = int(screen.get("screened_count") or 0)
        return {
            "strict_bitget_us_stocks": int(screen.get("universe_count") or len(candidates)),
            "warm_to_hot": warm_to_hot,
            "right_side_within_window": eligible,
            "ready_for_plan": 0,
            "screen_status_counts": screen.get("screen_status_counts") or {},
            "right_side_age_buckets": screen.get("warm_to_hot_right_side_age_buckets") or {},
        }

    def _cached_scan_path(self, as_of_date: str) -> Path | None:
        path = self._scan_root / as_of_date / "signal_scan_bitget_us_stocks.json"
        return path if path.exists() else None

    def import_cached_scan(self, session: Session, *, bundle: dict[str, Any],
                           archive: UsUniverseArchive) -> UsDailyRun | None:
        path = self._cached_scan_path(bundle["as_of_date"])
        if path is None:
            return None
        payload = _read_json(path, label="美股基础扫描")
        plan = payload.get("scan_plan") if isinstance(payload.get("scan_plan"), dict) else {}
        screen = payload.get("screen") if isinstance(payload.get("screen"), dict) else {}
        if plan.get("as_of_date") != bundle["as_of_date"] or screen.get("as_of_date") != bundle["as_of_date"]:
            raise UsManualError("archive_date_conflict", "缓存基础扫描与当前覆盖数据日不一致")
        fields = plan.get("requested_fields")
        if list(fields or []) != list(BASE_SIGNAL_FIELDS):
            raise UsManualError("archive_contract_mismatch", "缓存基础扫描字段集不是 H1 最小字段集")
        field_hash = sha256(list(BASE_SIGNAL_FIELDS))
        existing = repository.cached_run(
            session, as_of_date=bundle["as_of_date"], scope=US_MANUAL_LEGACY_SCOPE, base_fields_hash=field_hash,
        )
        if existing is not None:
            return existing
        run = UsDailyRun(
            run_id=f"us-run-{bundle['as_of_date'].replace('-', '')}-{field_hash[:10]}",
            as_of_date=bundle["as_of_date"],
            scope=US_MANUAL_LEGACY_SCOPE,
            status="importing_archive",
            rules_version=US_MANUAL_LEGACY_RULES_VERSION,
            universe_archive_id=archive.archive_id,
            base_fields=list(BASE_SIGNAL_FIELDS),
            base_fields_hash=field_hash,
            estimated_base_cost_cny=_decimal_or_none((payload.get("cost") or {}).get("estimated_cost_cny")),
            approved_base_budget_cny=_decimal_or_none((payload.get("cost") or {}).get("approved_budget_cny")),
            actual_base_cost_cny=_decimal_or_none((payload.get("cost") or {}).get("actual_cost_cny")),
            cache_hit=True,
            universe_count=int(screen.get("universe_count") or 0),
            returned_count=int(screen.get("returned_snapshot_count") or 0),
            funnel_json=self._funnel_from_screen(screen),
            source_dates_json={
                "signal_as_of_date": bundle["as_of_date"],
                "membership_as_of_dates": screen.get("membership_as_of_dates") or [bundle["as_of_date"]],
                "membership_archive_date": archive.trend_animals_as_of_date,
            },
            raw_archive_path=str(path),
            raw_sha256=sha256(payload),
        )
        saved = repository.save_run(session, run)
        if saved.run_id != run.run_id:
            return saved
        all_rows = screen.get("all_candidates")
        if not isinstance(all_rows, list):
            repository.update_run(session, saved, status="blocked", error_code="archive_corrupt",
                                  error_message="缓存基础扫描缺少候选数组")
            raise UsManualError("archive_corrupt", "缓存基础扫描缺少候选数组")
        repository.replace_candidates(
            session, run=saved,
            candidates=[self._candidate_from_screen_row(run_id=saved.run_id, row=row)
                        for row in all_rows if isinstance(row, dict)],
        )
        return repository.update_run(session, saved, status="ready_cached", completed_at=utc_now())

    def universe_latest(self, session: Session) -> dict[str, Any]:
        archive, bundle = self.ensure_universe_archive(session)
        return {
            "archive": repository.model_payload(archive),
            "trend_animals": {
                "membership_archive_date": bundle["as_of_date"],
                "seed_instrument_count": bundle["seed"].get("instrument_count"),
                "seed_sha256": bundle["seed_sha256"],
                "coverage_manifest_path": str(bundle["manifest_path"]) if bundle["manifest_path"] else None,
            },
            "bitget_public_intersection": {
                "stock_count": len(bundle["stocks"]),
                "etf_observation_count": len(bundle["etfs"]),
                "intersection_sha256": bundle["intersection_sha256"],
                "pool_path": str(bundle["pool_path"]),
                "retrieval_date": bundle["pool_path"].parent.name,
            },
            "etf_observation": {
                "enabled": True,
                "execution_enabled": True,
                "count": len(bundle["etfs"]),
                "notice": (
                    "美国 ETF 可进入 H6 候选与手工计划；"
                    "每日采集会自动核验 SEC 身份和发行商/SEC 权威跟踪指数，"
                    "只按已确认的跟踪指数去重。"
                ),
            },
            "notice": MANUAL_ONLY_NOTICE,
        }

    def etf_benchmark_census(
        self, session: Session, *, refresh_missing: bool = False, batch_size: int = 5,
        start_after: str | None = None,
    ) -> dict[str, Any]:
        archive, bundle = self.ensure_universe_archive(session)
        tickers = [str(row.get("tickerSymbol") or "") for row in bundle["etfs"]]
        return self._etf_benchmark_service().census(
            session,
            ticker_symbols=tickers,
            source_meta={
                "archive_id": archive.archive_id,
                "as_of_date": bundle["as_of_date"],
                "intersection_sha256": bundle["intersection_sha256"],
                "pool_path": str(bundle["pool_path"]),
            },
            refresh_missing=refresh_missing,
            batch_size=batch_size,
            start_after=start_after,
        )

    def _overview_h5_legacy(self, session: Session, *, as_of: str | None = None) -> dict[str, Any]:
        current_run = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_SCOPE)
        legacy_h4 = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_H4_SCOPE)
        legacy_h3 = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_H3_SCOPE)
        run = current_run or legacy_h4 or legacy_h3
        legacy_h2 = repository.latest_run(session, scope=US_MANUAL_H2_SCOPE)
        legacy = repository.latest_run(session, scope=US_MANUAL_LEGACY_SCOPE)
        candidates = repository.list_candidates(session, run.run_id) if run is not None else []
        funnel = dict(run.funnel_json or {}) if run is not None else {}
        cumulative_cost = (
            repository.cost_totals_for_date(session, as_of_date=run.as_of_date)
            if run is not None else None
        )
        result: dict[str, Any] = {
            "capabilities": self.capabilities(),
            "notice": MANUAL_ONLY_NOTICE,
            "policy": policy_payload(),
            "state": run.status if run is not None else "waiting_update",
            "run": self.run_payload(session, run, include_candidates=False) if run is not None else None,
            "candidates": [self._stop_service().candidate_payload(session, row) for row in candidates],
            "positions": self.position_summary(session),
            "next_step": self._next_step(session, run, candidates),
            "schedule": self._h5().schedule_payload(),
            "last_attempt": ({
                "run_id": run.run_id,
                "trigger": run.trigger,
                "attempt_count": run.attempt_count,
                "created_at": serialize(run.created_at),
                "completed_at": serialize(run.completed_at),
                "next_retry_at": serialize(run.next_retry_at),
            } if run is not None else None),
            "data_update": ({
                "as_of_date": run.as_of_date,
                "us_as_of_date": (run.source_dates_json or {}).get("us_as_of_date"),
                "us_etf_as_of_date": (run.source_dates_json or {}).get("us_etf_as_of_date"),
            } if run is not None else None),
            "cost": ({
                "daily_cap_cny": decimal_text(run.daily_cost_cap_cny),
                "estimated_total_cny": decimal_text(cumulative_cost["estimated_total_cny"]),
                "actual_total_cny": decimal_text(cumulative_cost["actual_total_cny"]),
                "current_run_estimated_cny": decimal_text(run.estimated_total_cost_cny),
                "current_run_actual_cny": decimal_text(run.actual_total_cost_cny),
                "breakdown": serialize(run.cost_breakdown_json or {}),
            } if run is not None else {
                "daily_cap_cny": decimal_text(config.US_MANUAL_DAILY_AUTO_BUDGET),
                "estimated_total_cny": None,
                "actual_total_cny": None,
                "breakdown": {},
            }),
            "combos": {
                "warm_to_hot_stocks": funnel.get("warm_to_hot_stocks", 0),
                "warm_to_hot_etfs": funnel.get("warm_to_hot_etfs", 0),
                "bitget_stock_intersection": funnel.get("bitget_stock_intersection", 0),
                "bitget_etf_intersection": funnel.get("bitget_etf_intersection", 0),
            },
            "quote_status": {
                "available": sum(1 for row in candidates if row.quote_status == "available"),
                "unavailable": sum(1 for row in candidates if row.quote_status == "quote_unavailable"),
            },
            "stop_status": {
                status: sum(
                    1 for row in candidates
                    if self._stop_service().candidate_payload(session, row).get("stop_status") == status
                )
                for status in ("pending", "auto_ready", "review_required", "manual_resolved", "blocked")
            },
            "legacy_h1": ({
                "latest_run_id": legacy.run_id,
                "as_of_date": legacy.as_of_date,
                "status": legacy.status,
                "read_only": True,
            } if legacy is not None else None),
            "legacy_h2": ({
                "latest_run_id": legacy_h2.run_id,
                "as_of_date": legacy_h2.as_of_date,
                "status": legacy_h2.status,
                "read_only": True,
            } if legacy_h2 is not None else None),
            "legacy_h3": ({
                "latest_run_id": legacy_h3.run_id,
                "as_of_date": legacy_h3.as_of_date,
                "status": legacy_h3.status,
                "read_only": True,
            } if legacy_h3 is not None else None),
            "legacy_h4": ({
                "latest_run_id": legacy_h4.run_id,
                "as_of_date": legacy_h4.as_of_date,
                "status": legacy_h4.status,
                "read_only": True,
            } if legacy_h4 is not None else None),
        }
        return result

    def _next_step_h5_legacy(self, session: Session, run: UsDailyRun | None,
                   candidates: list[UsCandidateSnapshot]) -> dict[str, Any]:
        if run is None:
            return {"code": "collect", "title": "等待今日美股数据更新",
                    "detail": "北京时间 07:00 后可手动采集；08:00–10:00 由调度器自动尝试。"}
        if run.rules_version != US_MANUAL_RULES_VERSION:
            return {"code": "collect_h5", "title": "采集 H5 双源止损证据",
                    "detail": "当前展示的是历史 H1–H4 只读结果；请采集当前 H5 候选。"}
        if run.status == "waiting_window":
            return {"code": "waiting_window", "title": "已完成免费更新检查",
                    "detail": "北京时间 07:00 后，手动按钮才会继续调用付费数据。"}
        if run.status == "waiting_update":
            return {"code": "waiting_update", "title": "等待美股与美国 ETF 同日更新",
                    "detail": "日期未对齐时不会调用任何付费接口。"}
        if run.status == "collecting":
            return {"code": "collecting", "title": "正在自动采集",
                    "detail": "重复点击与定时器会复用同一数据库租约，不会重复购买字段。"}
        if run.status == "budget_blocked":
            return {"code": "budget_blocked", "title": "费用上限已阻断",
                    "detail": run.error_message or "预计累计费用超过每日 ¥5 上限，未执行下一次付费调用。"}
        if run.status == "failed":
            return {"code": "retry", "title": "本次采集失败，可安全重试",
                    "detail": run.error_message or "已完成阶段会从本地归档复用。"}
        if run.status in {"ready", "ready_degraded"}:
            stop_service = self._stop_service()
            stop_rows = [stop_service.candidate_payload(session, row) for row in candidates]
            ready = [
                row for row in stop_rows
                if (row.get("stop_suggestion") or {}).get("planning_enabled")
            ]
            unavailable = [row for row in candidates if row.screen_status == "ready" and row.quote_status != "available"]
            review = [row for row in stop_rows if row.get("stop_status") == "review_required"]
            blocked = [row for row in stop_rows if row.get("stop_status") == "blocked"]
            if config.US_MANUAL_STOP_MODE == "shadow":
                detail = "只采集并核验双源证据，不人工定稿或生成仓位。"
                if review:
                    detail += f"{len(review)} 只偏差超限仅记录为 shadow 证据。"
                if blocked:
                    detail += f"{len(blocked)} 只缺源候选可单独重试。"
                detail += "连续三个真实数据日核验后再由运维切 active。"
                return {"code": "shadow", "title": "H5 正在影子验证", "detail": detail}
            if ready:
                detail = f"已有 {len(ready)} 只个股/ETF 具备冻结的双源止损，可直接计算仓位。"
                if unavailable:
                    detail += f"另有 {len(unavailable)} 只等待报价，可单独刷新。"
                return {"code": "complete_sizing", "title": "选择候选并计算风险仓位", "detail": detail}
            if review:
                return {"code": "review_stops", "title": "复核双源偏差",
                        "detail": f"{len(review)} 只候选双源偏差超过 1.5%，需选择证据源并填写理由。"}
            if blocked:
                return {"code": "retry_stops", "title": "补齐双源止损证据",
                        "detail": f"{len(blocked)} 只候选缺源或日期无法对齐；只能重试，不能单源绕过。"}
            if config.US_MANUAL_STOP_MODE == "off":
                return {"code": "stop_off", "title": "H5 止损阶段未启用",
                        "detail": "当前环境为 off；不会调用 Wind，也不能生成新仓位计划。"}
            if unavailable:
                return {"code": "refresh_quotes", "title": "刷新 Bitget 公共报价",
                        "detail": f"{len(unavailable)} 只质量候选缺少报价，其他筛选结果已保存。"}
            return {"code": "no_trade", "title": "今日不交易",
                    "detail": "今日没有满足 H5 门槛与双源纪律的可计划标的；不会自动放宽条件。"}
        return {"code": "collect", "title": "继续 H5 采集",
                "detail": "已完成阶段会命中缓存；单日预计费用不超过 ¥5 时自动继续。"}

    def _run_payload_h5_legacy(
        self, session: Session, run: UsDailyRun, *, include_candidates: bool = True,
    ) -> dict[str, Any]:
        payload = repository.model_payload(run)
        payload["funnel"] = serialize(run.funnel_json or {})
        if include_candidates:
            payload["candidates"] = [
                self._stop_service().candidate_payload(session, row)
                for row in repository.list_candidates(session, run.run_id)
            ]
        return payload

    def _collect_h5_legacy(self, session: Session, *, trigger: str = "manual",
                now: Any | None = None) -> dict[str, Any]:
        return self._h5().collect(session, trigger=trigger, now=now)

    def _refresh_candidate_quote_h5_legacy(
        self, session: Session, *, candidate_id: int,
    ) -> dict[str, Any]:
        return self._h5().refresh_quote(session, candidate_id=candidate_id)

    def _candidate_payload(self, session: Session, row: UsCandidateSnapshot) -> dict[str, Any]:
        run = repository.get_run(session, row.run_id)
        if run.rules_version == US_MANUAL_RULES_VERSION:
            payload = self._risk_anchor_service().candidate_payload(session, row)
        else:
            payload = {**repository.model_payload(row), "legacy_read_only": True}
        if row.etf_benchmark_evidence_id:
            evidence = repository.get_etf_benchmark(session, row.etf_benchmark_evidence_id)
            payload["etf_benchmark_evidence"] = EtfBenchmarkService().payload(session, evidence)
        else:
            payload["etf_benchmark_evidence"] = None
        return payload

    def candidate_detail(self, session: Session, *, candidate_id: int) -> dict[str, Any]:
        """Return one candidate together with immutable ETF and plan links."""
        row = repository.get_candidate(session, candidate_id)
        payload = self._candidate_payload(session, row)
        links: list[dict[str, Any]] = []
        for item in repository.plan_items_for_candidate(session, candidate_id):
            plan = repository.get_plan(session, item.plan_id)
            links.append({
                "plan_id": plan.plan_id,
                "plan_status": plan.status,
                "plan_rules_version": plan.rules_version,
                "plan_legacy_read_only": plan.rules_version != US_MANUAL_RULES_VERSION,
                "item_id": item.item_id,
                "item_status": item.status,
                "side": item.side,
                "allocation_preview_item_id": item.allocation_preview_item_id,
                "risk_anchor_id": item.risk_anchor_id,
                "exit_decision_id": item.exit_decision_id,
            })
        payload["plan_links"] = links
        return payload

    def overview(self, session: Session, *, as_of: str | None = None) -> dict[str, Any]:
        current = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_SCOPE)
        legacy_h5 = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_H5_SCOPE)
        legacy_h4 = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_H4_SCOPE)
        legacy_h3 = repository.latest_run(session, as_of_date=as_of, scope=US_MANUAL_H3_SCOPE)
        run = current or legacy_h5 or legacy_h4 or legacy_h3
        legacy_h2 = repository.latest_run(session, scope=US_MANUAL_H2_SCOPE)
        legacy_h1 = repository.latest_run(session, scope=US_MANUAL_LEGACY_SCOPE)
        candidates = repository.list_candidates(session, run.run_id) if run is not None else []
        candidate_payloads = [self._candidate_payload(session, row) for row in candidates]
        funnel = dict(run.funnel_json or {}) if run is not None else {}
        environment = (
            repository.environment_for_run(session, run.run_id)
            if run is not None and run.rules_version == US_MANUAL_RULES_VERSION else None
        )
        capacity = None
        if environment is not None and run is not None:
            capacity = capacity_snapshot(
                session,
                environment=environment,
                intended_execution_date=next_us_session(run.as_of_date),
            )
        cumulative_cost = (
            repository.cost_totals_for_date(session, as_of_date=run.as_of_date)
            if run is not None else None
        )
        positions = self.position_summary(session)
        account = account_state(session)
        latest_ocr = repository.latest_account_ocr_batch(session)
        risk_statuses = {
            status: sum(1 for row in candidate_payloads if row.get("risk_anchor_status") == status)
            for status in ("pending", "ready", "blocked")
        }
        result = {
            "capabilities": self.capabilities(),
            "notice": MANUAL_ONLY_NOTICE,
            "policy": policy_payload(),
            "state": run.status if run is not None else "waiting_update",
            "run": self.run_payload(session, run, include_candidates=False) if run is not None else None,
            "candidates": candidate_payloads,
            "positions": positions,
            "exit_actions": {
                "actionable": sum(1 for row in positions if row.get("action") in {"exit_all", "reduce_25", "reduce_50"}),
                "manual_review": sum(1 for row in positions if row.get("action") == "manual_review"),
                "legacy_read_only": sum(1 for row in positions if row.get("legacy_read_only")),
            },
            "market_environment": self._environment_payload(environment),
            "capacity": serialize(capacity),
            "account": serialize(account),
            "latest_account_ocr": (
                account_ocr_payload(session, latest_ocr) if latest_ocr is not None else None
            ),
            "next_step": self._next_step(session, run, candidate_payloads, positions),
            "schedule": self._h6().schedule_payload(),
            "last_attempt": ({
                "run_id": run.run_id,
                "trigger": run.trigger,
                "attempt_count": run.attempt_count,
                "created_at": serialize(run.created_at),
                "completed_at": serialize(run.completed_at),
                "next_retry_at": serialize(run.next_retry_at),
            } if run is not None else None),
            "data_update": ({
                "as_of_date": run.as_of_date,
                "us_as_of_date": (run.source_dates_json or {}).get("us_as_of_date"),
                "us_etf_as_of_date": (run.source_dates_json or {}).get("us_etf_as_of_date"),
            } if run is not None else None),
            "cost": ({
                "daily_cap_cny": decimal_text(run.daily_cost_cap_cny),
                "estimated_total_cny": decimal_text(cumulative_cost["estimated_total_cny"]),
                "actual_total_cny": decimal_text(cumulative_cost["actual_total_cny"]),
                "current_run_estimated_cny": decimal_text(run.estimated_total_cost_cny),
                "current_run_actual_cny": decimal_text(run.actual_total_cost_cny),
                "breakdown": serialize(run.cost_breakdown_json or {}),
            } if run is not None else {
                "daily_cap_cny": decimal_text(config.US_MANUAL_DAILY_AUTO_BUDGET),
                "estimated_total_cny": None,
                "actual_total_cny": None,
                "breakdown": {},
            }),
            "combos": {
                "warm_to_hot_stocks": funnel.get("warm_to_hot_stocks", 0),
                "warm_to_hot_etfs": funnel.get("warm_to_hot_etfs", 0),
                "bitget_stock_intersection": funnel.get("bitget_stock_intersection", 0),
                "bitget_etf_intersection": funnel.get("bitget_etf_intersection", 0),
            },
            "quote_status": {
                "available": sum(1 for row in candidates if row.quote_status == "available"),
                "unavailable": sum(1 for row in candidates if row.quote_status == "quote_unavailable"),
            },
            "risk_anchor_status": risk_statuses,
            "etf_benchmark_status": {
                "verified": sum(1 for row in candidates if row.asset_type == "etf" and row.benchmark_status == "verified"),
                "blocked": sum(1 for row in candidates if row.asset_type == "etf" and row.benchmark_status != "verified"),
            },
            "legacy": {
                "h1": repository.model_payload(legacy_h1) if legacy_h1 is not None else None,
                "h2": repository.model_payload(legacy_h2) if legacy_h2 is not None else None,
                "h3": repository.model_payload(legacy_h3) if legacy_h3 is not None else None,
                "h4": repository.model_payload(legacy_h4) if legacy_h4 is not None else None,
                "h5": repository.model_payload(legacy_h5) if legacy_h5 is not None else None,
                "read_only": True,
            },
        }
        # Keep the old top-level archive links as read-only compatibility aliases.
        # New H6 clients should use ``legacy.h1`` … ``legacy.h5``.
        for version, row in (
            ("h1", legacy_h1), ("h2", legacy_h2), ("h3", legacy_h3),
            ("h4", legacy_h4), ("h5", legacy_h5),
        ):
            result[f"legacy_{version}"] = ({
                "latest_run_id": row.run_id,
                "as_of_date": row.as_of_date,
                "status": row.status,
                "read_only": True,
            } if row is not None else None)
        return result

    def _next_step(
        self,
        session: Session,
        run: UsDailyRun | None,
        candidates: list[dict[str, Any]],
        positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        positions = positions or []
        actionable = [row for row in positions if row.get("action") in {"exit_all", "reduce_25", "reduce_50"}]
        manual_reviews = [row for row in positions if row.get("action") == "manual_review"]
        if actionable:
            return {
                "code": "execute_exits",
                "title": "先处理趋势退出",
                "detail": f"{len(actionable)} 个持仓有待执行卖出清单；卖出成交回填前不计入可用现金。",
            }
        if manual_reviews:
            return {
                "code": "review_exits",
                "title": "持仓退出数据待复核",
                "detail": f"{len(manual_reviews)} 个持仓退出字段不完整，新买入失败关闭。",
            }
        if run is None:
            return {"code": "collect", "title": "等待今日美股数据更新",
                    "detail": "北京时间 07:00 后可手动采集；08:00–10:00 自动尝试。"}
        if run.rules_version != US_MANUAL_RULES_VERSION:
            return {"code": "collect_h6", "title": "采集当前 H6 证据",
                    "detail": "当前是 H1–H5 历史只读结果，不伪造环境、锚点或退出字段。"}
        if run.status in {
            "waiting_window", "waiting_update", "collecting", "budget_blocked",
            "exit_data_blocked", "exit_ready_buy_blocked_budget", "failed",
        }:
            titles = {
                "waiting_window": "已完成免费更新检查",
                "waiting_update": "等待美股与美国 ETF 同日更新",
                "collecting": "正在采集 H6 证据",
                "budget_blocked": "费用上限已阻断",
                "exit_data_blocked": "退出数据被费用或来源阻断",
                "exit_ready_buy_blocked_budget": "退出已检查，买入被费用上限阻断",
                "failed": "本次采集失败，可安全重试",
            }
            return {"code": run.status, "title": titles[run.status], "detail": run.error_message or "已付费阶段会命中归档，不重复购买。"}
        environment = repository.environment_for_run(session, run.run_id)
        if (
            run.environment_status != "ready"
            or environment is None
            or environment.status != "ready"
        ):
            return {"code": "environment_blocked", "title": "今日不开新仓",
                    "detail": "美股整体温度缺失或不可识别，候选仅供观察。"}
        if environment.environment_factor <= 0:
            return {
                "code": "environment_zero",
                "title": "今日不开新仓",
                "detail": f"美股整体温度为{environment.market_temperature}，环境系数为 0；已有持仓仍按趋势退出纪律处理。",
            }
        if config.US_MANUAL_H6_MODE == "off":
            return {"code": "h6_off", "title": "H6 风险锚点未启用",
                    "detail": "当前 off 只保留筛选证据，不生成仓位分配。"}
        blocked = [row for row in candidates if row.get("risk_anchor_status") == "blocked"]
        ready = [row for row in candidates if (row.get("risk_anchor") or {}).get("planning_enabled")]
        if config.US_MANUAL_H6_MODE == "shadow":
            return {"code": "shadow", "title": "H6 正在影子验证",
                    "detail": f"已生成 {len(candidates) - len(blocked)} 只锚点证据，{len(blocked)} 只阻断；满三个真实数据日后才可切 active。"}
        if ready:
            return {"code": "create_allocation", "title": "生成今日推荐分配",
                    "detail": f"{len(ready)} 只候选可按环境总容量、50U 单票目标和 2.5U 锚点风险自动填充。"}
        if blocked:
            return {"code": "retry_risk_anchors", "title": "重试风险锚点",
                    "detail": f"{len(blocked)} 只候选的 Bitget 1D/1H 或 EP3 证据不完整，不允许单源绕过。"}
        return {"code": "no_trade", "title": "今日不交易",
                "detail": "今日无满足 H6 筛选、ETF 证据与风险锚点条件的标的。"}

    def run_payload(
        self, session: Session, run: UsDailyRun, *, include_candidates: bool = True,
    ) -> dict[str, Any]:
        payload = repository.model_payload(run)
        payload["funnel"] = serialize(run.funnel_json or {})
        payload["legacy_read_only"] = run.rules_version != US_MANUAL_RULES_VERSION
        environment = repository.environment_for_run(session, run.run_id)
        payload["market_environment"] = (
            self._environment_payload(environment)
        )
        if include_candidates:
            payload["candidates"] = [
                self._candidate_payload(session, row)
                for row in repository.list_candidates(session, run.run_id)
            ]
        return payload

    def collect(self, session: Session, *, trigger: str = "manual",
                now: Any | None = None) -> dict[str, Any]:
        return self._h6().collect(session, trigger=trigger, now=now)

    def refresh_candidate_quote(self, session: Session, *, candidate_id: int) -> dict[str, Any]:
        return self._h6().refresh_quote(session, candidate_id=candidate_id)

    def _new_run(self, session: Session, *, archive: UsUniverseArchive, bundle: dict[str, Any],
                 as_of_date: str, plan: dict[str, Any], audit: dict[str, Any]) -> UsDailyRun:
        field_hash = sha256(list(BASE_SIGNAL_FIELDS))
        existing = repository.cached_run(session, as_of_date=as_of_date, scope=US_MANUAL_LEGACY_SCOPE,
                                         base_fields_hash=field_hash)
        if existing is not None:
            return existing
        run = UsDailyRun(
            run_id=f"us-run-{as_of_date.replace('-', '')}-{uuid4().hex[:10]}",
            as_of_date=as_of_date,
            scope=US_MANUAL_LEGACY_SCOPE,
            status="awaiting_budget",
            rules_version=US_MANUAL_LEGACY_RULES_VERSION,
            universe_archive_id=archive.archive_id,
            base_fields=list(BASE_SIGNAL_FIELDS),
            base_fields_hash=field_hash,
            estimated_base_cost_cny=Decimal(str(plan["estimated_cost_cny"])),
            universe_count=int(plan["universe_count"]),
            funnel_json={
                "strict_bitget_us_stocks": int(plan["universe_count"]),
                "warm_to_hot": None,
                "right_side_within_window": None,
                "ready_for_plan": 0,
            },
            source_dates_json={
                "signal_as_of_date": as_of_date,
                "membership_archive_date": archive.trend_animals_as_of_date,
                "membership_as_of_dates": plan.get("membership_as_of_dates") or [],
                "preflight_audit": audit,
            },
        )
        return repository.save_run(session, run)

    def preflight(self, session: Session) -> dict[str, Any]:
        archive, bundle = self.ensure_universe_archive(session)
        client = self.client_factory()
        try:
            docs = client.get_api_doc_intro()
            changes = client.get_change_log()
            billing = client.get_snapshot_billing()
            status = _us_status(client.get_update_status())
            as_of_date = str(status["asOfDate"])
            plan = build_signal_scan_plan(
                pool=bundle["pool"], billing=billing, expected_as_of_date=as_of_date,
            )
            audit = {
                "preflight_at": utc_now_text(),
                "api_doc_entries": len(docs) if isinstance(docs, list) else None,
                "latest_changelog": _changed_fields(changes),
                "update_status": {key: status.get(key) for key in ("tmId", "asset", "asOfDate", "updateDt")},
                "pricing_cny_per_row": {field: billing_map(billing).get(field) for field in BASE_SIGNAL_FIELDS},
            }
            run = self._new_run(session, archive=archive, bundle=bundle, as_of_date=as_of_date,
                                plan=plan, audit=audit)
            payload = self.run_payload(session, run, include_candidates=False)
            payload.update({
                "cache_reused": run.status in {"ready", "ready_cached"},
                "scan_plan": {**plan, "batches": [
                    {"row_count": batch["row_count"], "estimated_cost_cny": batch["estimated_cost_cny"]}
                    for batch in plan["batches"]
                ]},
                "notice": MANUAL_ONLY_NOTICE,
            })
            return payload
        except TrendAnimalsError as exc:
            raise UsManualError(exc.code, str(exc), 503) from exc
        finally:
            client.close()

    def scan(self, session: Session, *, run_id: str, approved_budget_cny: Any) -> dict[str, Any]:
        run = repository.get_run(session, run_id)
        if run.status in {"ready", "ready_cached", "base_ready", "awaiting_enrichment_budget"}:
            return {**self.run_payload(session, run), "cache_reused": True}
        if run.status == "scanning":
            raise UsManualError("scan_in_progress", "该扫描正在执行；请勿重复提交", 409)
        if run.status not in {"awaiting_budget", "preflight_ready"}:
            raise UsManualError("run_not_scannable", f"当前扫描状态 {run.status} 不允许执行", 409)
        approved = parse_decimal(approved_budget_cny, field="approved_budget_cny", non_negative=True)
        estimated = run.estimated_base_cost_cny or Decimal("0")
        if approved < estimated:
            raise UsManualError("budget_confirmation_required", "批准上限低于预计费用，已阻断付费扫描", 422,
                                {"estimated_cost_cny": decimal_text(estimated), "approved_budget_cny": decimal_text(approved)})
        archive = repository.get_universe_archive(session, str(run.universe_archive_id))
        bundle = self.archive_bundle()
        if bundle["as_of_date"] != archive.trend_animals_as_of_date:
            raise UsManualError("universe_changed", "覆盖归档已变化；请重新免费预检", 409)
        client = self.client_factory()
        try:
            plan = build_signal_scan_plan(
                pool=bundle["pool"], billing=client.get_snapshot_billing(), expected_as_of_date=run.as_of_date,
            )
            if sha256(list(plan["requested_fields"])) != run.base_fields_hash:
                raise UsManualError("field_set_changed", "实时字段集与预检不一致，已阻断付费扫描")
            if Decimal(str(plan["estimated_cost_cny"])) > approved:
                raise UsManualError("budget_confirmation_required", "实时预计费用超过批准上限，已阻断付费扫描", 422,
                                    {"estimated_cost_cny": str(plan["estimated_cost_cny"]), "approved_budget_cny": decimal_text(approved)})
            repository.update_run(session, run, status="scanning", approved_base_budget_cny=approved)
            before = ledger_mark(client)
            raw_rows: list[dict[str, Any]] = []
            for batch in plan["batches"]:
                rows = client.get_snapshot(batch["tm_ids"], list(BASE_SIGNAL_FIELDS))
                if not isinstance(rows, list):
                    raise UsManualError("api_contract_error", "趋势动物基础快照不是数组")
                raw_rows.extend(rows)
            screen = classify_signal_rows(
                pool=bundle["pool"], snapshot_rows=raw_rows, expected_as_of_date=run.as_of_date,
            )
            actual = ledger_delta(client, before)
            archive_path = self._scan_root / run.as_of_date / "signal_scan_bitget_us_stocks.json"
            raw_payload = {
                "schema_version": 1,
                "created_at_utc": utc_now_text(),
                "pool_file": str(bundle["pool_path"]),
                "scan_plan": plan,
                "cost": {
                    "estimated_cost_cny": plan["estimated_cost_cny"],
                    "approved_budget_cny": decimal_text(approved),
                    "actual_cost_cny": actual,
                    "actual_cost_note": "免费账单差额不可用时保留 null，不伪造实际费用。",
                },
                "raw_snapshot_rows": raw_rows,
                "screen": screen,
            }
            _write_json_atomic(archive_path, raw_payload)
            repository.replace_candidates(
                session, run=run,
                candidates=[self._candidate_from_screen_row(run_id=run.run_id, row=row)
                            for row in screen["all_candidates"]],
            )
            updated = repository.update_run(
                session, run,
                status="base_ready",
                actual_base_cost_cny=_decimal_or_none(actual),
                returned_count=int(screen["returned_snapshot_count"]),
                funnel_json=self._funnel_from_screen(screen),
                raw_archive_path=str(archive_path),
                raw_sha256=sha256(raw_payload),
                error_code=None,
                error_message=None,
            )
            return self.run_payload(session, updated)
        except (TrendAnimalsError, UsManualError) as exc:
            code = exc.code if isinstance(exc, (TrendAnimalsError, UsManualError)) else "scan_failed"
            message = str(exc)
            repository.update_run(session, run, status="blocked", error_code=code, error_message=message)
            if isinstance(exc, UsManualError):
                raise
            raise UsManualError(code, message, 503) from exc
        finally:
            client.close()

    def enrichment_preflight(self, session: Session, *, run_id: str) -> dict[str, Any]:
        run = repository.get_run(session, run_id)
        if run.status in {"ready", "ready_cached"} and run.enrichment_fields:
            return {**self.run_payload(session, run, include_candidates=False), "cache_reused": True}
        if run.status not in {"base_ready", "awaiting_enrichment_budget", "ready_cached", "ready"}:
            raise UsManualError("enrichment_not_ready", "请先完成基础扫描", 409)
        candidates = [row for row in repository.list_candidates(session, run_id) if row.screen_status == "screened"]
        if not candidates:
            updated = repository.update_run(session, run, status="ready", completed_at=utc_now())
            return {**self.run_payload(session, updated), "zero_candidates": True}
        client = self.client_factory()
        try:
            billing = client.get_snapshot_billing()
            fields = resolve_enrichment_fields(billing)
            estimated = estimate_snapshot_cost(fields, len(candidates), billing)
            updated = repository.update_run(
                session, run,
                status="awaiting_enrichment_budget",
                enrichment_fields=fields,
                enrichment_fields_hash=enrichment_field_hash(fields),
                estimated_enrichment_cost_cny=Decimal(str(estimated)),
            )
            return {
                **self.run_payload(session, updated, include_candidates=False),
                "candidate_count": len(candidates),
                "deduplicated_industry_count": len({row.industry_tm_id for row in candidates if row.industry_tm_id}),
                "requested_fields": fields,
                "pricing_cny_per_row": {field: billing_map(billing).get(field) for field in fields},
            }
        except TrendAnimalsError as exc:
            raise UsManualError(exc.code, str(exc), 503) from exc
        finally:
            client.close()

    def enrich(self, session: Session, *, run_id: str, approved_budget_cny: Any) -> dict[str, Any]:
        run = repository.get_run(session, run_id)
        if run.status in {"ready", "ready_cached"} and run.enrichment_fields:
            return {**self.run_payload(session, run), "cache_reused": True}
        if run.status != "awaiting_enrichment_budget":
            raise UsManualError("enrichment_not_ready", "请先完成候选补充预检", 409)
        approved = parse_decimal(approved_budget_cny, field="approved_budget_cny", non_negative=True)
        estimated = run.estimated_enrichment_cost_cny or Decimal("0")
        if approved < estimated:
            raise UsManualError("budget_confirmation_required", "批准上限低于候选补充预计费用", 422,
                                {"estimated_cost_cny": decimal_text(estimated), "approved_budget_cny": decimal_text(approved)})
        candidates = [row for row in repository.list_candidates(session, run_id) if row.screen_status == "screened"]
        if not candidates:
            return self.run_payload(session, repository.update_run(session, run, status="ready", completed_at=utc_now()))
        fields = list(run.enrichment_fields or [])
        if not fields:
            raise UsManualError("enrichment_fields_missing", "候选补充字段缺失；请重新执行补充预检", 409)
        client = self.client_factory()
        try:
            billing = client.get_snapshot_billing()
            recalculated = Decimal(str(estimate_snapshot_cost(fields, len(candidates), billing)))
            if recalculated > approved:
                raise UsManualError("budget_confirmation_required", "实时候选补充费用超过批准上限", 422)
            repository.update_run(session, run, status="enriching", approved_enrichment_budget_cny=approved)
            before = ledger_mark(client)
            raw_rows = client.get_snapshot([row.tm_id for row in candidates], fields)
            by_id = validate_snapshot_rows(raw_rows, wanted_tm_ids={row.tm_id for row in candidates},
                                           as_of_date=run.as_of_date)
            for candidate in candidates:
                apply_enrichment(candidate, by_id[candidate.tm_id])
            selected, _ = rank_ready_candidates(candidates)
            session.add_all(candidates)
            session.commit()
            actual = ledger_delta(client, before)
            archive_path = self._scan_root / run.as_of_date / "candidate_enrichment_bitget_us_stocks.json"
            enrichment_payload = {
                "schema_version": 1,
                "created_at_utc": utc_now_text(),
                "run_id": run.run_id,
                "fields": fields,
                "candidate_tm_ids": [row.tm_id for row in candidates],
                "raw_snapshot_rows": raw_rows,
                "actual_cost_cny": actual,
            }
            _write_json_atomic(archive_path, enrichment_payload)
            funnel = dict(run.funnel_json or {})
            funnel["ready_for_plan"] = len(selected)
            funnel["enrichment_candidate_count"] = len(candidates)
            funnel["industry_dedup_selected_count"] = len(selected)
            updated = repository.update_run(
                session, run,
                status="ready",
                actual_enrichment_cost_cny=_decimal_or_none(actual),
                funnel_json=funnel,
                completed_at=utc_now(),
                error_code=None,
                error_message=None,
            )
            return self.run_payload(session, updated)
        except (TrendAnimalsError, UsManualError) as exc:
            code = exc.code if isinstance(exc, (TrendAnimalsError, UsManualError)) else "enrichment_failed"
            repository.update_run(session, run, status="blocked", error_code=code, error_message=str(exc))
            if isinstance(exc, UsManualError):
                raise
            raise UsManualError(code, str(exc), 503) from exc
        finally:
            client.close()

    def quote(self, session: Session, *, venue_instrument: str) -> dict[str, Any]:
        archive, bundle = self.ensure_universe_archive(session)
        wanted = venue_instrument.upper()
        allowed = {
            str(row["venue"].get("venue_instrument") or "").upper()
            for row in [*bundle.get("stocks", []), *bundle.get("etfs", [])]
            if isinstance(row.get("venue"), dict)
        }
        if wanted not in allowed:
            raise UsManualError("venue_instrument_not_in_universe", "该产品不在当前 Bitget 严格交集股池", 404)
        return fetch_public_quote(wanted)

    def position_summary(self, session: Session) -> list[dict[str, Any]]:
        from backend.us_manual.exits import position_actions
        return position_actions(session, as_of_date=None)

    def universe_preflight(self, session: Session) -> dict[str, Any]:
        """覆盖刷新不是日任务；只给出本地证据和必须人工确认的风险说明。"""
        archive, bundle = self.ensure_universe_archive(session)
        return {
            "status": "reuse_existing_identity",
            "archive": repository.model_payload(archive),
            "membership_archive_date": bundle["as_of_date"],
            "action_required": "如需刷新覆盖，必须单独确认 all_basic 展开费用风险；不与每日信号扫描合并。",
            "refresh_allowed": False,
            "notice": "根节点日期变化不自动触发全量成员覆盖刷新。",
        }

    def universe_refresh(self, session: Session, *, confirm_unbounded_all_basic: bool) -> dict[str, Any]:
        if not confirm_unbounded_all_basic:
            raise UsManualError(
                "coverage_refresh_confirmation_required",
                "全量成员展开的计费行数没有文档上限；必须明确确认该费用风险。",
                422,
            )
        # 不在网页后台隐式运行未知上限的 all_basic 请求。CLI 归档器保留完整审计
        # 证据和显式 `--confirm-unbounded-all-basic`，网页只清楚地告知下一步。
        raise UsManualError(
            "coverage_refresh_requires_audited_cli",
            "覆盖刷新必须通过受审计的 archive_trend_animals_us_coverage.py 执行，网页不会后台执行未知上限请求。",
            409,
        )
