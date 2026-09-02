from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import Session, select

from backend import config
from backend.db import (
    OkxMonitorEvent, OkxMonitorHeartbeat, OkxMonitorSync, OkxPositionPolicy,
    OkxPositionSnapshot, OkxPositionState, OkxProtectionOrderSnapshot,
)
from backend.engine import engine
from backend.okx_monitor.service import utcnow
from backend.okx_monitor.alerts import send_watchdog_alert
from backend.okx_monitor.repository import active_event


router = APIRouter(prefix="/api/okx-monitor", tags=["okx-monitor"])


class PolicyPatch(BaseModel):
    mode: str
    manual_stop: Decimal | None = PydanticField(default=None, gt=0)
    reason: str | None = PydanticField(default=None, max_length=500)


def run_watchdog(session: Session) -> dict:
    now = utcnow()
    row = session.get(OkxMonitorHeartbeat, "main")
    monitor_enabled = row.enabled if row else config.OKX_MONITOR_ENABLED
    shadow = row.shadow_mode if row else config.OKX_MONITOR_SHADOW_MODE
    age = (now - row.last_tick_at).total_seconds() if row and row.last_tick_at else None
    stale = age is None or age > config.OKX_STALE_SECONDS
    fingerprint = "okx-monitor-worker-stale"
    emailed = False
    if stale and monitor_enabled:
        event = active_event(
            session, position_key=None, event_type="worker_stale", severity="critical",
            fingerprint=fingerprint, now=now,
            details={"inst_id": "OKX monitor", "heartbeat_age_seconds": age,
                     "threshold_seconds": config.OKX_STALE_SECONDS},
        )
        session.commit()
        emailed = send_watchdog_alert(session, event=event, now=now, shadow=shadow)
    else:
        event = session.exec(select(OkxMonitorEvent).where(
            OkxMonitorEvent.fingerprint == fingerprint)).first()
        if event and event.status == "active":
            event.status = "resolved"; event.resolved_at = now; event.last_seen_at = now
            session.add(event); session.commit()
    return {"status": "failed" if stale and monitor_enabled else "done",
            "stage": "okx_monitor_watchdog", "monitor_enabled": monitor_enabled,
            "stale": stale, "heartbeat_age_seconds": age, "email_sent": emailed,
            "should_fail_workflow": bool(stale and monitor_enabled)}


def _json(row):
    return row.model_dump(mode="json") if row else None


@router.get("/capabilities")
def capabilities():
    with Session(engine) as session:
        heartbeat = session.get(OkxMonitorHeartbeat, "main")
    return {
        "read_only": True,
        "trading_routes": False,
        "supported_products": ["us_stock_spot", "us_stock_xperp", "crypto_spot", "crypto_derivative"],
        "policy_modes": ["auto_ema10", "manual"],
        "email_provider": "gmail_smtp",
        "monitor_enabled": heartbeat.enabled if heartbeat else config.OKX_MONITOR_ENABLED,
        "shadow_mode": heartbeat.shadow_mode if heartbeat else config.OKX_MONITOR_SHADOW_MODE,
    }


@router.get("/overview")
def overview():
    with Session(engine) as session:
        latest = session.exec(select(OkxMonitorSync).where(
            OkxMonitorSync.status == "done").order_by(OkxMonitorSync.completed_at.desc())).first()
        heartbeat = session.get(OkxMonitorHeartbeat, "main")
        if latest is None:
            return {"status": "awaiting_first_sync", "heartbeat": _json(heartbeat),
                    "positions": [], "cash": [], "events": []}
        snapshots = session.exec(select(OkxPositionSnapshot).where(
            OkxPositionSnapshot.sync_id == latest.sync_id).order_by(OkxPositionSnapshot.inst_id)).all()
        protections = session.exec(select(OkxProtectionOrderSnapshot).where(
            OkxProtectionOrderSnapshot.sync_id == latest.sync_id)).all()
        orders_by_position: dict[str, list] = {}
        for order in protections:
            if order.position_key:
                orders_by_position.setdefault(order.position_key, []).append(_json(order))
        rows = []
        cash = []
        for snapshot in snapshots:
            policy = session.get(OkxPositionPolicy, snapshot.position_key)
            state = session.get(OkxPositionState, snapshot.position_key)
            item = {**_json(snapshot), "policy": _json(policy), "state": _json(state),
                    "protections": orders_by_position.get(snapshot.position_key, [])}
            (cash if snapshot.is_cash else rows).append(item)
        events = session.exec(select(OkxMonitorEvent).where(
            OkxMonitorEvent.status == "active").order_by(OkxMonitorEvent.severity, OkxMonitorEvent.first_seen_at)).all()
        stale = not heartbeat or not heartbeat.last_tick_at or (
            utcnow() - heartbeat.last_tick_at > timedelta(seconds=config.OKX_STALE_SECONDS))
        return {"status": "stale" if stale else "ready", "stale": stale,
                "heartbeat": _json(heartbeat), "latest_sync": _json(latest),
                "positions": rows, "cash": cash, "events": [_json(row) for row in events]}


@router.get("/positions/{position_key}/history")
def position_history(position_key: str, limit: int = 100):
    limit = max(1, min(limit, 500))
    with Session(engine) as session:
        snapshots = session.exec(select(OkxPositionSnapshot).where(
            OkxPositionSnapshot.position_key == position_key
        ).order_by(OkxPositionSnapshot.captured_at.desc()).limit(limit)).all()
        events = session.exec(select(OkxMonitorEvent).where(
            OkxMonitorEvent.position_key == position_key
        ).order_by(OkxMonitorEvent.first_seen_at.desc()).limit(limit)).all()
        return {"position_key": position_key, "snapshots": [_json(row) for row in snapshots],
                "events": [_json(row) for row in events]}


@router.patch("/positions/{position_key}/policy")
def update_policy(position_key: str, payload: PolicyPatch):
    if payload.mode not in {"auto_ema10", "manual"}:
        raise HTTPException(422, "invalid_policy_mode")
    if payload.mode == "manual" and (payload.manual_stop is None or not (payload.reason or "").strip()):
        raise HTTPException(422, "manual_stop_and_reason_required")
    with Session(engine) as session:
        latest = session.exec(select(OkxPositionSnapshot).where(
            OkxPositionSnapshot.position_key == position_key
        ).order_by(OkxPositionSnapshot.captured_at.desc())).first()
        if latest is None:
            raise HTTPException(404, "position_not_found")
        if payload.mode == "auto_ema10" and latest.product_kind not in {"us_stock_spot", "us_stock_xperp"}:
            raise HTTPException(422, "auto_ema10_only_supports_us_equity_products")
        policy = session.get(OkxPositionPolicy, position_key) or OkxPositionPolicy(position_key=position_key)
        policy.mode = payload.mode
        if payload.mode == "manual":
            policy.manual_stop = payload.manual_stop; policy.manual_reason = payload.reason.strip()
            policy.effective_stop = payload.manual_stop; policy.effective_source = "manual"
        else:
            policy.manual_stop = None; policy.manual_reason = None
            policy.effective_stop = policy.last_auto_stop; policy.effective_source = "completed_rth_ema10"
        policy.updated_at = datetime.utcnow(); session.add(policy); session.commit(); session.refresh(policy)
        return _json(policy)
