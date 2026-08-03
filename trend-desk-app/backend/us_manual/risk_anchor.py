"""H6 Bitget rToken EP3 前期重要低点风险锚点。

锚点只用于 2.5U 风险预算反推仓位；真正卖出仅由趋势动物退出字段决定。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from sqlmodel import Session

from backend import config
from backend.db import UsRiskAnchor
from backend.us_manual import repository
from backend.us_manual.bitget_public import fetch_public_candles, fetch_public_quote
from backend.us_manual.contracts import (
    US_MANUAL_RULES_VERSION,
    US_MANUAL_SCOPE,
    US_RISK_ANCHOR_ALGORITHM_VERSION,
    UsManualError,
    canonical_json,
    serialize,
    sha256,
)
from backend.us_manual.stop_rules import (
    _confirmed_important_lows,
    buffered_stop,
    price_tick,
    validate_bitget_daily,
)


XNYS = xcals.get_calendar("XNYS")
NEW_YORK = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")
ALGORITHM_CONTRACT = {
    "source": "bitget_public_rtoken",
    "hourly_interval": "1H",
    "daily_interval": "1D_continuity_only",
    "calendar": "XNYS",
    "window_calendar_days": 120,
    "max_sessions": 60,
    "ep3": {"k": 2, "breakout_pct": "0", "strict": True, "future_data": False},
    "buffer": "floor_to_price_precision_then_minus_one_tick",
    "semantic": "risk_anchor_only_not_exit_stop",
}
REQUIRED_COMPLETE_SESSIONS = 60


def next_us_session(value: str | date) -> str:
    label = pd.Timestamp(str(value)[:10])
    if XNYS.is_session(label):
        return XNYS.next_session(label).date().isoformat()
    return XNYS.date_to_session(label, direction="next").date().isoformat()


def execution_schedule(
    *, signal_date: str, intended_execution_date: str, generated_at: datetime | None,
) -> dict[str, str | None]:
    """Return an authoritative XNYS-open timeline with explicit time zones.

    The browser must not infer US daylight-saving offsets from a bare session
    date.  This payload is derived from the same exchange calendar used to
    choose ``intended_execution_date``.
    """
    label = pd.Timestamp(str(intended_execution_date)[:10])
    if not XNYS.is_session(label):
        raise UsManualError(
            "intended_execution_session_invalid",
            "预定执行日不是 XNYS 常规交易日",
            409,
            {"intended_execution_date": intended_execution_date},
        )
    opened = XNYS.session_open(label).to_pydatetime().astimezone(timezone.utc)
    created = generated_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "calendar": "XNYS",
        "signal_date": str(signal_date)[:10],
        "session_date": label.date().isoformat(),
        "generated_at_utc": created.astimezone(timezone.utc).isoformat() if created else None,
        "generated_at_beijing": created.astimezone(BEIJING).isoformat() if created else None,
        "open_utc": opened.isoformat(),
        "open_new_york": opened.astimezone(NEW_YORK).isoformat(),
        "open_beijing": opened.astimezone(BEIJING).isoformat(),
    }


def _as_decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise UsManualError("risk_anchor_data_invalid", f"{field} 不是有效十进制定点数", 422) from exc
    if not result.is_finite() or result <= 0:
        raise UsManualError("risk_anchor_data_invalid", f"{field} 必须为有限正数", 422)
    return result


def aggregate_regular_sessions(
    rows: Iterable[dict[str, Any]], *, signal_date: str | date,
) -> list[dict[str, Any]]:
    """用 XNYS 日历按每个 session 的 open/close 聚合重叠 1H rToken 桶。"""
    signal = pd.Timestamp(str(signal_date)[:10])
    start = signal - pd.Timedelta(days=120)
    sessions = list(XNYS.sessions_in_range(start, signal))
    if len(sessions) < REQUIRED_COMPLETE_SESSIONS:
        raise UsManualError(
            "risk_anchor_sessions_incomplete",
            "120 个自然日窗口内不足 60 个美股交易日",
            409,
        )
    required_sessions = sessions[-REQUIRED_COMPLETE_SESSIONS:]
    materialized = list(rows)
    timestamps = [row.get("timestamp") for row in materialized]
    if any(not isinstance(value, datetime) or value.tzinfo is None for value in timestamps):
        raise UsManualError("bitget_candles_contract_error", "Bitget 1H K 线时间戳无时区", 409)
    if len(set(timestamps)) != len(timestamps):
        raise UsManualError("bitget_candles_contract_error", "Bitget 1H K 线包含重复时间戳", 409)
    materialized.sort(key=lambda row: row["timestamp"])

    output: list[dict[str, Any]] = []
    for session_label in required_sessions:
        opened = XNYS.session_open(session_label).to_pydatetime()
        closed = XNYS.session_close(session_label).to_pydatetime()
        selected: list[dict[str, Any]] = []
        for row in materialized:
            start_at = row["timestamp"].astimezone(timezone.utc)
            end_at = start_at + timedelta(hours=1)
            if start_at < closed and end_at > opened:
                selected.append(row)
        if not selected:
            raise UsManualError(
                "bitget_hourly_session_missing",
                f"Bitget 1H K 线缺少 {session_label.date()} 常规交易时段",
                409,
            )
        selected.sort(key=lambda row: row["timestamp"])
        cursor = opened
        for row in selected:
            start_at = max(row["timestamp"].astimezone(timezone.utc), opened)
            end_at = min(row["timestamp"].astimezone(timezone.utc) + timedelta(hours=1), closed)
            if start_at > cursor:
                raise UsManualError(
                    "bitget_hourly_session_incomplete",
                    f"Bitget 1H K 线在 {session_label.date()} 常规时段存在缺口",
                    409,
                )
            cursor = max(cursor, end_at)
        if cursor < closed:
            raise UsManualError(
                "bitget_hourly_session_incomplete",
                f"Bitget 1H K 线未覆盖 {session_label.date()} 收盘前时段",
                409,
            )
        opened_price = _as_decimal(selected[0].get("open"), field="hourly open")
        closed_price = _as_decimal(selected[-1].get("close"), field="hourly close")
        high = max(_as_decimal(row.get("high"), field="hourly high") for row in selected)
        low = min(_as_decimal(row.get("low"), field="hourly low") for row in selected)
        if low > min(opened_price, closed_price) or high < max(opened_price, closed_price):
            raise UsManualError("bitget_candles_contract_error", "Bitget 聚合日线 OHLC 无效", 409)
        output.append({
            "date": session_label.date(),
            "open": opened_price,
            "high": high,
            "low": low,
            "close": closed_price,
            "session_open": opened,
            "session_close": closed,
            "hourly_bar_count": len(selected),
            "hourly_timestamps": [row["timestamp"] for row in selected],
        })
    if len(output) != REQUIRED_COMPLETE_SESSIONS:
        raise UsManualError("risk_anchor_sessions_incomplete", "完整常规交易日不足 60 个，无法确认 EP3", 409)
    return output


def select_ep3_risk_anchor(
    rows: Iterable[dict[str, Any]], *, quote: Any, price_precision: Any,
) -> dict[str, Any]:
    materialized = list(rows)
    important = _confirmed_important_lows(materialized, k=2)
    if not important:
        raise UsManualError("risk_anchor_not_confirmed", "窗口内没有已确认的 EP3 前期重要低点", 409)
    reference = _as_decimal(quote, field="Bitget quote")
    candidate = important[-1]
    anchor = buffered_stop(candidate["low"], price_precision=price_precision)
    if anchor >= reference:
        raise UsManualError(
            "risk_anchor_not_below_quote",
            "最近的已确认重要低点不低于当前 Bitget 公开报价；不得改挑更早低点以通过",
            409,
        )
    distance = (reference - anchor) / reference
    return {
        "anchor_date": candidate["date"],
        "anchor_price": anchor,
        "anchor_distance": distance,
        "price_tick": price_tick(price_precision),
        "ep3": candidate,
        "bar_count": len(materialized),
        "important_low_count": len(important),
        "future_data_used": False,
    }


class RiskAnchorService:
    def __init__(
        self,
        *,
        data_root: Path | None = None,
        quote_fetcher: Callable[[str], dict[str, Any]] = fetch_public_quote,
        candle_fetcher: Callable[..., list[dict[str, Any]]] = fetch_public_candles,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.data_root = data_root or config.DATA
        self.quote_fetcher = quote_fetcher
        self.candle_fetcher = candle_fetcher
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    @property
    def mode(self) -> str:
        return config.US_MANUAL_H6_MODE

    @property
    def root(self) -> Path:
        return self.data_root / "research" / "bitget" / "us_risk_anchor_h6"

    def _archive(self, candidate_id: int, input_hash: str, payload: dict[str, Any]) -> str:
        path = self.root / str(candidate_id) / f"{input_hash}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        return str(path)

    def refresh(self, session: Session, *, candidate_id: int, allow_collecting: bool = False) -> dict[str, Any]:
        candidate = repository.get_candidate(session, candidate_id)
        run = repository.get_run(session, candidate.run_id)
        latest = repository.latest_run(session, scope=US_MANUAL_SCOPE)
        if run.rules_version != US_MANUAL_RULES_VERSION or (latest is not None and latest.run_id != run.run_id):
            raise UsManualError("legacy_read_only", "历史 H1-H5 候选只读；请从当前 H6 run 重新选择", 409)
        allowed_statuses = {"ready", "ready_degraded"} | ({"collecting"} if allow_collecting else set())
        if run.status not in allowed_statuses or candidate.screen_status != "ready":
            raise UsManualError("candidate_not_plan_ready", "候选未通过当前 H6 选股纪律", 422)
        if candidate.asset_type == "etf" and candidate.benchmark_status != "verified":
            raise UsManualError("etf_benchmark_not_verified", "ETF 基准证据未核验，不能生成风险锚点", 422)
        if not candidate.venue_instrument:
            raise UsManualError("venue_metadata_missing", "候选缺少 Bitget 严格交集产品", 422)

        quote_payload: dict[str, Any] | None = None
        daily: list[dict[str, Any]] = []
        hourly: list[dict[str, Any]] = []
        base_input = {
            "candidate_id": candidate_id,
            "run_id": run.run_id,
            "signal_date": run.as_of_date,
            "venue_instrument": candidate.venue_instrument,
            "venue_metadata": candidate.venue_metadata_json,
            "contract": ALGORITHM_CONTRACT,
        }
        try:
            quote_payload = self.quote_fetcher(candidate.venue_instrument)
            quote = _as_decimal(quote_payload.get("reference_price"), field="Bitget quote")
            signal_label = pd.Timestamp(run.as_of_date)
            session_label = (
                signal_label
                if XNYS.is_session(signal_label)
                else XNYS.date_to_session(signal_label, direction="previous")
            )
            start = datetime.combine(
                date.fromisoformat(run.as_of_date) - timedelta(days=120),
                time.min,
                tzinfo=timezone.utc,
            )
            # Fetch no later than the signal session close.  Filtering future
            # bars after download would still let them affect continuity checks.
            end = XNYS.session_close(session_label).to_pydatetime()
            daily = self.candle_fetcher(
                candidate.venue_instrument, interval="1D", start_time=start, end_time=end,
            )
            hourly = self.candle_fetcher(
                candidate.venue_instrument, interval="1H", start_time=start, end_time=end,
            )
            daily_check = validate_bitget_daily(daily)
            sessions = aggregate_regular_sessions(hourly, signal_date=run.as_of_date)
            anchor = select_ep3_risk_anchor(
                sessions,
                quote=quote,
                price_precision=(candidate.venue_metadata_json or {}).get("price_precision"),
            )
            evidence = {
                "input": base_input,
                "quote": quote_payload,
                "daily_validation": daily_check,
                "aggregated_sessions": sessions,
                "anchor": anchor,
                "source_notice": "Bitget rToken USDT 执行代理证据，不是美国原生证券收盘价。",
                "exit_authority": "none; true exits use Trend Animals danger/temperature/boiling/champagne",
            }
            input_hash = sha256({**base_input, "quote": quote_payload, "daily": daily, "hourly": hourly})
            existing = repository.risk_anchor_by_input(session, candidate_id=candidate_id, input_hash=input_hash)
            if existing is not None:
                return self.payload(existing)
            quote_at = datetime.fromisoformat(str(quote_payload["quoted_at"]).replace("Z", "+00:00"))
            if quote_at.tzinfo is not None:
                quote_at = quote_at.astimezone(timezone.utc).replace(tzinfo=None)
            row = UsRiskAnchor(
                anchor_id=f"us-anchor-{candidate_id}-{uuid4().hex[:12]}",
                candidate_id=candidate_id,
                run_id=run.run_id,
                status="ready",
                signal_date=run.as_of_date,
                algorithm_version=US_RISK_ANCHOR_ALGORITHM_VERSION,
                algorithm_sha256=sha256(ALGORITHM_CONTRACT),
                contract_hash=run.base_fields_hash,
                input_hash=input_hash,
                bitget_symbol=candidate.venue_instrument,
                quote_usdt=quote,
                quote_at=quote_at,
                anchor_date=str(anchor["anchor_date"]),
                anchor_price_usdt=anchor["anchor_price"],
                anchor_distance=anchor["anchor_distance"],
                price_tick_usdt=anchor["price_tick"],
                daily_sha256=sha256(daily),
                hourly_sha256=sha256(hourly),
                evidence_json=serialize(evidence),
            )
            row.archive_path = self._archive(candidate_id, input_hash, evidence)
            saved = repository.save_risk_anchor(session, row)
            candidate.reference_price_usdt = quote
            candidate.reference_price_at = quote_at
            candidate.reference_price_source = "bitget_public_quote"
            candidate.quote_status = "available"
            repository.save_candidates(session, [candidate])
            # Saving the candidate commits the shared session and expires other ORM
            # instances. Refresh before serializing so the API never emits only the
            # three synthetic semantic fields.
            session.refresh(saved)
            return self.payload(saved)
        except UsManualError as exc:
            input_hash = sha256({**base_input, "quote": quote_payload, "daily": daily, "hourly": hourly, "error": exc.code})
            existing = repository.risk_anchor_by_input(session, candidate_id=candidate_id, input_hash=input_hash)
            if existing is not None:
                return self.payload(existing)
            evidence = {
                "input": base_input,
                "quote": quote_payload,
                "daily_sha256": sha256(daily),
                "hourly_sha256": sha256(hourly),
                "error": exc.as_payload(),
            }
            row = UsRiskAnchor(
                anchor_id=f"us-anchor-{candidate_id}-{uuid4().hex[:12]}",
                candidate_id=candidate_id, run_id=run.run_id, status="blocked",
                signal_date=run.as_of_date, algorithm_version=US_RISK_ANCHOR_ALGORITHM_VERSION,
                algorithm_sha256=sha256(ALGORITHM_CONTRACT), contract_hash=run.base_fields_hash,
                input_hash=input_hash, bitget_symbol=candidate.venue_instrument,
                daily_sha256=sha256(daily) if daily else None,
                hourly_sha256=sha256(hourly) if hourly else None,
                evidence_json=serialize(evidence), error_code=exc.code, error_message=exc.message,
            )
            row.archive_path = self._archive(candidate_id, input_hash, evidence)
            return self.payload(repository.save_risk_anchor(session, row))

    @staticmethod
    def payload(row: UsRiskAnchor) -> dict[str, Any]:
        value = repository.model_payload(row)
        value.update({
            "semantic": "前期重要低点风险锚点，只用于仓位反推，不是真实止损",
            "planning_enabled": row.status == "ready" and config.US_MANUAL_H6_MODE == "active",
            "source_mode": config.US_MANUAL_H6_MODE,
        })
        return value

    def candidate_payload(self, session: Session, candidate: Any) -> dict[str, Any]:
        payload = repository.model_payload(candidate)
        anchor = repository.latest_risk_anchor(session, int(candidate.candidate_id))
        payload["risk_anchor_status"] = anchor.status if anchor is not None else "pending"
        payload["risk_anchor"] = self.payload(anchor) if anchor is not None else None
        payload["legacy_read_only"] = repository.get_run(session, candidate.run_id).rules_version != US_MANUAL_RULES_VERSION
        return payload
