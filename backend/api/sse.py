# backend/api/sse.py
import asyncio
import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session

from backend.engine import engine
from backend.db import Batch
from backend.pipeline.state import get_state

router = APIRouter(prefix="/api/sse")


@router.get("/pipeline/{batch_id}")
async def sse_pipeline(batch_id: str, request: Request):
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            with Session(engine) as s:
                if s.get(Batch, batch_id) is None:
                    yield {"event": "error", "data": json.dumps({"error": f"unknown batch {batch_id}"})}
                    break
                state = get_state(s, batch_id)
            if state != last:
                yield {"event": "state", "data": json.dumps(state)}
                last = state
            await asyncio.sleep(1)
    return EventSourceResponse(gen())
