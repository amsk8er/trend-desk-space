"""Protected automation trigger and authenticated status controls."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Body, Header, HTTPException
from sqlmodel import Session

from backend import config
from backend.discipline.automation import automation_status, run_automation
from backend.discipline.daily_data import china_trade_date
from backend.engine import engine
from backend.notify.email import deliver_email

router = APIRouter(prefix="/api/automation", tags=["automation"])


def _verify_automation_key(value: str | None) -> None:
    expected = config.AUTOMATION_SECRET
    if not expected or not value or not secrets.compare_digest(expected, value):
        raise HTTPException(401, "invalid_automation_key")


@router.post("/tick")
def tick(
    payload: dict = Body(default_factory=dict),
    x_automation_key: str | None = Header(default=None),
):
    _verify_automation_key(x_automation_key)
    with Session(engine) as session:
        return run_automation(
            session,
            stage=payload.get("stage"),
            trade_date=payload.get("trade_date"),
            trigger="github_actions",
        )


@router.get("/status")
def status():
    with Session(engine) as session:
        return automation_status(session)


@router.post("/run-now")
def run_now(payload: dict = Body(default_factory=dict)):
    with Session(engine) as session:
        return run_automation(
            session,
            stage=payload.get("stage", "finalize"),
            trade_date=payload.get("trade_date"),
            trigger="manual",
            allow_disabled=True,
        )


@router.post("/send-test")
def send_test():
    trade_date = china_trade_date()
    with Session(engine) as session:
        return deliver_email(
            session,
            trade_date=trade_date,
            kind="test",
            idempotency_key=f"test:{datetime_token()}",
            subject="[Trend Desk] 邮件配置测试",
            text_body="Trend Desk 已成功连接 Gmail。此邮件不包含任何交易建议或账户数据。",
        )


def datetime_token() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
