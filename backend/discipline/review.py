"""计划—成交对账与日纪律复盘。"""
from __future__ import annotations

from sqlmodel import Session, select

from backend.db import BrokerImport, DailyReview, Execution, TradePlan, TradePlanItem


def generate_review(s: Session, plan_id: str) -> dict:
    plan = s.get(TradePlan, plan_id)
    if plan is None:
        raise KeyError(plan_id)
    existing = s.exec(select(DailyReview).where(DailyReview.plan_id == plan_id)).first()
    if existing is not None:
        return existing.model_dump()
    items = s.exec(select(TradePlanItem).where(TradePlanItem.plan_id == plan_id)).all()
    imports = s.exec(select(BrokerImport).where(BrokerImport.plan_id == plan_id)).all()
    import_ids = [x.import_id for x in imports if x.import_id is not None]
    executions = (s.exec(select(Execution).where(Execution.import_id.in_(import_ids))).all()
                  if import_ids else [])
    actionable = [x for x in items if x.side != "hold"]
    completed = [x for x in actionable if x.status == "completed"]
    rate = (len(completed) / len(actionable)) if actionable else 1.0
    violations = []
    for x in actionable:
        if x.status == "missed":
            violations.append({"item_id": x.item_id, "code": x.instrument_id,
                               "type": "missed_plan_item", "side": x.side})
        elif x.status == "partially_executed":
            violations.append({"item_id": x.item_id, "code": x.instrument_id,
                               "type": "partial_execution", "side": x.side})
    unplanned = [e for e in executions if e.deviation_type == "unplanned_execution"]
    violations += [{"execution_id": e.execution_id, "code": e.instrument_id,
                    "type": "unplanned_execution"} for e in unplanned]
    score = max(0.0, rate * 100.0 - len(unplanned) * 10.0)
    pnl = sum(((-1 if e.side == "buy" else 1) * e.price * e.shares - e.fees) for e in executions)
    review = DailyReview(
        plan_id=plan_id, trade_date=plan.execute_date,
        plan_completion_rate=round(rate, 4), discipline_score=round(score, 2),
        trade_result="当日成交现金流为正" if pnl >= 0 else "当日成交现金流为负（非最终盈亏）",
        discipline_result="纪律合格" if not violations else "存在纪律偏差",
        violations=violations, data_issues=(plan.data_health or {}).get("warnings") or [],
        metrics={"actionable_items": len(actionable), "completed_items": len(completed),
                 "execution_count": len(executions), "net_execution_cashflow": round(pnl, 2)},
    )
    s.add(review); s.commit(); s.refresh(review)
    return review.model_dump()
