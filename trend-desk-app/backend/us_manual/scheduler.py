"""北京时间 08:00–10:00 的 H6 美股/ETF、持仓退出与风险锚点调度器。"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

from sqlmodel import Session

from backend import config
from backend.engine import engine
from backend.us_manual.collection import shanghai_now
from backend.us_manual.service import UsManualService


log = logging.getLogger("trend-desk.us-manual-scheduler")


def scheduler_enabled() -> bool:
    return os.getenv(
        "US_MANUAL_SCHEDULER_ENABLED",
        str(config.US_MANUAL_SCHEDULER_ENABLED),
    ).lower() == "true"


def automatic_slots() -> list[str]:
    start_hour, start_minute = (int(part) for part in config.US_MANUAL_AUTO_START.split(":", 1))
    cutoff_hour, cutoff_minute = (int(part) for part in config.US_MANUAL_AUTO_CUTOFF.split(":", 1))
    cursor = datetime(2000, 1, 1, start_hour, start_minute)
    cutoff = datetime(2000, 1, 1, cutoff_hour, cutoff_minute)
    step = timedelta(minutes=max(1, config.US_MANUAL_RETRY_MINUTES))
    slots: list[str] = []
    while cursor <= cutoff:
        slots.append(cursor.strftime("%H:%M"))
        cursor += step
    return slots


def scheduler_tick(*, now: datetime | None = None,
                   service_factory: Callable[[], UsManualService] = UsManualService) -> dict:
    local_now = shanghai_now(now)
    slot = local_now.strftime("%H:%M")
    if slot not in automatic_slots():
        return {"ran": False, "reason": "outside_slot", "local_time": local_now.isoformat()}
    with Session(engine) as session:
        result = service_factory().collect(session, trigger="scheduled", now=local_now)
    return {"ran": True, "slot": slot, "result": result}


async def scheduler_loop() -> None:
    log.info(
        "US manual scheduler enabled: %s slots=%s",
        config.US_MANUAL_TIMEZONE,
        ",".join(automatic_slots()),
    )
    last_slot_key: str | None = None
    while True:
        local_now = shanghai_now()
        slot_key = f"{local_now.date().isoformat()}T{local_now.strftime('%H:%M')}"
        try:
            if local_now.strftime("%H:%M") in automatic_slots() and slot_key != last_slot_key:
                await asyncio.to_thread(scheduler_tick, now=local_now)
                last_slot_key = slot_key
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("US manual scheduler tick failed")
        await asyncio.sleep(20)
