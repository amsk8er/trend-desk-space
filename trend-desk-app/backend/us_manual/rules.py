"""H6 美股与美国 ETF 候选排序、环境容量及 Decimal 仓位规则。

这里是实验性、可审计的执行规则，不是收益预测。阈值或排序变更必须升级
``US_MANUAL_RULES_VERSION`` 并生成新计划。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from backend.trend_animals.us_micro_live import (
    DEFAULT_ACCOUNT_USDT,
    DEFAULT_MAX_OPEN_POSITIONS,
    DEFAULT_MAX_SINGLE_NOTIONAL_USDT,
    DEFAULT_MAX_TOTAL_NOTIONAL_USDT,
    DEFAULT_MIN_SINGLE_NOTIONAL_USDT,
    DEFAULT_RISK_PCT,
)
from backend.us_manual.contracts import UsManualError, decimal_text, parse_decimal, sha256


H5_POLICY = {
    "account_usdt": Decimal(str(DEFAULT_ACCOUNT_USDT)),
    "risk_pct_per_trade": Decimal(str(DEFAULT_RISK_PCT)),
    "risk_budget_usdt_per_trade": Decimal(str(DEFAULT_ACCOUNT_USDT)) * Decimal(str(DEFAULT_RISK_PCT)),
    "min_single_notional_usdt": Decimal(str(DEFAULT_MIN_SINGLE_NOTIONAL_USDT)),
    "max_single_notional_usdt": Decimal(str(DEFAULT_MAX_SINGLE_NOTIONAL_USDT)),
    "max_open_positions": DEFAULT_MAX_OPEN_POSITIONS,
    "max_total_notional_usdt": Decimal(str(DEFAULT_MAX_TOTAL_NOTIONAL_USDT)),
    "entry_max_right_side_calendar_days": 9,
    "minimum_relative_strength": Decimal("90"),
}

POLICY = {
    "account_usdt": Decimal("1000"),
    "risk_pct_per_trade": Decimal("0.0025"),
    "risk_budget_usdt_per_trade": Decimal("2.5"),
    "min_single_notional_usdt": Decimal("25"),
    "target_single_notional_usdt": Decimal("50"),
    "max_open_positions": 20,
    "max_total_notional_usdt": Decimal("1000"),
    "entry_max_right_side_calendar_days": 9,
    "minimum_relative_strength": Decimal("90"),
    "environment_factors": {
        "温": Decimal("1"), "热": Decimal("1"), "沸": Decimal("1"),
        "平": Decimal("0.5"), "凉": Decimal("0.25"),
        "寒": Decimal("0"), "冻": Decimal("0"),
    },
}


def rules_hash() -> str:
    return sha256(policy_payload())


def environment_factor(temperature: Any) -> Decimal:
    value = str(temperature or "").strip()
    factors = POLICY["environment_factors"]
    if value not in factors:
        raise UsManualError("market_environment_missing", "美股整体温度缺失或无法识别，今日不开新仓", 409)
    return factors[value]


def _precision(value: Any) -> int:
    try:
        precision = int(str(value))
    except (TypeError, ValueError):
        raise UsManualError("venue_precision_missing", "Bitget 产品缺少数量精度，不能计算散股数量", 422)
    if precision < 0 or precision > 12:
        raise UsManualError("venue_precision_invalid", "Bitget 产品数量精度无效", 422)
    return precision


def _floor_precision(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-precision), rounding=ROUND_DOWN)


def sizing_preview(*, entry_price: Any, stop_price: Any, quantity_precision: Any,
                   min_trade_usdt: Any | None = None, open_position_count: int = 0,
                   open_notional_usdt: Any = Decimal("0")) -> dict[str, Any]:
    """按 2.5U 风险预算反推散股数，所有输出金额/数量保持 Decimal 字符串。"""
    entry = parse_decimal(entry_price, field="entry_price", positive=True)
    stop = parse_decimal(stop_price, field="stop_price", positive=True)
    if stop >= entry:
        raise UsManualError("stop_price_invalid", "止损价必须低于参考入场价", 422)
    precision = _precision(quantity_precision)
    current_open = parse_decimal(open_notional_usdt, field="open_notional_usdt", non_negative=True)
    if open_position_count >= int(H5_POLICY["max_open_positions"]):
        return _blocked("position_capacity_full", "当前持仓已达两只上限", entry, stop)
    distance = (entry - stop) / entry
    risk_notional = H5_POLICY["risk_budget_usdt_per_trade"] / distance
    target = min(risk_notional, H5_POLICY["max_single_notional_usdt"])
    min_trade = parse_decimal(min_trade_usdt, field="min_trade_usdt", non_negative=True) if min_trade_usdt is not None else Decimal("0")
    required_minimum = max(H5_POLICY["min_single_notional_usdt"], min_trade)
    if target < required_minimum:
        return _blocked(
            "stop_distance_too_far_for_minimum_notional",
            "止损距离导致风险反推金额低于最低交易额，转观察而非强行下单",
            entry, stop, distance=distance, target=target,
        )
    quantity = _floor_precision(target / entry, precision)
    notional = quantity * entry
    if quantity <= 0 or notional < required_minimum:
        return _blocked(
            "venue_quantity_precision_reduces_notional_below_policy_floor",
            "Bitget 数量精度向下取整后低于最低交易额",
            entry, stop, distance=distance, target=target,
        )
    if current_open + notional > H5_POLICY["max_total_notional_usdt"]:
        return _blocked(
            "total_notional_capacity_exceeded",
            "与现有持仓合计超过 200 USDT 初始名义金额上限",
            entry, stop, distance=distance, target=target,
            extra={"open_notional_usdt": decimal_text(current_open)},
        )
    return {
        "verdict": "manual_review",
        "reason": "risk_sized_manual_order_only",
        "entry_price": decimal_text(entry),
        "stop_price": decimal_text(stop),
        "stop_distance": decimal_text(distance),
        "stop_distance_pct": decimal_text(distance * Decimal("100")),
        "risk_budget_usdt": decimal_text(H5_POLICY["risk_budget_usdt_per_trade"]),
        "target_notional_usdt": decimal_text(target),
        "planned_notional_usdt": decimal_text(notional),
        "planned_quantity": decimal_text(quantity),
        "estimated_max_loss_usdt": decimal_text(notional * distance),
        "quantity_precision": precision,
        "policy": legacy_policy_payload(),
        "execution_guard": "只供手工核对 Bitget 产品、报价和订单；Trend Desk 不会提交订单。",
    }


def _blocked(code: str, message: str, entry: Decimal, stop: Decimal, *,
             distance: Decimal | None = None, target: Decimal | None = None,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "verdict": "watch",
        "reason": code,
        "message": message,
        "entry_price": decimal_text(entry),
        "stop_price": decimal_text(stop),
        "policy": legacy_policy_payload(),
    }
    if distance is not None:
        result["stop_distance"] = decimal_text(distance)
        result["stop_distance_pct"] = decimal_text(distance * Decimal("100"))
    if target is not None:
        result["target_notional_usdt"] = decimal_text(target)
    if extra:
        result.update(extra)
    return result


def policy_payload() -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return decimal_text(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    return {key: convert(value) for key, value in POLICY.items()}


def legacy_policy_payload() -> dict[str, Any]:
    return {
        key: decimal_text(value) if isinstance(value, Decimal) else value
        for key, value in H5_POLICY.items()
    }


def stable_rank_key(row: Any) -> tuple[Any, ...]:
    """相对强度优先，再按右侧天数/流动性/规模/代码稳定打破平局。"""
    days = row.right_side_calendar_days if row.right_side_calendar_days is not None else 10_000
    strength = row.strength_local if row.strength_local is not None else Decimal("-999999")
    amount = row.amount_1d if row.amount_1d is not None else Decimal("-999999")
    market_cap = row.market_cap if row.market_cap is not None else Decimal("-999999")
    return (-strength, days, -amount, -market_cap, row.ticker_symbol, row.tm_id)


def rank_ready_candidates(rows: list[Any]) -> tuple[list[Any], list[Any]]:
    """执行字段完整性与强度纪律；同行业不再淘汰候选。"""
    ready = [
        row for row in rows
        if row.asset_type in {"stock", "etf"} and row.screen_status == "screened"
    ]
    ready.sort(key=stable_rank_key)
    selected: list[Any] = []
    for order, row in enumerate(ready, 1):
        row.rank = order
        required = ((row.strength_local,) if row.asset_type == "etf" else (
            row.strength_local,
            row.amount_1d,
            row.market_cap,
            row.industry_tm_id,
            row.industry_name,
            row.industry_temperature_curr,
            row.industry_strength_local,
        ))
        if any(value is None for value in required):
            row.screen_status = "data_incomplete"
            row.primary_reason = "enrichment_required_fields_missing"
            row.all_reasons = ["enrichment_required_fields_missing"]
            continue
        if row.strength_local < POLICY["minimum_relative_strength"]:
            row.screen_status = "observe"
            row.primary_reason = "relative_strength_below_90"
            row.all_reasons = ["relative_strength_below_90"]
            continue
        row.screen_status = "ready"
        row.primary_reason = "ready_after_h6_selection_disciplines"
        row.all_reasons = [
            "right_side_age",
            "etf_warm_to_hot_membership" if row.asset_type == "etf" else "sector_temperature_warm_plus",
            "relative_strength_at_least_90",
            "quality_complete",
        ]
        selected.append(row)
    return selected, ready


def rank_observation_candidates(rows: list[Any]) -> list[Any]:
    """H6 common-pool watchlist ordered by local relative strength."""
    observations = [
        row for row in rows
        if row.asset_type in {"stock", "etf"} and row.screen_status == "ready"
    ]
    observations.sort(key=stable_rank_key)
    for order, row in enumerate(observations, 1):
        row.observation_rank = order
    return observations
