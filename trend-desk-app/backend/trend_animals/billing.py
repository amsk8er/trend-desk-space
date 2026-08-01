"""按实时字段计费表估算趋势动物 API 费用。"""
from __future__ import annotations

import re

from backend.trend_animals.errors import TrendAnimalsError


def billing_map(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        name = row.get("columnName")
        cost = row.get("priceCost")
        if name is not None and isinstance(cost, (int, float)):
            out[str(name)] = float(cost)
    return out


def estimate_snapshot_cost(fields: list[str], row_count: int, rows: list[dict]) -> float:
    if row_count < 0:
        raise TrendAnimalsError("api_contract_error", "返回行数不能为负")
    if row_count > 300:
        raise TrendAnimalsError(
            "unsupported_row_count", "超过 300 行后的快照计费规则文档未提供")
    prices = billing_map(rows)
    missing = [field for field in fields if field not in prices]
    if missing:
        raise TrendAnimalsError(
            "missing_required_fields", f"实时计费表缺少字段：{','.join(missing)}")
    per_row = sum(prices[field] for field in fields)
    first = min(row_count, 20)
    second = min(max(row_count - 20, 0), 80)
    third = min(max(row_count - 100, 0), 200)
    return round(per_row * (first + second * 0.8 + third * 0.6), 6)


def endpoint_fixed_cost(api_docs: list[dict], api_name: str) -> float:
    row = next((r for r in api_docs if r.get("ApiName") == api_name), None)
    if row is None:
        raise TrendAnimalsError("api_contract_error", f"实时接口文档缺少 {api_name}")
    model = str(row.get("billingModel") or "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元/次", model)
    if not match:
        if "免费" in model:
            return 0.0
        raise TrendAnimalsError("api_contract_error", f"无法解析 {api_name} 实时计费规则")
    return float(match.group(1))


def component_pricing(api_docs: list[dict]) -> tuple[float, float, float]:
    """返回 (基础次费, 普通行费, 组合榜单行费)，解析失败就阻塞而不是沿用旧价格。"""
    row = next((r for r in api_docs if r.get("ApiName") == "getComponentTicker"), None)
    if row is None:
        raise TrendAnimalsError("api_contract_error", "实时接口文档缺少 getComponentTicker")
    model = str(row.get("billingModel") or "")
    base = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元/次", model)
    normal = re.search(r"\+\s*([0-9]+(?:\.[0-9]+)?)\s*元/行", model)
    combo = re.search(r"组合榜单成分\s*([0-9]+(?:\.[0-9]+)?)\s*元/行", model)
    if not (base and normal and combo):
        raise TrendAnimalsError("api_contract_error", "无法解析 getComponentTicker 实时计费规则")
    return float(base.group(1)), float(normal.group(1)), float(combo.group(1))


def estimate_component_cost(row_count: int, *, combo: bool = True,
                            base_cost: float = 0.1, normal_row_cost: float = 0.001,
                            combo_row_cost: float = 0.005) -> float:
    if row_count < 0:
        raise TrendAnimalsError("api_contract_error", "成分行数不能为负")
    return round(base_cost + row_count * (combo_row_cost if combo else normal_row_cost), 6)


def ensure_budget(estimated_cost: float, approved_budget: float | None) -> None:
    from backend.trend_animals.errors import BudgetConfirmationRequired
    # 用户显式给出的预算就是预先批准；没有预算时遵守 Agent 指南的 1 元确认线。
    if estimated_cost >= 1.0 and approved_budget is None:
        raise BudgetConfirmationRequired(estimated_cost, approved_budget)
    if approved_budget is not None and estimated_cost > approved_budget:
        raise BudgetConfirmationRequired(estimated_cost, approved_budget)
