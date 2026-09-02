"""Decision-event persistence.

The application contract is deliberately append-only: this module exposes no
update/delete operation. A correction is another event whose payload points at
the event being corrected.
"""
from __future__ import annotations

from datetime import date
import re
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from backend.db import DecisionEvent


EVENT_TYPES = frozenset({
    "plan_generated",
    "no_trade_confirmed",
    "candidate_excluded",
    "risk_blocked",
    "execution_confirmed",
    "execution_missed",
    "review_completed",
    "note",
})


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field}_required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_trade_date(value: Any) -> str:
    text = _required_text(value, "trade_date")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is None:
        raise ValueError("invalid_trade_date")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("invalid_trade_date") from exc


def serialize_decision_event(row: DecisionEvent) -> dict:
    return row.model_dump()


def append_decision_event(session: Session, payload: dict) -> DecisionEvent:
    event_type = _required_text(payload.get("event_type"), "event_type")
    if event_type not in EVENT_TYPES:
        raise ValueError("invalid_event_type")

    idempotency_key = _optional_text(payload.get("idempotency_key")) or f"manual:{uuid4()}"
    existing = session.exec(select(DecisionEvent).where(
        DecisionEvent.idempotency_key == idempotency_key,
    )).first()
    if existing is not None:
        return existing

    row = DecisionEvent(
        event_id=str(uuid4()),
        idempotency_key=idempotency_key,
        trade_date=_validate_trade_date(payload.get("trade_date")),
        market=_required_text(payload.get("market"), "market"),
        event_type=event_type,
        instrument_id=_optional_text(payload.get("instrument_id")),
        plan_id=_optional_text(payload.get("plan_id")),
        candidate_id=_optional_text(payload.get("candidate_id")),
        reason_code=_optional_text(payload.get("reason_code")),
        note=_optional_text(payload.get("note")),
        discipline_version=_optional_text(payload.get("discipline_version")),
        dataset_id=_optional_text(payload.get("dataset_id")),
        facts_hash=_optional_text(payload.get("facts_hash")),
        payload_json=dict(payload.get("payload_json") or {}),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # A simultaneous retry may win after the lookup but before commit.
        session.rollback()
        winner = session.exec(select(DecisionEvent).where(
            DecisionEvent.idempotency_key == idempotency_key,
        )).first()
        if winner is not None:
            return winner
        raise
    session.refresh(row)
    return row


def list_decision_events(
    session: Session,
    *,
    trade_date: str | None = None,
    market: str | None = None,
    instrument_id: str | None = None,
    plan_id: str | None = None,
    limit: int = 100,
) -> list[DecisionEvent]:
    statement = select(DecisionEvent)
    if trade_date:
        statement = statement.where(DecisionEvent.trade_date == trade_date)
    if market:
        statement = statement.where(DecisionEvent.market == market)
    if instrument_id:
        statement = statement.where(DecisionEvent.instrument_id == instrument_id)
    if plan_id:
        statement = statement.where(DecisionEvent.plan_id == plan_id)
    statement = statement.order_by(DecisionEvent.created_at.desc()).limit(
        min(max(int(limit), 1), 500)
    )
    return list(session.exec(statement).all())
