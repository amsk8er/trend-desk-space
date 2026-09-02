"""只追加决定事件 API；不提供修改或删除路由。"""
from fastapi import APIRouter, Body, HTTPException
from sqlmodel import Session

from backend.decision_events import (
    append_decision_event,
    list_decision_events,
    serialize_decision_event,
)
from backend.engine import engine


router = APIRouter(prefix="/api/decision-events", tags=["decision-events"])


@router.get("")
def decision_events_list(
    trade_date: str | None = None,
    market: str | None = None,
    instrument_id: str | None = None,
    plan_id: str | None = None,
    limit: int = 100,
):
    with Session(engine) as session:
        rows = list_decision_events(
            session,
            trade_date=trade_date,
            market=market,
            instrument_id=instrument_id,
            plan_id=plan_id,
            limit=limit,
        )
        return [serialize_decision_event(row) for row in rows]


@router.post("", status_code=201)
def decision_event_create(payload: dict = Body(...)):
    try:
        with Session(engine) as session:
            return serialize_decision_event(append_decision_event(session, payload))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
