"""H5 双源止损的纯 Decimal 规则。

本模块不碰网络和数据库。Wind 只决定结构锚点和相对距离，Bitget rToken
只决定执行低点；任一来源不完整时由调用方阻断，禁止退回单源建议。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from backend.us_manual.contracts import (
    US_STOP_ALGORITHM_VERSION,
    US_STOP_DEVIATION_THRESHOLD,
    UsManualError,
    decimal_text,
)


NEW_YORK = ZoneInfo("America/New_York")
ONE_HOUR = timedelta(hours=1)
MAX_DAILY_GAP = timedelta(days=4)
MAX_DAILY_JUMP = Decimal("0.35")


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UsManualError("stop_data_invalid", f"{field} 不是有效十进制定点数", 422) from exc
    if not number.is_finite() or number <= 0:
        raise UsManualError("stop_data_invalid", f"{field} 必须为有限正数", 422)
    return number


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise UsManualError("stop_data_invalid", f"{field} 不是有效日期", 422) from exc


def _valid_ohlc(row: dict[str, Any]) -> bool:
    try:
        opened = _decimal(row.get("open"), field="open")
        high = _decimal(row.get("high"), field="high")
        low = _decimal(row.get("low"), field="low")
        close = _decimal(row.get("close"), field="close")
    except UsManualError:
        return False
    return low <= min(opened, close, high) and high >= max(opened, close, low)


def _ordered_wind_rows(rows: Iterable[dict[str, Any]], *, signal_date: date) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    seen: set[date] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise UsManualError("wind_daily_contract_error", "Wind 日线包含非对象行")
        trade_date = _date(raw.get("date"), field="Wind 日线日期")
        if trade_date > signal_date:
            # 明确丢弃并由证据记录，而不是让未来数据进入 EP3 判定。
            continue
        if trade_date in seen:
            raise UsManualError("wind_daily_contract_error", "Wind 日线包含重复交易日")
        seen.add(trade_date)
        materialized.append({**raw, "date": trade_date})
    materialized.sort(key=lambda row: row["date"])
    if not materialized:
        raise UsManualError("wind_daily_unavailable", "Wind 未返回信号日前有效日线", 409)
    return materialized[-60:]


def _confirmed_important_lows(rows: list[dict[str, Any]], *, k: int = 2) -> list[dict[str, Any]]:
    """EP3：严格五 K 摆动低点，先突破参考摆动高点才确认。"""
    lows: list[tuple[int, Decimal]] = []
    highs: list[tuple[int, Decimal]] = []
    for index in range(k, len(rows) - k):
        if not all(_valid_ohlc(row) for row in rows[index - k:index + k + 1]):
            continue
        low = _decimal(rows[index]["low"], field="Wind low")
        neighbour_lows = [
            _decimal(rows[pos]["low"], field="Wind low")
            for pos in range(index - k, index + k + 1) if pos != index
        ]
        if all(low < value for value in neighbour_lows):
            lows.append((index, low))
        high = _decimal(rows[index]["high"], field="Wind high")
        neighbour_highs = [
            _decimal(rows[pos]["high"], field="Wind high")
            for pos in range(index - k, index + k + 1) if pos != index
        ]
        if all(high > value for value in neighbour_highs):
            highs.append((index, high))

    important: list[dict[str, Any]] = []
    for low_pos, low_price in lows:
        prior_highs = [item for item in highs if item[0] < low_pos]
        later_highs = [item for item in highs if item[0] > low_pos]
        reference = prior_highs[-1] if prior_highs else (later_highs[0] if later_highs else None)
        if reference is None:
            continue
        reference_pos, reference_high = reference
        confirmed_on: date | None = None
        for position in range(low_pos + 1, len(rows)):
            row = rows[position]
            if not _valid_ohlc(row):
                break
            close = _decimal(row["close"], field="Wind close")
            if close < low_price:
                break
            # A later swing high is unknown until its own bar has completed. It
            # may be used as the reference, but never to "confirm" the low on
            # an earlier bar; that would leak future path information.
            if position > reference_pos and close > reference_high:
                confirmed_on = row["date"]
                break
        if confirmed_on is not None:
            important.append({
                "position": low_pos,
                "date": rows[low_pos]["date"],
                "low": low_price,
                "reference_high": reference_high,
                "reference_high_date": rows[reference_pos]["date"],
                "confirmed_on": confirmed_on,
            })
    return important


def select_wind_anchor(
    rows: Iterable[dict[str, Any]],
    *,
    signal_date: str | date,
    bitget_quote: Any,
) -> dict[str, Any]:
    """返回信号日低点，或仅在主锚点无效时回退到最近确认 EP3 低点。"""
    signal = _date(signal_date, field="signal_date")
    quote = _decimal(bitget_quote, field="Bitget quote")
    ordered = _ordered_wind_rows(rows, signal_date=signal)
    signal_rows = [row for row in ordered if row["date"] == signal]
    if len(signal_rows) != 1:
        raise UsManualError("wind_signal_day_missing", "Wind 日线缺少唯一的信号日记录", 409)
    signal_row = signal_rows[0]
    signal_close = _decimal(signal_row.get("close"), field="Wind signal close")

    primary_reason: str | None = None
    if _valid_ohlc(signal_row):
        signal_low = _decimal(signal_row["low"], field="Wind signal low")
        primary_ratio = signal_low / signal_close
        if quote * primary_ratio < quote:
            return {
                "anchor_type": "signal_day_low",
                "anchor_date": signal,
                "anchor_low": signal_low,
                "signal_close": signal_close,
                "wind_ratio": primary_ratio,
                "wind_mapped_stop": quote * primary_ratio,
                "fallback_reason": None,
                "ep3": None,
                "bar_count": len(ordered),
            }
        primary_reason = "signal_day_mapped_stop_not_below_quote"
    else:
        primary_reason = "signal_day_ohlc_invalid"

    important = _confirmed_important_lows(ordered, k=2)
    for candidate in reversed(important):
        ratio = candidate["low"] / signal_close
        mapped = quote * ratio
        if mapped < quote:
            return {
                "anchor_type": "confirmed_swing_low",
                "anchor_date": candidate["date"],
                "anchor_low": candidate["low"],
                "signal_close": signal_close,
                "wind_ratio": ratio,
                "wind_mapped_stop": mapped,
                "fallback_reason": primary_reason,
                "ep3": candidate,
                "bar_count": len(ordered),
            }
    raise UsManualError(
        "wind_anchor_unavailable",
        "信号日低点无效，且信号日前没有可确认的重要低点",
        409,
        {"fallback_reason": primary_reason},
    )


def validate_bitget_daily(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """只验证连续性与异常跳变；绝不把 rToken 1D 时间戳解释成美股交易日。"""
    materialized = list(rows)
    if any(not isinstance(row, dict) for row in materialized):
        raise UsManualError("bitget_candles_contract_error", "Bitget 1D K 线包含非对象行")
    if any(not isinstance(row.get("timestamp"), datetime) for row in materialized):
        raise UsManualError("bitget_candles_contract_error", "Bitget 1D K 线时间戳无效")
    materialized.sort(key=lambda row: row["timestamp"])
    if len(materialized) < 5:
        raise UsManualError("bitget_daily_incomplete", "Bitget 1D K 线少于 5 根，无法验证连续性", 409)
    timestamps: list[datetime] = []
    previous_close: Decimal | None = None
    largest_jump = Decimal("0")
    for row in materialized:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise UsManualError("bitget_candles_contract_error", "Bitget 1D K 线时间戳无时区")
        if timestamps and timestamp <= timestamps[-1]:
            raise UsManualError("bitget_daily_incomplete", "Bitget 1D K 线时间戳重复或倒序", 409)
        if timestamps and timestamp - timestamps[-1] > MAX_DAILY_GAP:
            raise UsManualError("bitget_daily_incomplete", "Bitget 1D K 线存在超过四天的断档", 409)
        if not _valid_ohlc(row):
            raise UsManualError("bitget_candles_contract_error", "Bitget 1D K 线 OHLC 无效")
        opened = _decimal(row["open"], field="Bitget daily open")
        close = _decimal(row["close"], field="Bitget daily close")
        if previous_close is not None:
            jump = max(abs(opened / previous_close - Decimal("1")),
                       abs(close / previous_close - Decimal("1")))
            largest_jump = max(largest_jump, jump)
            if jump > MAX_DAILY_JUMP:
                raise UsManualError(
                    "bitget_daily_abnormal_jump",
                    "Bitget 1D K 线出现超过 35% 的异常跳变，可能涉及拆股或数据断层",
                    409,
                    {"jump_ratio": decimal_text(jump)},
                )
        previous_close = close
        timestamps.append(timestamp)
    return {
        "bar_count": len(materialized),
        "first_timestamp": timestamps[0],
        "last_timestamp": timestamps[-1],
        "largest_jump_ratio": largest_jump,
        "timestamp_interpretation": "rtoken_bucket_only_not_us_trading_date",
    }


def bitget_regular_session_anchor(
    rows: Iterable[dict[str, Any]],
    *,
    anchor_date: str | date,
) -> dict[str, Any]:
    """按纽约 09:30–16:00 对齐 1H 桶；要求桶的并集完整覆盖常规时段。"""
    target = _date(anchor_date, field="anchor_date")
    session_start = datetime.combine(target, time(9, 30), tzinfo=NEW_YORK)
    session_end = datetime.combine(target, time(16, 0), tzinfo=NEW_YORK)
    selected: list[dict[str, Any]] = []
    for row in rows:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise UsManualError("bitget_candles_contract_error", "Bitget 1H K 线时间戳无时区")
        local_start = timestamp.astimezone(NEW_YORK)
        local_end = local_start + ONE_HOUR
        if local_start < session_end and local_end > session_start:
            if not _valid_ohlc(row):
                raise UsManualError("bitget_candles_contract_error", "Bitget 1H K 线 OHLC 无效")
            selected.append({**row, "local_start": local_start, "local_end": local_end})
    selected.sort(key=lambda row: row["local_start"])
    if not selected:
        raise UsManualError("bitget_hourly_session_missing", "Bitget 1H K 线未覆盖锚点日常规交易时段", 409)

    cursor = session_start
    for row in selected:
        start = max(row["local_start"], session_start)
        end = min(row["local_end"], session_end)
        if start > cursor:
            raise UsManualError("bitget_hourly_session_incomplete", "Bitget 1H K 线常规时段存在缺口", 409)
        if end > cursor:
            cursor = end
    if cursor < session_end:
        raise UsManualError("bitget_hourly_session_incomplete", "Bitget 1H K 线未完整覆盖至 16:00", 409)
    low = min(_decimal(row["low"], field="Bitget hourly low") for row in selected)
    return {
        "anchor_low": low,
        "anchor_date": target,
        "session_timezone": "America/New_York",
        "session_start": session_start,
        "session_end": session_end,
        "bar_count": len(selected),
        "bar_timestamps": [row["timestamp"] for row in selected],
    }


def price_tick(price_precision: Any) -> Decimal:
    if isinstance(price_precision, bool):
        raise UsManualError("price_precision_invalid", "Bitget 产品价格精度无效", 422)
    try:
        precision = int(str(price_precision))
    except (TypeError, ValueError) as exc:
        raise UsManualError("price_precision_invalid", "Bitget 产品价格精度无效", 422) from exc
    if precision < 0 or precision > 12:
        raise UsManualError("price_precision_invalid", "Bitget 产品价格精度超出 0–12", 422)
    return Decimal("1").scaleb(-precision)


def buffered_stop(value: Any, *, price_precision: Any) -> Decimal:
    raw = _decimal(value, field="stop basis")
    tick = price_tick(price_precision)
    rounded = raw.quantize(tick, rounding=ROUND_DOWN)
    result = rounded - tick
    if result <= 0:
        raise UsManualError("stop_price_invalid", "止损建议减一档后不再为正数", 422)
    return result


def custom_review_stop(value: Any, *, price_precision: Any) -> Decimal:
    """人工自定义价只按产品精度向下量化，不额外改变用户明确输入。"""
    raw = _decimal(value, field="custom_stop_usdt")
    return raw.quantize(price_tick(price_precision), rounding=ROUND_DOWN)


def dual_source_decision(
    *,
    wind_anchor: dict[str, Any],
    bitget_anchor_low: Any,
    bitget_quote: Any,
    price_precision: Any,
    threshold: Decimal = US_STOP_DEVIATION_THRESHOLD,
) -> dict[str, Any]:
    mapped = _decimal(wind_anchor.get("wind_mapped_stop"), field="wind_mapped_stop")
    execution_low = _decimal(bitget_anchor_low, field="bitget_anchor_low")
    quote = _decimal(bitget_quote, field="bitget_quote")
    deviation = abs(execution_low - mapped) / mapped
    status = "auto_ready" if deviation <= threshold else "review_required"
    suggestion = buffered_stop(execution_low, price_precision=price_precision) if status == "auto_ready" else None
    if suggestion is not None and suggestion >= quote:
        raise UsManualError("stop_not_below_quote", "自动止损建议不低于 Bitget 当前报价", 409)
    return {
        "status": status,
        "algorithm_version": US_STOP_ALGORITHM_VERSION,
        "wind_mapped_stop": mapped,
        "bitget_anchor_low": execution_low,
        "bitget_quote": quote,
        "deviation_ratio": deviation,
        "deviation_threshold": threshold,
        "suggested_stop": suggestion,
        "price_tick": price_tick(price_precision),
    }
