from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import exchange_calendars as xcals
import pandas as pd

from backend.us_calculator import _ema, fetch_us_market_snapshot


XNYS = xcals.get_calendar("XNYS")


def completed_us_ema10(symbol: str, *, now: datetime | None = None) -> tuple[Decimal, str, str]:
    """EMA10 from completed US regular-session daily bars only."""
    now = now or datetime.now(timezone.utc)
    snapshot = fetch_us_market_snapshot(symbol)
    bars = list(snapshot.get("bars") or [])
    now_stamp = pd.Timestamp(now)
    completed = XNYS.schedule[XNYS.schedule["close"] <= now_stamp]
    if completed.empty:
        raise RuntimeError("us_completed_session_unavailable")
    last_completed_session = completed.index[-1].date().isoformat()
    bars = [row for row in bars if str(row.get("time")) <= last_completed_session]
    closes = [float(row["close"]) for row in bars if row.get("close") is not None]
    if len(closes) < 10:
        raise RuntimeError("us_completed_daily_history_short")
    ema = _ema(closes, 10)[-1]
    if ema is None:
        raise RuntimeError("us_completed_ema10_unavailable")
    return Decimal(str(round(ema, 4))), str(bars[-1]["time"]), str(snapshot.get("source") or "public")


def is_us_rth_bar(closed_at: datetime) -> bool:
    """XNYS calendar gate, including holidays and early-close sessions."""
    return bool(XNYS.is_trading_minute(pd.Timestamp(closed_at)))


def product_day(*, us_equity_related: bool, now: datetime) -> str:
    if us_equity_related:
        opened = XNYS.schedule[XNYS.schedule["open"] <= pd.Timestamp(now)]
        if opened.empty:
            raise RuntimeError("us_product_session_unavailable")
        return opened.index[-1].date().isoformat()
    return now.astimezone(timezone.utc).date().isoformat()
