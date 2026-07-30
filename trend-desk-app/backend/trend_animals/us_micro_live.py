"""美股小额实盘验证的只读筛选与仓位反推。

此模块只处理趋势动物信号、缓存计划与人工下单前的金额/数量反推：

* 不持有交易所凭证；
* 不调用交易所下单接口；
* ``daysSinceTrendEntry`` 按实时字段说明视为右侧状态的自然日年龄，
  不能冒充温转热发生后的交易日数。

1000 USDT 只是研究账户的风险标尺，不是任何标的的买入建议。
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_DOWN
from typing import Any

from backend.trend_animals.billing import estimate_snapshot_cost
from backend.trend_animals.errors import TrendAnimalsError


US_MICRO_LIVE_VERSION = "us-micro-live-h1"
US_MICRO_LIVE_SCOPE = "trend_animals_us_bitget_manual_micro_live_h1"
SIGNAL_FIELDS = [
    "trendTemperatureCurr",
    "trendTemperaturePrev",
    "daysSinceTrendEntry",
]
MAX_SNAPSHOT_BATCH = 300

# 这些是小额验证的账户约束，和 A 股纪律 v1.5 保持隔离。
DEFAULT_ACCOUNT_USDT = 1000.0
DEFAULT_RISK_PCT = 0.0025
DEFAULT_MAX_SINGLE_NOTIONAL_USDT = 100.0
DEFAULT_MIN_SINGLE_NOTIONAL_USDT = 25.0
DEFAULT_MAX_OPEN_POSITIONS = 2
DEFAULT_MAX_TOTAL_NOTIONAL_USDT = 200.0
DEFAULT_ENTRY_MAX_RIGHT_SIDE_DAYS = 10


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _batches(values: list[int], size: int = MAX_SNAPSHOT_BATCH) -> list[list[int]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def bitget_us_stock_universe(pool: dict) -> list[dict]:
    """从公开交集存档取出 Bitget 的美股个股，ETF 和非严格匹配一律不混入。"""
    rows: list[dict] = []
    seen_tm_ids: set[int] = set()
    for row in pool.get("candidates") or []:
        if not isinstance(row, dict) or row.get("root_asset") != "美股":
            continue
        tm_id = _int(row.get("tmId"))
        symbol = _text(row.get("tickerSymbol")).upper()
        venues = row.get("venues") or []
        bitget = next((venue for venue in venues if isinstance(venue, dict)
                       and venue.get("venue") == "bitget"
                       and venue.get("match_method") == "exact"), None)
        if tm_id is None or tm_id <= 0 or not symbol or bitget is None:
            continue
        if tm_id in seen_tm_ids:
            raise TrendAnimalsError("api_contract_error", f"公开池存在重复 tmId={tm_id}")
        seen_tm_ids.add(tm_id)
        rows.append({
            "tmId": tm_id,
            "tickerSymbol": symbol,
            "tickerName": row.get("tickerName"),
            "asset": row.get("asset"),
            "root_asset": row.get("root_asset"),
            "asOfDate": row.get("asOfDate"),
            "venue": {
                "venue": "bitget",
                "venue_instrument": bitget.get("venue_instrument"),
                "quote_coin": bitget.get("quote_coin"),
                "quantity_precision": bitget.get("quantity_precision"),
                "price_precision": bitget.get("price_precision"),
                "min_trade_usdt": bitget.get("min_trade_usdt"),
            },
        })
    return sorted(rows, key=lambda item: (item["tickerSymbol"], item["tmId"]))


def build_signal_scan_plan(*, pool: dict, billing: list[dict], expected_as_of_date: str) -> dict:
    """构造最小字段、最多 300 行一批的计费计划，但绝不发起快照请求。"""
    universe = bitget_us_stock_universe(pool)
    if not expected_as_of_date:
        raise TrendAnimalsError("data_stale", "美股更新状态缺少 asOfDate")
    stale = [row["tickerSymbol"] for row in universe if row.get("asOfDate") != expected_as_of_date]
    if stale:
        raise TrendAnimalsError(
            "data_stale",
            f"公开池日期与趋势动物美股日期不一致；需先刷新覆盖池（示例：{stale[:5]}）",
        )
    tm_ids = [row["tmId"] for row in universe]
    batches = _batches(tm_ids)
    estimated_parts = [estimate_snapshot_cost(SIGNAL_FIELDS, len(batch), billing) for batch in batches]
    return {
        "schema_version": 1,
        "scope": US_MICRO_LIVE_SCOPE,
        "rules_version": US_MICRO_LIVE_VERSION,
        "as_of_date": expected_as_of_date,
        "universe_count": len(universe),
        "requested_fields": list(SIGNAL_FIELDS),
        "batches": [{"tm_ids": batch, "row_count": len(batch), "estimated_cost_cny": cost}
                    for batch, cost in zip(batches, estimated_parts)],
        "estimated_cost_cny": round(sum(estimated_parts), 6),
        "field_saving_note": (
            "第一阶段只读取温度当前/前期和右侧自然日数；价格、成交额、行业温度、强度"
            "只允许在温转热候选的第二阶段按实际候选数再取。"
        ),
        "execution_guard": (
            "仅生成手工复核对象；不代表账户可交易资格，不创建订单，不调用 Bitget 下单接口。"
        ),
    }


def right_side_age_bucket(days: int | None, *, entry_max_days: int = DEFAULT_ENTRY_MAX_RIGHT_SIDE_DAYS) -> str:
    """以自然日口径展示年龄，不把周末错误压缩成交易日。"""
    if days is None or days < 1:
        return "missing_or_not_right_side"
    if days <= 3:
        return "1-3"
    if days <= 5:
        return "4-5"
    if days <= entry_max_days:
        return f"6-{entry_max_days}"
    return f">{entry_max_days}"


def classify_signal_rows(*, pool: dict, snapshot_rows: list[dict], expected_as_of_date: str,
                         entry_max_days: int = DEFAULT_ENTRY_MAX_RIGHT_SIDE_DAYS) -> dict:
    """合并公开池和最小快照，保留所有不通过原因以便观察筛选宽松程度。"""
    if entry_max_days < 1:
        raise ValueError("entry_max_days 必须大于等于 1")
    universe = bitget_us_stock_universe(pool)
    snapshots: dict[int, dict] = {}
    for raw in snapshot_rows:
        if not isinstance(raw, dict):
            continue
        tm_id = _int(raw.get("tmId"))
        if tm_id is None:
            continue
        if raw.get("asOfDate") != expected_as_of_date:
            raise TrendAnimalsError(
                "data_stale", f"tmId={tm_id} 快照日期 {raw.get('asOfDate')}，期望 {expected_as_of_date}")
        if tm_id in snapshots:
            raise TrendAnimalsError("api_contract_error", f"快照返回重复 tmId={tm_id}")
        snapshots[tm_id] = raw

    candidates: list[dict] = []
    counts: Counter[str] = Counter()
    age_buckets: Counter[str] = Counter()
    for item in universe:
        raw = snapshots.get(item["tmId"])
        result = dict(item)
        if raw is None:
            result.update({
                "screen_status": "data_incomplete",
                "screen_reason": "signal_snapshot_missing",
                "manual_order_required": True,
            })
            counts[result["screen_status"]] += 1
            candidates.append(result)
            continue
        prev = raw.get("trendTemperaturePrev")
        curr = raw.get("trendTemperatureCurr")
        days = _int(raw.get("daysSinceTrendEntry"))
        bucket = right_side_age_bucket(days, entry_max_days=entry_max_days)
        warm_to_hot = prev == "温" and curr == "热"
        result.update({
            "temperature_prev": prev,
            "temperature_curr": curr,
            "days_since_trend_entry": days,
            "right_side_age_bucket": bucket,
            "warm_to_hot": warm_to_hot,
            "manual_order_required": True,
        })
        if not warm_to_hot:
            result.update({"screen_status": "observe", "screen_reason": "not_warm_to_hot"})
        elif days is None or days < 1:
            result.update({"screen_status": "data_incomplete", "screen_reason": "right_side_age_missing"})
        elif days > entry_max_days:
            result.update({"screen_status": "observe", "screen_reason": "right_side_age_exceeded"})
        else:
            result.update({
                "screen_status": "screened",
                "screen_reason": "warm_to_hot_within_right_side_age_window",
            })
        counts[result["screen_status"]] += 1
        if warm_to_hot:
            age_buckets[bucket] += 1
        candidates.append(result)

    screened = [row for row in candidates if row["screen_status"] == "screened"]
    return {
        "schema_version": 1,
        "scope": US_MICRO_LIVE_SCOPE,
        "rules_version": US_MICRO_LIVE_VERSION,
        "as_of_date": expected_as_of_date,
        "entry_max_right_side_calendar_days": entry_max_days,
        "right_side_age_field_note": (
            "daysSinceTrendEntry 是右侧状态开始后的自然日数（含非交易日），"
            "不是温转热发生后的交易日数，也不是持仓天数。"
        ),
        "universe_count": len(universe),
        "returned_snapshot_count": len(snapshots),
        "screen_status_counts": dict(sorted(counts.items())),
        "warm_to_hot_right_side_age_buckets": dict(sorted(age_buckets.items())),
        "screened_count": len(screened),
        "screened_candidates": screened,
        "all_candidates": candidates,
        "execution_guard": (
            "screened 只表示基础信号通过；仍须补齐价格、结构止损、行业相关性和账户产品资格，"
            "并由用户在 Bitget 手工确认后执行。"
        ),
    }


def micro_live_policy(*, account_usdt: float = DEFAULT_ACCOUNT_USDT,
                      risk_pct: float = DEFAULT_RISK_PCT,
                      max_single_notional_usdt: float = DEFAULT_MAX_SINGLE_NOTIONAL_USDT,
                      min_single_notional_usdt: float = DEFAULT_MIN_SINGLE_NOTIONAL_USDT,
                      max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
                      max_total_notional_usdt: float = DEFAULT_MAX_TOTAL_NOTIONAL_USDT) -> dict:
    """返回可审计的小额实盘风险边界。"""
    if account_usdt <= 0 or risk_pct <= 0 or max_single_notional_usdt <= 0:
        raise ValueError("账户权益、单笔风险和单票上限必须为正数")
    if min_single_notional_usdt <= 0 or min_single_notional_usdt > max_single_notional_usdt:
        raise ValueError("最小名义金额必须大于 0 且不超过单票上限")
    if max_open_positions < 1 or max_total_notional_usdt < max_single_notional_usdt:
        raise ValueError("组合容量配置不一致")
    return {
        "account_usdt": float(account_usdt),
        "risk_pct_per_trade": float(risk_pct),
        "risk_budget_usdt_per_trade": round(account_usdt * risk_pct, 6),
        "max_single_notional_usdt": float(max_single_notional_usdt),
        "min_single_notional_usdt": float(min_single_notional_usdt),
        "max_open_positions": int(max_open_positions),
        "max_total_notional_usdt": float(max_total_notional_usdt),
        "manual_only": True,
    }


def _floor_to_precision(value: Decimal, precision: int) -> Decimal:
    quantum = Decimal("1").scaleb(-precision)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def size_manual_micro_live_position(*, entry_price: float, stop_price: float,
                                    quantity_precision: int | str | None = None,
                                    policy: dict | None = None) -> dict:
    """按 1000U 风险预算反推一笔手工订单的金额和散股数量。

    ``entry_price`` 必须是用户下单前在交易所看到的可成交参考价，``stop_price``
    是已经写入计划的结构止损；缺任一项不输出买入数量。
    """
    supplied = policy or {}
    policy = micro_live_policy(
        account_usdt=float(supplied.get("account_usdt", DEFAULT_ACCOUNT_USDT)),
        risk_pct=float(supplied.get("risk_pct", supplied.get("risk_pct_per_trade", DEFAULT_RISK_PCT))),
        max_single_notional_usdt=float(supplied.get(
            "max_single_notional_usdt", DEFAULT_MAX_SINGLE_NOTIONAL_USDT)),
        min_single_notional_usdt=float(supplied.get(
            "min_single_notional_usdt", DEFAULT_MIN_SINGLE_NOTIONAL_USDT)),
        max_open_positions=int(supplied.get("max_open_positions", DEFAULT_MAX_OPEN_POSITIONS)),
        max_total_notional_usdt=float(supplied.get(
            "max_total_notional_usdt", DEFAULT_MAX_TOTAL_NOTIONAL_USDT)),
    )
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_price))
    if entry <= 0:
        return {"verdict": "no_entry", "reason": "entry_price_missing_or_invalid", "policy": policy}
    if stop <= 0 or stop >= entry:
        return {"verdict": "no_entry", "reason": "stop_price_missing_or_invalid", "policy": policy}
    distance = (entry - stop) / entry
    risk_budget = Decimal(str(policy["risk_budget_usdt_per_trade"]))
    target = risk_budget / distance
    max_single = Decimal(str(policy["max_single_notional_usdt"]))
    min_single = Decimal(str(policy["min_single_notional_usdt"]))
    notional = min(target, max_single)
    if notional < min_single:
        return {
            "verdict": "watch",
            "reason": "stop_distance_too_far_for_minimum_notional",
            "entry_price": float(entry),
            "stop_price": float(stop),
            "stop_distance_pct": float(distance),
            "target_notional_usdt": float(notional),
            "policy": policy,
        }

    raw_quantity = notional / entry
    precision = _int(quantity_precision)
    if precision is not None and precision >= 0:
        quantity = _floor_to_precision(raw_quantity, precision)
    else:
        quantity = raw_quantity
    actual_notional = quantity * entry
    if quantity <= 0 or actual_notional < min_single:
        return {
            "verdict": "watch",
            "reason": "venue_quantity_precision_reduces_notional_below_policy_floor",
            "entry_price": float(entry),
            "stop_price": float(stop),
            "stop_distance_pct": float(distance),
            "target_notional_usdt": float(notional),
            "policy": policy,
        }
    max_loss = actual_notional * distance
    return {
        "verdict": "manual_review",
        "reason": "risk_sized_manual_order_only",
        "entry_price": float(entry),
        "stop_price": float(stop),
        "stop_distance_pct": float(distance),
        "target_notional_usdt": float(notional),
        "planned_notional_usdt": float(actual_notional),
        "planned_quantity": format(quantity, "f"),
        "estimated_max_loss_usdt": float(max_loss),
        "policy": policy,
        "execution_guard": (
            "仅供用户在交易所手工核对价格、最小交易额、产品资格和订单类型；本模块从不提交订单。"
        ),
    }
