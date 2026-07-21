"""Idempotent Beijing-time automation orchestration."""

from __future__ import annotations

from datetime import datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from backend import config
from backend.db import (
    AutomationRun,
    DailyDataset,
    EmailDelivery,
    FeeSchedule,
    PortfolioSnapshot,
    TradingDayConfirmation,
)
from backend.discipline.dataset_plan import ensure_executable_plan
from backend.discipline.daily_data import china_trade_date
from backend.discipline.ledger import roll_forward
from backend.discipline.plan import lock_plan
from backend.notify.email import (
    deliver_email,
    email_config_status,
    render_action_list,
    render_blocked,
    render_reminder,
)

CHINA = ZoneInfo("Asia/Shanghai")


def stage_for_now(now: datetime | None = None) -> str:
    current = (now or datetime.now(CHINA)).astimezone(CHINA).time()
    if current < time(17, 0):
        return "before_window"
    if current < time(19, 30):
        return "reminder"
    if current <= time(23, 0):
        return "finalize"
    return "after_window"


def _start_run(session: Session, trade_date: str, stage: str, trigger: str) -> AutomationRun:
    row = AutomationRun(
        run_id=f"auto_{trade_date.replace('-', '')}_{uuid4().hex[:12]}",
        trade_date=trade_date,
        stage=stage,
        status="running",
        trigger=trigger,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _finish(session: Session, row: AutomationRun, status: str, details: dict) -> dict:
    row.status = status
    row.details = details
    row.finished_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


def _blocked(session: Session, run: AutomationRun, errors: list[str]) -> dict:
    subject, body = render_blocked(run.trade_date, errors, config.PUBLIC_URL)
    delivery = None
    email_error = None
    if email_config_status()["configured"]:
        try:
            delivery = deliver_email(
                session,
                trade_date=run.trade_date,
                kind="blocked",
                idempotency_key=f"blocked:{run.trade_date}:v1",
                subject=subject,
                text_body=body,
            )
        except Exception as exc:
            email_error = f"{type(exc).__name__}: {exc}"
    details = {
        "errors": errors,
        "email": delivery,
    }
    if email_error:
        details["email_error"] = email_error
    return _finish(session, run, "blocked", details)


def run_automation(
    session: Session,
    *,
    stage: str | None = None,
    trade_date: str | None = None,
    trigger: str = "scheduled",
    allow_disabled: bool = False,
) -> dict:
    trade_date = trade_date or china_trade_date()
    stage = stage or stage_for_now()
    run = _start_run(session, trade_date, stage, trigger)
    if not config.AUTOMATION_ENABLED and not allow_disabled:
        return _finish(session, run, "skipped", {"reason": "automation_disabled"})
    if stage in {"before_window", "after_window"}:
        return _finish(session, run, "skipped", {"reason": stage})
    # The external request also wakes the service and performs the same guarded
    # collection tick as the in-process scheduler. Re-open state through the
    # current session afterwards.
    try:
        from backend.discipline.scheduler import scheduler_tick
        scheduler_tick()
        session.expire_all()
    except Exception:
        # Final data-health gates below decide whether an email may contain
        # quantities; a collection error therefore becomes a blocked notice.
        pass
    dataset = session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date)).first()
    if dataset and dataset.error_code == "not_trade_day":
        return _finish(session, run, "skipped", {"reason": "not_trade_day"})
    confirmation = session.exec(select(TradingDayConfirmation).where(
        TradingDayConfirmation.trade_date == trade_date)).first()
    if stage == "reminder":
        if confirmation is not None:
            return _finish(session, run, "skipped", {"reason": "already_confirmed"})
        subject, body = render_reminder(trade_date, config.PUBLIC_URL)
        try:
            delivery = deliver_email(
                session,
                trade_date=trade_date,
                kind="reminder",
                idempotency_key=f"reminder:{trade_date}:v1",
                subject=subject,
                text_body=body,
            )
        except Exception as exc:
            return _finish(session, run, "failed", {
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
        return _finish(session, run, "done", {"email": delivery})

    errors = []
    if dataset is None or dataset.status not in {"ready", "ready_degraded"}:
        errors.append("收盘数据尚未就绪")
    if confirmation is None:
        errors.append("尚未上传并确认成交截图，也未确认今日无成交")
    fee = session.get(FeeSchedule, "default")
    if fee is None or not fee.configured:
        errors.append("券商费率尚未配置")
    if session.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.confirmed.is_(True),
        PortfolioSnapshot.trade_date < trade_date,
    )).first() is None:
        errors.append("缺少可滚动的期初账户快照")
    if errors:
        return _blocked(session, run, errors)
    try:
        snapshot = roll_forward(session, trade_date)
        plan = ensure_executable_plan(
            session,
            dataset_id=dataset.dataset_id,
            portfolio_snapshot_id=snapshot.snapshot_id,
            change_notice="由确认成交台账自动生成",
        )
        health = plan.get("data_health") or {}
        if not health.get("lockable"):
            return _blocked(session, run, list(health.get("errors") or ["计划数据健康未通过"]))
        shadow = config.AUTOMATION_SHADOW_MODE
        if not shadow:
            plan = lock_plan(session, plan["plan_id"])
        subject, body = render_action_list(plan, shadow=shadow)
        delivery = deliver_email(
            session,
            trade_date=trade_date,
            plan_id=plan["plan_id"],
            kind="action_list_shadow" if shadow else "action_list",
            idempotency_key=f"action:{plan['plan_id']}:{'shadow' if shadow else 'live'}:v1",
            subject=subject,
            text_body=body,
        )
        return _finish(session, run, "done", {
            "shadow": shadow,
            "snapshot_id": snapshot.snapshot_id,
            "plan_id": plan["plan_id"],
            "email": delivery,
        })
    except Exception as exc:
        return _blocked(session, run, [str(exc)])


def automation_status(session: Session) -> dict:
    from backend.engine import is_postgres
    from backend.schema import postgres_revision
    latest = session.exec(select(AutomationRun).order_by(
        AutomationRun.started_at.desc())).first()
    latest_email = session.exec(select(EmailDelivery).order_by(
        EmailDelivery.created_at.desc())).first()
    recent_runs = session.exec(select(AutomationRun).where(
        AutomationRun.status == "done",
    ).order_by(AutomationRun.started_at.desc()).limit(30)).all()
    shadow_dates = sorted({
        row.trade_date for row in recent_runs if (row.details or {}).get("shadow") is True
    }, reverse=True)
    return {
        "enabled": config.AUTOMATION_ENABLED,
        "shadow_mode": config.AUTOMATION_SHADOW_MODE,
        "timezone": "Asia/Shanghai",
        "reminder_time": "17:00",
        "finalize_time": "19:30",
        "late_deadline": "23:00",
        "shadow_verified_days": len(shadow_dates),
        "shadow_ready_for_live": len(shadow_dates) >= 3,
        "email": email_config_status(),
        "database": {
            "backend": "postgresql" if is_postgres() else "sqlite",
            "persistent": is_postgres(),
            "revision": postgres_revision() if is_postgres() else "local-compat",
        },
        "latest_run": latest.model_dump() if latest else None,
        "latest_email": latest_email.model_dump() if latest_email else None,
    }
