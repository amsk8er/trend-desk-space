"""Idempotent Beijing-time automation orchestration."""

from __future__ import annotations

import hashlib
import json
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
    TrendDailyMembership,
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

BROKER_SNAPSHOT_SOURCES = {"broker_ocr"}
HOLDINGS_RECHECK_TRADING_DAYS = 5


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


def _collection_summary(session: Session, trade_date: str) -> dict:
    dataset = session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date)).first()
    if dataset is None:
        return {"status": "missing", "warm_to_hot_stock": 0, "warm_to_hot_etf": 0,
                "warm_to_hot_total": 0}
    memberships = session.exec(select(TrendDailyMembership).where(
        TrendDailyMembership.dataset_id == dataset.dataset_id)).all()
    stock = sum(row.membership_type == "warm_to_hot_stock" for row in memberships)
    etf = sum(row.membership_type == "warm_to_hot_etf" for row in memberships)
    return {
        "dataset_id": dataset.dataset_id,
        "status": dataset.status,
        "source_mode": dataset.source_mode,
        "warm_to_hot_stock": stock,
        "warm_to_hot_etf": etf,
        "warm_to_hot_total": stock + etf,
    }


def automation_readiness(session: Session, trade_date: str) -> dict:
    """返回稳定 blocker code，邮件、前端和 Actions 共用同一份真相。"""
    dataset = session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date)).first()
    confirmation = session.exec(select(TradingDayConfirmation).where(
        TradingDayConfirmation.trade_date == trade_date)).first()
    fee = session.get(FeeSchedule, "default")
    snapshots = session.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.confirmed.is_(True),
        PortfolioSnapshot.trade_date <= trade_date,
    ).order_by(PortfolioSnapshot.trade_date.desc(),
               PortfolioSnapshot.synced_at.desc())).all()
    blockers: list[dict] = []
    if dataset is None or dataset.status not in {"ready", "ready_degraded"}:
        blockers.append({
            "code": "dataset_not_ready",
            "message": "收盘数据尚未就绪",
            "action": "系统将继续重试趋势动物与行情采集",
            "human_required": False,
        })
    if confirmation is None:
        blockers.append({
            "code": "execution_confirmation_missing",
            "message": "尚未上传并确认成交截图，也未确认今日无成交",
            "action": "上传今日成交记录，或点击“今日无成交”",
            "human_required": True,
        })
    if fee is None or not fee.configured:
        blockers.append({
            "code": "fee_schedule_missing",
            "message": "券商费率尚未配置",
            "action": "一次性填写佣金、最低佣金及其他真实费率",
            "human_required": True,
        })
    can_roll = any(
        row.trade_date < trade_date
        or (row.trade_date == trade_date and row.source in BROKER_SNAPSHOT_SOURCES)
        for row in snapshots
    )
    if not can_roll:
        blockers.append({
            "code": "opening_snapshot_missing",
            "message": "缺少可滚动的期初账户快照",
            "action": "上传最新持仓截图并确认账户净值与现金",
            "human_required": True,
        })
    else:
        broker_snapshot = next(
            (row for row in snapshots if row.source in BROKER_SNAPSHOT_SOURCES), None
        )
        if broker_snapshot is None:
            stale_days = HOLDINGS_RECHECK_TRADING_DAYS
        else:
            ready_days = session.exec(select(DailyDataset).where(
                DailyDataset.trade_date > broker_snapshot.trade_date,
                DailyDataset.trade_date <= trade_date,
                DailyDataset.status.in_(["ready", "ready_degraded"]),
            )).all()
            stale_days = len(ready_days)
        if stale_days >= HOLDINGS_RECHECK_TRADING_DAYS:
            blockers.append({
                "code": "holdings_snapshot_stale",
                "message": f"最新券商持仓快照已超过 {HOLDINGS_RECHECK_TRADING_DAYS} 个交易日",
                "action": "上传最新持仓截图完成周期对账",
                "human_required": True,
            })
    summary = _collection_summary(session, trade_date)
    return {
        "trade_date": trade_date,
        "ready": not blockers,
        "human_action_required": any(row["human_required"] for row in blockers),
        "blockers": blockers,
        "collection_summary": summary,
        "confirmation": confirmation.model_dump(mode="json") if confirmation else None,
        "account_snapshot_id": snapshots[0].snapshot_id if snapshots else None,
        "fee_configured": bool(fee and fee.configured),
    }


