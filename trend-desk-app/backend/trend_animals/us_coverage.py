"""趋势动物美股覆盖盘点的最小、可审计查询规则。

此模块只定义“趋势动物当前直接提供了哪些美股/美国 ETF 成分”，不把它
误当作全量美股，也不读取温度、行情或基本面字段。调用方先用免费
``getUpdateStatus`` 取得两个根节点，再以一笔最小快照读取成分数。注意：
实时文档没有承诺 ``constituentCount`` 等于 ``getAllBasicComponentsFlag=1``
的基础子级展开行数；因此该计数只能描述直接层级，不能作为全量展开的预算上限。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from backend.trend_animals.billing import (
    component_pricing,
    estimate_component_cost,
    estimate_snapshot_cost,
)
from backend.trend_animals.errors import TrendAnimalsError


US_ROOT_ASSETS = ("美股", "美国ETF")
US_COVERAGE_SCOPE = "trend_animals_us_direct_components_v1"
COUNT_FIELDS = [
    "tmId", "tickerName", "tickerSymbol", "asset", "assetCategory", "asOfDate",
    "constituentCount",
]


def _int_tm_id(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise TrendAnimalsError("api_contract_error", f"{context} 的 tmId 非法")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TrendAnimalsError("api_contract_error", f"{context} 缺少有效 tmId") from exc
    if result <= 0:
        raise TrendAnimalsError("api_contract_error", f"{context} 的 tmId 非法")
    return result


def select_us_roots(status_rows: list[dict]) -> list[dict]:
    """从免费更新状态中精确选择美股和美国 ETF 的直接根节点。"""
    roots: list[dict] = []
    for asset in US_ROOT_ASSETS:
        matches = [row for row in status_rows if isinstance(row, dict) and row.get("asset") == asset]
        if len(matches) != 1:
            raise TrendAnimalsError(
                "api_contract_error", f"更新状态中的 {asset} 根节点应唯一，实际 {len(matches)}")
        row = matches[0]
        as_of_date = row.get("asOfDate")
        if not isinstance(as_of_date, str) or not as_of_date:
            raise TrendAnimalsError("api_contract_error", f"更新状态中的 {asset} 缺少 asOfDate")
        roots.append({
            "asset": asset,
            "tmId": _int_tm_id(row.get("tmId"), context=asset),
            "tickerName": row.get("tickerName"),
            "asOfDate": as_of_date,
        })
    dates = {root["asOfDate"] for root in roots}
    if len(dates) != 1:
        raise TrendAnimalsError("data_stale", f"美股与美国ETF数据日期不一致：{sorted(dates)}")
    return roots


def build_preflight(*, docs: list[dict], billing: list[dict], status_rows: list[dict],
                    count_rows: list[dict]) -> dict:
    """验证最小计数快照，并给出仅适用于直接层级的两档费用代理值。

    文档没有定义“美股”与“美国ETF”根节点是否属于“组合榜单成分”。因此，
    下限按普通成分行价、上限按组合榜单行价。更重要的是，文档未定义
    ``constituentCount`` 与 ``all_basic=1`` 的递归展开行数关系，故不能把
    该代理值当作全量覆盖拉取的预算闸门。
    """
    roots = select_us_roots(status_rows)
    root_by_tm = {root["tmId"]: root for root in roots}
    rows_by_tm: dict[int, dict] = {}
    for row in count_rows:
        if not isinstance(row, dict) or row.get("tmId") is None:
            continue
        tm_id = _int_tm_id(row.get("tmId"), context="成分数快照")
        if tm_id in root_by_tm:
            rows_by_tm[tm_id] = row

    counts: dict[str, int] = {}
    for root in roots:
        row = rows_by_tm.get(root["tmId"])
        if row is None:
            raise TrendAnimalsError(
                "missing_required_fields", f"{root['asset']} 成分数快照缺少 tmId={root['tmId']}")
        if row.get("asOfDate") != root["asOfDate"]:
            raise TrendAnimalsError(
                "data_stale",
                f"{root['asset']} 成分数日期 {row.get('asOfDate')}，期望 {root['asOfDate']}",
            )
        count = row.get("constituentCount")
        if isinstance(count, bool) or not isinstance(count, (int, float)) or int(count) < 0:
            raise TrendAnimalsError("missing_required_fields", f"{root['asset']} 缺少有效 constituentCount")
        counts[root["asset"]] = int(count)

    base, normal_row, combo_row = component_pricing(docs)
    count_snapshot_cost = estimate_snapshot_cost(COUNT_FIELDS, len(roots), billing)
    normal_component_cost = round(sum(
        estimate_component_cost(
            counts[root["asset"]], combo=False, base_cost=base,
            normal_row_cost=normal_row, combo_row_cost=combo_row,
        )
        for root in roots
    ), 6)
    combo_component_cost = round(sum(
        estimate_component_cost(
            counts[root["asset"]], combo=True, base_cost=base,
            normal_row_cost=normal_row, combo_row_cost=combo_row,
        )
        for root in roots
    ), 6)
    return {
        "schema_version": 1,
        "scope": US_COVERAGE_SCOPE,
        "as_of_date": roots[0]["asOfDate"],
        "roots": roots,
        "constituent_counts": counts,
        "requested_snapshot_fields": COUNT_FIELDS,
        "planned_paid_calls": [
            {"endpoint": "getTickerSnapshot", "tmIds": [root["tmId"] for root in roots],
             "fields": COUNT_FIELDS},
            *[
                {"endpoint": "getComponentTicker", "tmId": root["tmId"],
                 "all_basic": True}
                for root in roots
            ],
        ],
        "fee_estimate_cny": {
            "count_snapshot": count_snapshot_cost,
            "component_base_per_root": base,
            "component_normal_row": normal_row,
            "component_combo_row": combo_row,
            "direct_components_proxy_lower": normal_component_cost,
            "direct_components_proxy_upper": combo_component_cost,
            "direct_total_proxy_lower": round(count_snapshot_cost + normal_component_cost, 6),
            "direct_total_proxy_upper": round(count_snapshot_cost + combo_component_cost, 6),
            "all_basic_expansion_estimate": None,
            "budget_gate": "blocked_all_basic_count_undocumented",
        },
        "scope_note": (
            "仅对 getUpdateStatus 中“美股”和“美国ETF”两个根节点各调用一次 "
            "getComponentTicker(all_basic=1) 并归档其返回的基础子级；不自行递归请求 "
            "ETF 持仓，不主张为全量美股。"
        ),
        "pricing_note": (
            "实时文档未说明这两个根节点按普通成分还是组合榜单成分计价；"
            "因此以普通行价为下限、组合榜单行价为上限。"
        ),
        "all_basic_guard_note": (
            "实时文档未承诺 constituentCount 等于 getAllBasicComponentsFlag=1 的展开行数；"
            "全量直接成分归档必须得到单独、明确的费用风险确认，不能只按本代理值放行。"
        ),
        "field_saving_note": (
            "未请求温度、趋势阶段、行情、基本面字段；覆盖盘点只使用身份字段和 constituentCount。"
        ),
    }


def validate_component_rows(rows: list[dict], *, root: dict) -> dict:
    """校验 all_basic 返回行的日期和唯一性，并返回可存入 manifest 的摘要。"""
    if not isinstance(rows, list):
        raise TrendAnimalsError("api_contract_error", f"{root['asset']} 成分返回不是数组")
    if not rows and root.get("constituentCount", 0):
        raise TrendAnimalsError("missing_required_fields", f"{root['asset']} 成分返回为空")
    tm_ids: list[int] = []
    missing_identity: list[int] = []
    date_counts: Counter[str] = Counter()
    root_date_mismatch_rows: list[int] = []
    assets: Counter[str] = Counter()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TrendAnimalsError("api_contract_error", f"{root['asset']} 第 {index} 行不是对象")
        tm_ids.append(_int_tm_id(row.get("tmId"), context=f"{root['asset']} 第 {index} 行"))
        row_date = row.get("asOfDate")
        date_counts[str(row_date or "未提供")] += 1
        if row_date != root["asOfDate"]:
            root_date_mismatch_rows.append(index)
        if not row.get("tickerName"):
            missing_identity.append(index)
        assets[str(row.get("asset") or "未提供")] += 1
    if len(set(tm_ids)) != len(tm_ids):
        raise TrendAnimalsError("api_contract_error", f"{root['asset']} 成分返回重复 tmId")
    return {
        "returned_rows": len(rows),
        "unique_tm_ids": len(set(tm_ids)),
        "missing_ticker_name_rows": missing_identity,
        "missing_ticker_symbol_rows": [
            index for index, row in enumerate(rows) if not isinstance(row, dict) or not row.get("tickerSymbol")
        ],
        "component_asset_breakdown": dict(sorted(assets.items())),
        "as_of_date_breakdown": dict(sorted(date_counts.items())),
        "root_as_of_date_rows": len(rows) - len(root_date_mismatch_rows),
        "root_date_mismatch_rows": root_date_mismatch_rows,
    }


def summarize_coverage(*, preflight: dict, component_rows_by_asset: dict[str, list[dict]]) -> dict:
    """生成不含温度/行情字段的基础子级存档摘要，供后续美股股池筛选读取。"""
    per_root: dict[str, dict] = {}
    unique_tm_ids: set[int] = set()
    count_warnings: list[dict] = []
    date_warnings: list[dict] = []
    for root in preflight["roots"]:
        asset = root["asset"]
        rows = component_rows_by_asset.get(asset)
        if rows is None:
            raise TrendAnimalsError("missing_required_fields", f"缺少 {asset} 成分存档")
        reported = int(preflight["constituent_counts"][asset])
        summary = validate_component_rows(rows, root={**root, "constituentCount": reported})
        if summary["returned_rows"] != reported:
            count_warnings.append({
                "asset": asset,
                "constituent_count": reported,
                "returned_basic_count": summary["returned_rows"],
                "note": "实时文档未说明 constituentCount 必须等于 all_basic 返回行数",
            })
        if summary["root_date_mismatch_rows"]:
            date_warnings.append({
                "asset": asset,
                "root_as_of_date": root["asOfDate"],
                "rows_on_root_date": summary["root_as_of_date_rows"],
                "rows_on_other_dates": len(summary["root_date_mismatch_rows"]),
                "as_of_date_breakdown": summary["as_of_date_breakdown"],
                "note": "覆盖归档保留原始日期；后续建池只使用根节点同日的品种。",
            })
        per_root[asset] = {"root": root, "reported_constituent_count": reported, **summary}
        unique_tm_ids.update(int(row["tmId"]) for row in rows)
    return {
        "scope": preflight["scope"],
        "as_of_date": preflight["as_of_date"],
        "per_root": per_root,
        "unique_basic_components": len(unique_tm_ids),
        "component_count_warnings": count_warnings,
        "component_as_of_date_warnings": date_warnings,
        "no_snapshot_fields_after_preflight": True,
    }


def build_current_universe_seed(*, preflight: dict,
                                component_rows_by_asset: dict[str, list[dict]]) -> dict:
    """从原始覆盖归档生成同日期、仅身份字段的后续建池种子。"""
    instruments: list[dict] = []
    excluded_other_dates: dict[str, int] = {}
    for root in preflight["roots"]:
        asset = root["asset"]
        rows = component_rows_by_asset.get(asset)
        if rows is None:
            raise TrendAnimalsError("missing_required_fields", f"缺少 {asset} 成分存档")
        excluded = 0
        for row in rows:
            if row.get("asOfDate") != root["asOfDate"]:
                excluded += 1
                continue
            instruments.append({
                "root_asset": asset,
                "tmId": row.get("tmId"),
                "tickerName": row.get("tickerName"),
                "tickerSymbol": row.get("tickerSymbol"),
                "asset": row.get("asset"),
                "assetCategory": row.get("assetCategory"),
                "currencyDefault": row.get("currencyDefault"),
                "asOfDate": row.get("asOfDate"),
            })
        excluded_other_dates[asset] = excluded
    unique = {item.get("tmId") for item in instruments if item.get("tmId") is not None}
    return {
        "schema_version": 1,
        "scope": "trend_animals_us_current_universe_seed_v1",
        "as_of_date": preflight["as_of_date"],
        "instrument_count": len(instruments),
        "unique_tm_ids": len(unique),
        "excluded_other_as_of_dates": excluded_other_dates,
        "scope_note": (
            "这是趋势动物同日覆盖范围的身份种子，不是可交易股票池；"
            "尚未做券商/交易平台可交易性、流动性、杠杆ETF或风险筛选。"
        ),
        "instruments": instruments,
    }
