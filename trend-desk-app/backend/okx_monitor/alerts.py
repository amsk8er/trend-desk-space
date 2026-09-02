from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from backend import config
from backend.db import OkxMonitorEvent
from backend.notify.email import deliver_email


def _due(event: OkxMonitorEvent, now: datetime) -> bool:
    if event.last_email_at is None:
        return True
    interval = timedelta(minutes=30) if event.email_count == 1 else timedelta(hours=1)
    return now - event.last_email_at >= interval


def _render(event: OkxMonitorEvent) -> tuple[str, str]:
    inst = str(event.details.get("inst_id") or event.position_key or "账户")
    labels = {
        "missing_protection": "缺少完整保护单",
        "weak_protection": "保护单弱于纪律线",
        "stop_line_breached": "价格触及纪律线",
        "liquidation_near": "接近强平价",
        "manual_review": "保护关系需要人工确认",
        "reentry_ready": "重新站回纪律线",
        "worker_stale": "监控心跳中断",
    }
    label = labels.get(event.event_type, event.event_type)
    subject = f"[Trend Desk][OKX][{event.severity.upper()}] {inst} {label}"
    body = (
        f"标的：{inst}\n事件：{label}\n级别：{event.severity}\n"
        f"首次发现：{event.first_seen_at.isoformat()}Z\n\n"
        f"详情：{event.details}\n\n"
        "这是只读监控提醒，系统没有下单、改单或撤单。请登录 OKX 人工核对。\n"
        f"监控页：{config.PUBLIC_URL}/"
    )
    return subject, body


def send_due_alerts(session: Session, *, now: datetime | None = None, shadow: bool) -> list[int]:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if shadow:
        return []
    sent: list[int] = []
    events = session.exec(select(OkxMonitorEvent).where(OkxMonitorEvent.status == "active")).all()
    for event in events:
        if not _due(event, now):
            continue
        subject, body = _render(event)
        deliver_email(
            session, trade_date=now.date().isoformat(), kind="okx_monitor",
            idempotency_key=f"okx:{event.fingerprint}:{event.email_count + 1}",
            subject=subject, text_body=body,
        )
        event.email_count += 1; event.last_email_at = now
        session.add(event); session.commit()
        if event.event_id is not None:
            sent.append(event.event_id)
    return sent


def send_watchdog_alert(session: Session, *, event: OkxMonitorEvent, now: datetime, shadow: bool) -> bool:
    """The web process can alert when the worker itself is unavailable."""
    if shadow or not _due(event, now):
        return False
    subject, body = _render(event)
    deliver_email(
        session, trade_date=now.date().isoformat(), kind="okx_monitor_watchdog",
        idempotency_key=f"okx:{event.fingerprint}:{event.email_count + 1}",
        subject=subject, text_body=body,
    )
    event.email_count += 1; event.last_email_at = now
    session.add(event); session.commit()
    return True
