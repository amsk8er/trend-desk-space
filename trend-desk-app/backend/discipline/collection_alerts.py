"""Idempotent alerts for daily market-fact collection failures."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlmodel import Session

from backend import config
from backend.notify.email import deliver_email, email_config_status


def _as_text(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fingerprint(*, code: str, dataset: dict, scheduler: dict) -> str:
    material = {
        "code": code,
        "dataset_status": dataset.get("status"),
        "error_code": dataset.get("error_code"),
        "source_status": dataset.get("source_status"),
        "scheduler_reason": scheduler.get("last_reason"),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def render_collection_alert(
    *,
    trade_date: str,
    code: str,
    dataset: dict,
    scheduler: dict,
    public_url: str,
) -> tuple[str, str]:
    source_status = dataset.get("source_status") or {}
    trend = (source_status.get("trend_animals") or {}).get("status", "pending")
    tushare = (source_status.get("tushare") or {}).get("status", "pending")
    subject = f"[Trend Desk][采集告警] {trade_date} {code}"
    body = "\n".join([
        f"交易日：{trade_date}",
        f"告警代码：{code}",
        f"当前数据集：{dataset.get('dataset_id') or '尚未建立'} / {dataset.get('status') or 'missing'}",
        f"采集尝试：{dataset.get('attempt_count') or 0}",
        f"趋势动物：{trend}；Tushare：{tushare}",
        f"数据错误：{dataset.get('error_code') or '—'} {dataset.get('error_message') or ''}".rstrip(),
        f"调度心跳：{_as_text(scheduler.get('last_tick_at'))}",
        f"心跳年龄：{scheduler.get('heartbeat_age_seconds') if scheduler.get('heartbeat_age_seconds') is not None else '—'} 秒",
        f"最近来源：{scheduler.get('last_trigger') or '—'}",
        f"下一重试：{_as_text(dataset.get('next_retry_at'))}",
        f"处理入口：{public_url}",
        "",
        "系统不会用当前快照补写历史交易日，也不会绕过券商截图、OCR 与人工确认门。",
    ])
    return subject, body


def deliver_collection_alert(
    session: Session,
    *,
    trade_date: str,
    code: str,
    dataset: dict,
    scheduler: dict,
) -> dict:
    """Send at most one mail for an unchanged collection failure state.

    A delivery error is returned to the caller rather than raised, so the
    scheduler keeps its heartbeat and the watchdog can expose the failure.
    """
    if not email_config_status()["configured"]:
        return {"attempted": False, "status": "not_configured"}
    fingerprint = _fingerprint(code=code, dataset=dataset, scheduler=scheduler)
    subject, body = render_collection_alert(
        trade_date=trade_date,
        code=code,
        dataset=dataset,
        scheduler=scheduler,
        public_url=config.PUBLIC_URL,
    )
    try:
        delivery = deliver_email(
            session,
            trade_date=trade_date,
            kind="collection_alert",
            idempotency_key=f"collection:{trade_date}:{code}:{fingerprint}:v1",
            subject=subject,
            text_body=body,
        )
        return {"attempted": True, "status": "sent", "delivery": delivery}
    except Exception as exc:
        return {
            "attempted": True,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
