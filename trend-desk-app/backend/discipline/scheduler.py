"""北京时间每日采集调度；服务进程是主时钟，数据库保存可审计心跳。"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend import config
from backend.db import DailyDataset, DailySchedulerState
from backend.discipline.collection_alerts import deliver_collection_alert
from backend.discipline.daily_data import (
    after_cutoff,
    before_collection_window,
    china_now,
    china_trade_date,
    ensure_dataset,
    run_daily_collection,
)
from backend.discipline.data_sources import TushareProbeClient
from backend.engine import engine
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import redact_secret

log = logging.getLogger("trend-desk.daily-scheduler")

SCHEDULER_KEY = "discipline_daily"
READY_STATES = {"ready", "ready_degraded"}


def scheduler_enabled() -> bool:
    return os.getenv(
        "TREND_DAILY_SCHEDULER_ENABLED",
        str(config.TREND_DAILY_SCHEDULER_ENABLED)).lower() == "true"


def _china(value: datetime | None = None) -> datetime:
    current = value or china_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=china_now().tzinfo)
    return current.astimezone(china_now().tzinfo)


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _window_state(now: datetime) -> str:
    if before_collection_window(now):
        return "before_window"
    if after_cutoff(now):
        return "outside_window"
    return "open"


def _dataset_summary(dataset: DailyDataset | None) -> dict:
    if dataset is None:
        return {
            "dataset_id": None,
            "status": "missing",
            "attempt_count": 0,
            "next_retry_at": None,
            "error_code": None,
            "error_message": None,
            "source_status": {},
        }
    return {
        "dataset_id": dataset.dataset_id,
        "status": dataset.status,
        "attempt_count": dataset.attempt_count,
        "next_retry_at": dataset.next_retry_at,
        "error_code": dataset.error_code,
        "error_message": redact_secret(dataset.error_message) if dataset.error_message else None,
        "source_status": dataset.source_status or {},
    }


def _ensure_state(session: Session) -> DailySchedulerState:
    state = session.get(DailySchedulerState, SCHEDULER_KEY)
    if state is not None:
        return state
    state = DailySchedulerState(scheduler_key=SCHEDULER_KEY, enabled=scheduler_enabled())
    session.add(state)
    try:
        session.commit()
        session.refresh(state)
        return state
    except IntegrityError:
        session.rollback()
        current = session.get(DailySchedulerState, SCHEDULER_KEY)
        if current is None:
            raise
        return current


def _age_seconds(value: datetime | None, now_utc: datetime) -> int | None:
    if value is None:
        return None
    current = value
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0, int((now_utc - current).total_seconds()))


def scheduler_status(session: Session, *, now: datetime | None = None) -> dict:
    """Return a secret-free view of the service-side scheduler heartbeat."""
    current = _china(now)
    now_utc = _utc_naive(current)
    state = session.get(DailySchedulerState, SCHEDULER_KEY)
    window_state = _window_state(current)
    threshold = (
        config.TREND_DAILY_HEARTBEAT_WINDOW_SECONDS
        if window_state == "open"
        else config.TREND_DAILY_HEARTBEAT_IDLE_SECONDS
    )
    age = _age_seconds(state.last_tick_at, now_utc) if state else None
    boot_age = _age_seconds(state.process_started_at, now_utc) if state else None
    heartbeat_fresh = age is not None and age <= threshold
    fresh_boot = boot_age is not None and boot_age <= config.TREND_DAILY_HEARTBEAT_WINDOW_SECONDS
    enabled = scheduler_enabled()
    last_trigger = state.last_trigger if state else None
    primary_healthy = bool(
        enabled
        and state
        and state.enabled
        and heartbeat_fresh
        and not fresh_boot
        and last_trigger != "github_fallback"
    )
    return {
        "scheduler_key": SCHEDULER_KEY,
        "enabled": enabled,
        "recorded_enabled": state.enabled if state else None,
        "window_state": window_state,
        "server_time_china": current,
        "heartbeat_threshold_seconds": threshold,
        "heartbeat_age_seconds": age,
        "boot_age_seconds": boot_age,
        "heartbeat_fresh": heartbeat_fresh,
        "fresh_boot": fresh_boot,
        "primary_healthy": primary_healthy,
        "boot_id": state.boot_id if state else None,
        "process_started_at": state.process_started_at if state else None,
        "last_tick_at": state.last_tick_at if state else None,
        "last_window_tick_at": state.last_window_tick_at if state else None,
        "last_trade_date": state.last_trade_date if state else None,
        "last_result": state.last_result if state else None,
        "last_reason": state.last_reason if state else None,
        "last_dataset_status": state.last_dataset_status if state else None,
        "last_attempt_at": state.last_attempt_at if state else None,
        "next_due_at": state.next_due_at if state else None,
        "last_trigger": last_trigger,
        "last_error": state.last_error if state else None,
        "updated_at": state.updated_at if state else None,
    }


def _record_heartbeat(
    session: Session,
    *,
    now: datetime,
    trade_date: str,
    trigger: str,
    result: str,
    reason: str | None,
    dataset: DailyDataset | None,
    attempted: bool = False,
    boot_id: str | None = None,
    process_started_at: datetime | None = None,
) -> DailySchedulerState:
    state = _ensure_state(session)
    now_utc = _utc_naive(now)
    state.enabled = scheduler_enabled()
    if boot_id:
        state.boot_id = boot_id
    if process_started_at:
        state.process_started_at = _utc_naive(process_started_at)
    state.last_tick_at = now_utc
    if _window_state(now) == "open":
        state.last_window_tick_at = now_utc
    state.last_trade_date = trade_date
    state.last_result = result
    state.last_reason = reason
    state.last_dataset_status = dataset.status if dataset else "missing"
    if attempted:
        state.last_attempt_at = now_utc
    state.next_due_at = dataset.next_retry_at if dataset else None
    state.last_trigger = trigger
    state.last_error = (
        redact_secret(dataset.error_message)[:1000]
        if dataset and dataset.error_message
        else None
    )
    state.updated_at = now_utc
    session.add(state)
    session.commit()
    session.refresh(state)
    return state


def _find_dataset(session: Session, trade_date: str) -> DailyDataset | None:
    return session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date,
    )).first()


def _alert_code(*, window_state: str, dataset: DailyDataset | None) -> str | None:
    if dataset is not None and dataset.error_code == "not_trade_day":
        return None
    status = dataset.status if dataset else "missing"
    if status in READY_STATES:
        return None
    if window_state == "outside_window":
        return "outside_window"
    if status == "awaiting_budget":
        return "awaiting_budget"
    if status == "manual_required":
        return (dataset.error_code if dataset else None) or "manual_required"
    if status in {"missing", "pending", "checking", "fetching", "waiting_retry", "failed"}:
        return "dataset_not_ready"
    return None


def _attach_alert(
    session: Session,
    *,
    result: dict,
    current: datetime,
    dataset: DailyDataset | None,
) -> dict:
    code = _alert_code(window_state=result["window_state"], dataset=dataset)
    if code is None:
        return result
    if result["window_state"] == "before_window":
        return result
    result["alert_code"] = code
    result["alert"] = deliver_collection_alert(
        session,
        trade_date=result["trade_date"],
        code=code,
        dataset=result["dataset"],
        scheduler=scheduler_status(session, now=current),
    )
    return result


def scheduler_tick(
    *,
    now: datetime | None = None,
    trigger: str = "service_scheduled",
    boot_id: str | None = None,
    process_started_at: datetime | None = None,
) -> dict:
    """Run at most one due collection attempt and always persist a heartbeat."""
    current = _china(now)
    trade_date = china_trade_date(current)
    window_state = _window_state(current)
    with Session(engine) as session:
        dataset = _find_dataset(session, trade_date)
        result = {
            "ran": False,
            "trigger": trigger,
            "server_time_china": current,
            "trade_date": trade_date,
            "window_state": window_state,
            "reason": None,
            "dataset": _dataset_summary(dataset),
        }
        if window_state != "open":
            reason = "before_window" if window_state == "before_window" else "outside_window"
            result["reason"] = reason
            _record_heartbeat(
                session, now=current, trade_date=trade_date, trigger=trigger,
                result=reason, reason=reason, dataset=dataset,
                boot_id=boot_id, process_started_at=process_started_at,
            )
            return _attach_alert(session, result=result, current=current, dataset=dataset)

        dataset = ensure_dataset(session, trade_date)
        result["dataset"] = _dataset_summary(dataset)
        if dataset.error_code == "not_trade_day":
            result["reason"] = "not_trade_day"
        elif dataset.status in READY_STATES | {"manual_required", "awaiting_budget"}:
            result["reason"] = dataset.status
        elif dataset.next_retry_at:
            due = dataset.next_retry_at
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > current.astimezone(timezone.utc):
                result["reason"] = "not_due"

        if result["reason"] is not None:
            _record_heartbeat(
                session, now=current, trade_date=trade_date, trigger=trigger,
                result=dataset.status, reason=result["reason"], dataset=dataset,
                boot_id=boot_id, process_started_at=process_started_at,
            )
            result["dataset"] = _dataset_summary(dataset)
            return _attach_alert(session, result=result, current=current, dataset=dataset)

        previous_attempts = dataset.attempt_count
        trend = TrendAnimalsClient()
        tushare = TushareProbeClient()
        try:
            out = run_daily_collection(
                session,
                trend_client=trend,
                tushare_client=tushare,
                trade_date=trade_date,
                trigger=trigger,
                scheduled_for=current,
                manual=False,
                now=current,
            )
            session.expire_all()
            dataset = _find_dataset(session, trade_date)
            result["ran"] = True
            result["dataset"] = _dataset_summary(dataset)
            result["reason"] = (
                "not_trade_day"
                if dataset and dataset.error_code == "not_trade_day"
                else (out.get("error_code") if isinstance(out, dict) else None)
            )
            _record_heartbeat(
                session, now=current, trade_date=trade_date, trigger=trigger,
                result=dataset.status if dataset else "missing", reason=result["reason"],
                dataset=dataset,
                attempted=bool(dataset and dataset.attempt_count > previous_attempts),
                boot_id=boot_id, process_started_at=process_started_at,
            )
            return _attach_alert(session, result=result, current=current, dataset=dataset)
        except Exception as exc:
            session.rollback()
            dataset = _find_dataset(session, trade_date)
            result["reason"] = f"{type(exc).__name__}"
            result["dataset"] = _dataset_summary(dataset)
            _record_heartbeat(
                session, now=current, trade_date=trade_date, trigger=trigger,
                result="error", reason=result["reason"], dataset=dataset,
                boot_id=boot_id, process_started_at=process_started_at,
            )
            result["scheduler_error"] = redact_secret(exc)
            return _attach_alert(session, result=result, current=current, dataset=dataset)
        finally:
            trend.close()
            tushare.close()


async def scheduler_loop() -> None:
    """Keep one process-level heartbeat alive; DailyDataset lease handles replicas."""
    boot_id = uuid4().hex
    started_at = china_now()
    log.info(
        "daily scheduler enabled: %s %s-%s boot=%s",
        config.TREND_DAILY_TIMEZONE,
        config.TREND_DAILY_START,
        config.TREND_DAILY_CUTOFF,
        boot_id[:8],
    )
    first_tick = True
    while True:
        try:
            await asyncio.to_thread(
                scheduler_tick,
                trigger="startup_catchup" if first_tick else "service_scheduled",
                boot_id=boot_id,
                process_started_at=started_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("daily scheduler tick failed")
        first_tick = False
        await asyncio.sleep(60)
