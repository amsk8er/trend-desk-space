"""趋势动物批次与费用预检。"""
from __future__ import annotations

import hashlib
import json

from backend.trend_animals.billing import estimate_snapshot_cost

from .contracts import MAX_SNAPSHOT_BATCH, UsRightSideError, batches


def ensure_fields_available(fields: tuple[str, ...] | list[str], billing: list[dict]) -> None:
    names = {str(row.get("columnName")) for row in billing if row.get("columnName")}
    missing = [field for field in fields if field not in names]
    if missing:
        raise UsRightSideError(
            "billing_contract_changed",
            f"趋势动物实时字段表缺少：{','.join(missing)}",
            status_code=409,
            context={"missing_fields": missing},
        )


def _seal(plan: dict) -> dict:
    sealed = dict(plan)
    sealed.pop("preflight_hash", None)
    sealed["preflight_hash"] = hashlib.sha256(json.dumps(
        sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return sealed


def snapshot_plan(tm_ids: list[int], fields: tuple[str, ...] | list[str], billing: list[dict]) -> dict:
    ensure_fields_available(fields, billing)
    parts = batches(tm_ids, MAX_SNAPSHOT_BATCH)
    planned = []
    total = 0.0
    for index, part in enumerate(parts):
        cost = estimate_snapshot_cost(list(fields), len(part), billing)
        total += cost
        planned.append({
            "batch_index": index,
            "row_count": len(part),
            "tm_ids": part,
            "estimated_cost_cny": cost,
        })
    plan = {
        "row_count": len(tm_ids),
        "batch_count": len(parts),
        "fields": list(fields),
        "batches": planned,
        "estimated_cost_cny": round(total, 6),
    }
    plan["total_batch_count"] = len(parts)
    plan["completed_batch_indexes"] = []
    return _seal(plan)


def remaining_plan(plan: dict, completed_indexes: set[int]) -> dict:
    pending = [
        batch for batch in plan.get("batches", [])
        if int(batch["batch_index"]) not in completed_indexes
    ]
    return _seal({
        **plan,
        "row_count": sum(int(batch["row_count"]) for batch in pending),
        "batch_count": len(pending),
        "batches": pending,
        "estimated_cost_cny": round(sum(float(batch["estimated_cost_cny"]) for batch in pending), 6),
        "completed_batch_indexes": sorted(completed_indexes),
        "total_batch_count": int(plan.get("total_batch_count", plan.get("batch_count", len(pending)))),
    })


def require_budget(estimated_cost: float, approved_budget: float | None) -> None:
    if approved_budget is None or approved_budget + 1e-9 < estimated_cost:
        raise UsRightSideError(
            "budget_confirmation_required",
            f"预计费用 {estimated_cost:.4f} 元，需要批准足额预算",
            status_code=409,
            context={"estimated_cost_cny": estimated_cost, "approved_budget_cny": approved_budget},
        )


def require_preflight(plan: dict, supplied_hash: str | None) -> None:
    expected = str(plan.get("preflight_hash") or "")
    if not supplied_hash or supplied_hash != expected:
        raise UsRightSideError(
            "billing_contract_changed",
            "费用或字段计划已变化，请重新预检后再批准",
            status_code=409,
            context={"expected_preflight_hash": expected},
        )
