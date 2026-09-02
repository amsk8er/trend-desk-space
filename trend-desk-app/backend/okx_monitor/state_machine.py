from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class ReentryState:
    state: str
    active_quantity: Decimal
    previous_quantity: Decimal
    cooldown_until: datetime | None
    frozen_line: Decimal | None
    attempts: int
    consecutive_bars: int
    product_day: str


def reset_for_product_day(current: ReentryState, product_day: str) -> ReentryState:
    if current.product_day == product_day:
        return current
    return ReentryState(
        state="active" if current.active_quantity > 0 else "closed",
        active_quantity=current.active_quantity,
        previous_quantity=current.previous_quantity,
        cooldown_until=None,
        frozen_line=None,
        attempts=0,
        consecutive_bars=0,
        product_day=product_day,
    )


def observe_quantity(
    current: ReentryState,
    *,
    quantity: Decimal,
    effective_stop: Decimal | None,
    now: datetime,
    cooldown_minutes: int = 30,
    stop_exit_confirmed: bool = False,
) -> tuple[ReentryState, str | None]:
    previous = current.active_quantity
    if previous > 0 and quantity == 0:
        if not stop_exit_confirmed or effective_stop is None:
            return ReentryState(
                "closed", quantity, previous, None, None, current.attempts, 0, current.product_day,
            ), "position_closed"
        locked = current.attempts >= 2
        return ReentryState(
            "daily_locked" if locked else "cooldown",
            quantity, previous,
            None if locked else now + timedelta(minutes=cooldown_minutes),
            effective_stop, current.attempts, 0, current.product_day,
        ), "stop_exit"
    if previous == 0 and quantity > 0:
        linked = current.state == "reentry_ready" and current.attempts < 2
        return ReentryState(
            "active", quantity, previous, None, None,
            current.attempts + 1 if linked else current.attempts,
            0, current.product_day,
        ), "reentry_filled" if linked else "position_opened"
    return ReentryState(
        current.state, quantity, previous, current.cooldown_until, current.frozen_line,
        current.attempts, current.consecutive_bars, current.product_day,
    ), None


def advance_cooldown(current: ReentryState, now: datetime) -> ReentryState:
    if current.state == "cooldown" and current.cooldown_until and now >= current.cooldown_until:
        return ReentryState(
            "awaiting_confirmation", current.active_quantity, current.previous_quantity,
            None, current.frozen_line, current.attempts, 0, current.product_day,
        )
    return current


def observe_confirmed_bar(current: ReentryState, *, side: str, close: Decimal,
                          eligible_session: bool) -> tuple[ReentryState, bool]:
    if current.state != "awaiting_confirmation" or current.frozen_line is None or not eligible_session:
        return current, False
    beyond = close > current.frozen_line if side == "long" else close < current.frozen_line
    count = current.consecutive_bars + 1 if beyond else 0
    ready = count >= 2
    return ReentryState(
        "reentry_ready" if ready else current.state,
        current.active_quantity, current.previous_quantity, current.cooldown_until,
        current.frozen_line, current.attempts, count, current.product_day,
    ), ready


def ratchet_stop(*, side: str, previous: Decimal | None, candidate: Decimal) -> Decimal:
    if previous is None:
        return candidate
    return max(previous, candidate) if side == "long" else min(previous, candidate)