def _blocker_fingerprint(blockers: list[dict]) -> str:
    material = sorted(
        [{"code": row.get("code"), "message": row.get("message")} for row in blockers],
        key=lambda row: (str(row["code"]), str(row["message"])),
    )
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _blocked(session: Session, run: AutomationRun, readiness: dict) -> dict:
    blockers = list(readiness.get("blockers") or [])
    subject, body = render_blocked(run.trade_date, blockers, config.PUBLIC_URL,
                                   readiness.get("collection_summary") or {})
    delivery = None
    email_error = None
    if email_config_status()["configured"]:
        try:
            delivery = deliver_email(
                session,
                trade_date=run.trade_date,
                kind="blocked",
                idempotency_key=(
                    f"blocked:{run.trade_date}:{_blocker_fingerprint(blockers)}:v2"
                ),
                subject=subject,
                text_body=body,
            )
        except Exception as exc:
            email_error = f"{type(exc).__name__}: {exc}"
    details = {
        "errors": [row.get("message") for row in blockers],
        "readiness": readiness,
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
    collection_tick = None
    collection_error = None
    try:
        from backend.discipline.scheduler import scheduler_tick
        collection_tick = scheduler_tick()
        session.expire_all()
    except Exception as exc:
        # Final data-health gates below decide whether an email may contain
        # quantities; a collection error therefore becomes a blocked notice.
        collection_error = f"{type(exc).__name__}: {exc}"
    dataset = session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date)).first()
    if dataset and dataset.error_code == "not_trade_day":
        return _finish(session, run, "skipped", {"reason": "not_trade_day"})
    readiness = automation_readiness(session, trade_date)
    if collection_error:
        readiness = dict(readiness)
        readiness["collection_error"] = collection_error
    if stage == "reminder":
        if readiness["ready"]:
            return _finish(session, run, "skipped", {
                "reason": "already_ready",
                "collection_tick": collection_tick,
                "readiness": readiness,
            })
        subject, body = render_reminder(trade_date, config.PUBLIC_URL, readiness)
        try:
            delivery = deliver_email(
                session,
                trade_date=trade_date,
                kind="reminder",
                idempotency_key=(
                    f"reminder:{trade_date}:"
                    f"{_blocker_fingerprint(readiness['blockers'])}:v3"
                ),
                subject=subject,
                text_body=body,
            )
        except Exception as exc:
            return _finish(session, run, "failed", {
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
        return _finish(session, run, "done", {
            "collection_tick": collection_tick,
            "readiness": readiness,
            "email": delivery,
        })

    if not readiness["ready"]:
        return _blocked(session, run, readiness)
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
            blockers = [{
                "code": "plan_data_health_blocked",
                "message": str(error),
                "action": "打开交易台查看计划数据闸门",
                "human_required": True,
            } for error in (health.get("errors") or ["计划数据健康未通过"])]
            return _blocked(session, run, {**readiness, "ready": False, "blockers": blockers})
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
            "collection_tick": collection_tick,
            "readiness": readiness,
            "email": delivery,
        })
    except Exception as exc:
        blocker = {
            "code": "finalization_error",
            "message": str(exc),
            "action": "查看交易台台账和计划错误详情",
            "human_required": True,
        }
        return _blocked(session, run, {**readiness, "ready": False, "blockers": [blocker]})


def maybe_finalize_after_input(session: Session, *, trade_date: str,
                               trigger: str) -> dict | None:
    if not config.AUTOMATION_ENABLED or stage_for_now() not in {"reminder", "finalize"}:
        return None
    readiness = automation_readiness(session, trade_date)
    if not readiness["ready"]:
        return {"status": "waiting", "readiness": readiness}
    return run_automation(
        session,
        stage="finalize",
        trade_date=trade_date,
        trigger=trigger,
    )


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
    trade_date = china_trade_date()
    return {
        "enabled": config.AUTOMATION_ENABLED,
        "shadow_mode": config.AUTOMATION_SHADOW_MODE,
        "timezone": "Asia/Shanghai",
        "reminder_time": "17:00",
        "finalize_time": "19:30",
        "late_deadline": "23:00",
        "shadow_verified_days": len(shadow_dates),
        "shadow_ready_for_live": len(shadow_dates) >= 3,
        "readiness": automation_readiness(session, trade_date),
        "email": email_config_status(),
        "database": {
            "backend": "postgresql" if is_postgres() else "sqlite",
            "persistent": is_postgres(),
            "revision": postgres_revision() if is_postgres() else "local-compat",
        },
        "latest_run": latest.model_dump() if latest else None,
        "latest_email": latest_email.model_dump() if latest_email else None,
    }
