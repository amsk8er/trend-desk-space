"""每日漏斗、成交与纪律复盘。"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from backend.db import UsDailyReview, UsDailyRun, UsManualExecution, UsManualPlan
from backend.us_manual import repository
from backend.us_manual.contracts import serialize
from backend.us_manual.exits import position_actions
from backend.us_manual.ledger import account_state


def _run_for_date(session: Session, as_of_date: str) -> UsDailyRun | None:
    return session.exec(select(UsDailyRun).where(UsDailyRun.as_of_date == as_of_date).order_by(
        UsDailyRun.completed_at.desc(), UsDailyRun.created_at.desc(),
    ).limit(1)).first()


def generate_review(session: Session, *, as_of_date: str, plan_id: str | None = None,
                    notes: str | None = None) -> dict[str, Any]:
    run = _run_for_date(session, as_of_date)
    if plan_id:
        plan = repository.get_plan(session, plan_id)
    else:
        plan = session.exec(select(UsManualPlan).where(UsManualPlan.signal_date == as_of_date).order_by(
            UsManualPlan.created_at.desc()).limit(1)).first()
    executions = session.exec(select(UsManualExecution).where(
        UsManualExecution.trade_date == as_of_date,
        UsManualExecution.confirmed.is_(True),
    ).order_by(UsManualExecution.execution_id)).all()
    actions = position_actions(session, as_of_date=as_of_date)
    account = account_state(session)
    funnel = dict(run.funnel_json) if run else {"state": "run_missing"}
    execution_json = {
        "count": len(executions),
        "records": [repository.model_payload(row) for row in executions],
        "plan_status": plan.status if plan else None,
    }
    compliance = {
        "manual_only": True,
        "has_confirmed_execution": bool(executions),
        "no_execution_confirmed": bool(plan and plan.status == "no_execution"),
        "positions_need_manual_exit": sum(1 for row in actions if row["action"] == "manual_exit"),
    }
    metrics = {"account": serialize(account), "position_actions": actions}
    existing = session.exec(select(UsDailyReview).where(
        UsDailyReview.as_of_date == as_of_date,
        UsDailyReview.plan_id == (plan.plan_id if plan else None),
    )).first()
    if existing is None:
        review = UsDailyReview(
            as_of_date=as_of_date,
            run_id=run.run_id if run else None,
            plan_id=plan.plan_id if plan else None,
            funnel_json=funnel,
            execution_json=execution_json,
            compliance_json=compliance,
            metrics_json=metrics,
            notes=notes,
        )
        session.add(review)
    else:
        # 复盘是每日状态快照，允许重新生成；计划、成交和原始扫描仍保持追加式证据。
        review = existing
        review.funnel_json = funnel
        review.execution_json = execution_json
        review.compliance_json = compliance
        review.metrics_json = metrics
        review.notes = notes or review.notes
        session.add(review)
    session.commit()
    session.refresh(review)
    return repository.model_payload(review)


def list_reviews(session: Session, *, as_of_date: str | None = None) -> list[dict[str, Any]]:
    statement = select(UsDailyReview)
    if as_of_date:
        statement = statement.where(UsDailyReview.as_of_date == as_of_date)
    rows = session.exec(statement.order_by(UsDailyReview.as_of_date.desc(), UsDailyReview.review_id.desc()).limit(50)).all()
    return [repository.model_payload(row) for row in rows]
