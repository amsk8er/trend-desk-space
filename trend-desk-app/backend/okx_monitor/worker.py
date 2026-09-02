from __future__ import annotations

import logging
import signal
import time

from sqlmodel import Session

from backend import config
from backend.engine import engine
from backend.okx_monitor.client import OkxReadOnlyClient
from backend.okx_monitor.repository import heartbeat
from backend.okx_monitor.service import OkxMonitorService, utcnow
from backend.okx_monitor.stream import OkxPublicStream
from backend.schema import ensure_database_ready
from backend.secrets import load_secrets_env


log = logging.getLogger("trend-desk.okx-monitor")


def _sleep_seconds(deadline: float, now: float) -> float:
    """Return a bounded, non-negative sleep interval for the heartbeat loop."""
    return max(0.0, min(1.0, deadline - now))


def build_client() -> OkxReadOnlyClient:
    return OkxReadOnlyClient(
        base_url=config.OKX_API_BASE_URL, api_key=config.OKX_API_KEY,
        api_secret=config.OKX_API_SECRET, passphrase=config.OKX_API_PASSPHRASE,
        demo=config.OKX_API_DEMO,
    )


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_secrets_env()
    ensure_database_ready()
    client = build_client()
    stream = OkxPublicStream()
    stream.start()
    service = OkxMonitorService(client, stream)
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        started = time.monotonic()
        try:
            with Session(engine) as session:
                if not config.OKX_MONITOR_ENABLED:
                    heartbeat(session, now=utcnow(), enabled=False,
                              shadow=config.OKX_MONITOR_SHADOW_MODE, status="disabled")
                    session.commit()
                elif not client.private_configured:
                    heartbeat(session, now=utcnow(), enabled=True,
                              shadow=config.OKX_MONITOR_SHADOW_MODE, status="configuration_required",
                              error="okx_private_credentials_not_configured")
                    session.commit()
                else:
                    heartbeat(session, now=utcnow(), enabled=True,
                              shadow=config.OKX_MONITOR_SHADOW_MODE, status="reconciling")
                    session.commit()
                    result = service.sync(session)
                    log.info("OKX reconciliation completed: %s", result)
        except Exception:
            log.exception("OKX reconciliation failed")
        elapsed = time.monotonic() - started
        wait = max(1.0, config.OKX_ACCOUNT_POLL_SECONDS - elapsed)
        deadline = time.monotonic() + wait
        next_heartbeat = time.monotonic() + config.OKX_HEARTBEAT_SECONDS
        while not stopping and time.monotonic() < deadline:
            if time.monotonic() >= next_heartbeat:
                with Session(engine) as session:
                    heartbeat(session, now=utcnow(), enabled=config.OKX_MONITOR_ENABLED,
                              shadow=config.OKX_MONITOR_SHADOW_MODE,
                              status="healthy" if client.private_configured else "configuration_required")
                    session.commit()
                next_heartbeat += config.OKX_HEARTBEAT_SECONDS
            sleep_seconds = _sleep_seconds(deadline, time.monotonic())
            if sleep_seconds <= 0:
                break
            time.sleep(sleep_seconds)
    stream.stop()


if __name__ == "__main__":
    run()
