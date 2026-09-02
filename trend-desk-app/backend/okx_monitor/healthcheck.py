from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from backend import config
from backend.db import OkxMonitorHeartbeat
from backend.engine import engine
from backend.okx_monitor.service import utcnow


def main() -> int:
    with Session(engine) as session:
        row = session.get(OkxMonitorHeartbeat, "main")
        if row is None or row.last_tick_at is None:
            return 1
        if utcnow() - row.last_tick_at > timedelta(seconds=config.OKX_STALE_SECONDS):
            return 1
        return 0 if row.status in {"healthy", "reconciling", "disabled", "configuration_required"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
