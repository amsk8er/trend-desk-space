"""北京时间每日采集调度；数据库记录任务状态，进程内循环只负责唤醒。"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timezone

from sqlmodel import Session

from backend import config
from backend.discipline.daily_data import (
    after_cutoff, before_collection_window, china_now, china_trade_date,
    ensure_dataset, run_daily_collection,
)
from backend.discipline.data_sources import TushareProbeClient
from backend.engine import engine
from backend.trend_animals.client import TrendAnimalsClient

log = logging.getLogger("trend-desk.daily-scheduler")


def scheduler_enabled() -> bool:
    return os.getenv(
        "TREND_DAILY_SCHEDULER_ENABLED",
        str(config.TREND_DAILY_SCHEDULER_ENABLED)).lower() == "true"


def scheduler_tick() -> dict:
    now = china_now()
    if before_collection_window(now) or after_cutoff(now):
        return {"ran": False, "reason": "outside_window"}
    trade_date = china_trade_date(now)
    with Session(engine) as s:
        dataset = ensure_dataset(s, trade_date)
        if dataset.status in {"ready", "ready_degraded", "manual_required", "awaiting_budget"}:
            return {"ran": False, "reason": dataset.status}
        if dataset.next_retry_at:
            due = dataset.next_retry_at.replace(tzinfo=timezone.utc)
            if due > now.astimezone(timezone.utc):
                return {"ran": False, "reason": "not_due"}
        trigger = "startup_catchup" if dataset.attempt_count == 0 else "scheduled"
        trend = TrendAnimalsClient()
        tushare = TushareProbeClient()
        try:
            out = run_daily_collection(
                s, trend_client=trend, tushare_client=tushare,
                trade_date=trade_date, trigger=trigger, scheduled_for=now,
                manual=False, now=now,
            )
            return {"ran": True, "dataset": out}
        finally:
            trend.close(); tushare.close()


async def scheduler_loop() -> None:
    log.info("daily scheduler enabled: %s %s-%s", config.TREND_DAILY_TIMEZONE,
             config.TREND_DAILY_START, config.TREND_DAILY_CUTOFF)
    while True:
        try:
            await asyncio.to_thread(scheduler_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("daily scheduler tick failed")
        await asyncio.sleep(60)
