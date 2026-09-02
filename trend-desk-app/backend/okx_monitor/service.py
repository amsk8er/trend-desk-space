from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from backend import config
from backend.db import OkxMonitorSync, OkxPositionPolicy, OkxPositionState
from backend.okx_monitor.alerts import send_due_alerts
from backend.okx_monitor.client import OkxReadOnlyClient
from backend.okx_monitor.contracts import PositionView, decimal_or_none
from backend.okx_monitor.market import completed_us_ema10, is_us_rth_bar, product_day
from backend.okx_monitor.normalization import (
    full_cover, is_spot_dust, normalize_balances, normalize_contract_positions, normalize_protections,
    protection_for_position,
)
from backend.okx_monitor.repository import (
    active_event, ensure_policy, heartbeat, resolve_unseen_events, save_position,
    save_protection, upsert_instruments,
)
from backend.okx_monitor.state_machine import (
    ReentryState, advance_cooldown, observe_confirmed_bar, observe_quantity, ratchet_stop,
    reset_for_product_day,
)
from backend.okx_monitor.stream import OkxPublicStream


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _price(position: PositionView) -> Decimal | None:
    return position.mark_price or position.last_price


def _fingerprint(position_key: str | None, event_type: str, product_day_value: str | None = None) -> str:
    raw = f"{position_key or 'account'}:{event_type}:{product_day_value or 'persistent'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _to_runtime(model: OkxPositionState, day: str) -> ReentryState:
    return reset_for_product_day(ReentryState(
        state=model.state, active_quantity=model.active_quantity, previous_quantity=model.previous_quantity,
        cooldown_until=model.cooldown_until, frozen_line=model.reentry_frozen_line,
        attempts=model.reentry_attempts, consecutive_bars=model.consecutive_confirmed_bars,
        product_day=model.product_day or day,
    ), day)


def _store_runtime(model: OkxPositionState, runtime: ReentryState, now: datetime, details: dict) -> None:
    model.state = runtime.state; model.active_quantity = runtime.active_quantity
    model.previous_quantity = runtime.previous_quantity; model.cooldown_until = runtime.cooldown_until
    model.reentry_frozen_line = runtime.frozen_line; model.reentry_attempts = runtime.attempts
    model.consecutive_confirmed_bars = runtime.consecutive_bars; model.product_day = runtime.product_day
    model.last_seen_at = now; model.details = details


class OkxMonitorService:
    def __init__(self, client: OkxReadOnlyClient, market_stream: OkxPublicStream | None = None) -> None:
        self.client = client
        self.market_stream = market_stream
        self._instruments: list[dict[str, Any]] = []
        self._instrument_refresh_at: datetime | None = None
        self._ema_cache: dict[str, tuple[datetime, Decimal, str, str]] = {}

    def _load_instruments(self, now: datetime) -> list[dict[str, Any]]:
        if not self._instruments or self._instrument_refresh_at is None or now >= self._instrument_refresh_at:
            self._instruments = self.client.all_instruments()
            self._instrument_refresh_at = now + timedelta(hours=6)
        return self._instruments

    def sync(self, session: Session, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        sync_id = uuid.uuid4().hex
        sync = OkxMonitorSync(sync_id=sync_id, started_at=now)
        session.add(sync); session.commit()
        try:
            instruments = self._load_instruments(now)
            instrument_map = {str(row.get("instId") or ""): row for row in instruments}
            balances = self.client.account_balance()
            contract_rows = self.client.positions()
            order_rows = self.client.pending_orders() + self.client.pending_algo_orders()
            positions = normalize_contract_positions(contract_rows, instrument_map)
            positions.extend(normalize_balances(
                balances, instruments,
                dust_notional_usd=Decimal(str(config.OKX_SPOT_DUST_USD)),
            ))
            # A currency can appear as a balance and a derivatives collateral row; position keys de-duplicate safely.
            positions = list({row.position_key: row for row in positions}.values())
            if self.market_stream:
                self.market_stream.set_instruments({row.identity.inst_id for row in positions if not row.identity.is_cash})
                priced = []
                for row in positions:
                    live_price = self.market_stream.price(row.identity.inst_id) or row.last_price
                    if live_price is None and row.identity.inst_type == "SPOT" and not row.identity.is_cash:
                        live_price = decimal_or_none(self.client.ticker(row.identity.inst_id).get("last"))
                    priced.append(replace(row, last_price=live_price))
                positions = priced
            dust_limit = Decimal(str(config.OKX_SPOT_DUST_USD))
            positions = [row for row in positions if not is_spot_dust(
                row, dust_notional_usd=dust_limit,
            )]
            protections = normalize_protections(order_rows)

            upsert_instruments(session, instruments, now)
            seen_events: set[str] = set()
            seen_positions: set[str] = set()
            for position in positions:
                seen_positions.add(position.position_key)
                save_position(session, sync_id, now, position)
                matches = protection_for_position(position, protections)
                policy = ensure_policy(session, position, matches, now)
                self._refresh_policy(policy, position, now)
                self._audit_position(session, position, matches, policy, now, seen_events)
                self._observe_open_position(session, position, policy, now, seen_events)
                for protection in matches:
                    save_protection(session, sync_id, now, protection, position.position_key)

            linked_order_keys = {row.order_key for position in positions for row in protection_for_position(position, protections)}
            for protection in protections:
                if protection.order_key not in linked_order_keys:
                    save_protection(session, sync_id, now, protection, None)

            self._observe_missing_positions(session, seen_positions, now, seen_events)
            self._advance_reentry(session, now, seen_events)
            resolve_unseen_events(session, seen_events, now)
            payload_hash = hashlib.sha256(json.dumps({
                "positions": sorted(seen_positions), "protections": sorted(row.order_key for row in protections),
            }).encode()).hexdigest()
            sync.status = "done"; sync.completed_at = now; sync.position_count = len(positions)
            sync.protection_count = len(protections); sync.balance_count = len(balances); sync.raw_hash = payload_hash
            heartbeat(session, now=now, enabled=config.OKX_MONITOR_ENABLED,
                      shadow=config.OKX_MONITOR_SHADOW_MODE, status="healthy", sync_at=now,
                      price_at=self.market_stream.last_message_at if self.market_stream else None,
                      details={"sync_id": sync_id, "positions": len(positions), "protections": len(protections)})
            session.add(sync); session.commit()
            sent = send_due_alerts(session, now=now, shadow=config.OKX_MONITOR_SHADOW_MODE)
            return {"status": "done", "sync_id": sync_id, "positions": len(positions),
                    "protections": len(protections), "emails_sent": len(sent)}
        except Exception as exc:
            session.rollback()
            sync = session.get(OkxMonitorSync, sync_id) or sync
            sync.status = "failed"; sync.completed_at = now
            sync.error = f"{type(exc).__name__}:{str(exc)[:300]}"
            heartbeat(session, now=now, enabled=config.OKX_MONITOR_ENABLED,
                      shadow=config.OKX_MONITOR_SHADOW_MODE, status="error", error=sync.error)
            session.add(sync); session.commit()
            raise

    def _refresh_policy(self, policy: OkxPositionPolicy, position: PositionView, now: datetime) -> None:
        if policy.mode == "manual":
            policy.effective_stop = policy.manual_stop
            policy.effective_source = "manual" if policy.manual_stop is not None else None
            policy.updated_at = now
            return
        if not position.identity.is_us_equity_related or not position.identity.underlying_symbol:
            policy.mode = "manual"; policy.effective_stop = policy.manual_stop; policy.effective_source = "manual"
            policy.updated_at = now
            return
        symbol = position.identity.underlying_symbol
        cached = self._ema_cache.get(symbol)
        if cached and now - cached[0] < timedelta(minutes=15):
            _, stop, session_date, source = cached
        else:
            stop, session_date, source = completed_us_ema10(symbol, now=now.replace(tzinfo=timezone.utc))
            self._ema_cache[symbol] = (now, stop, session_date, source)
        policy.last_auto_stop = ratchet_stop(side=position.side, previous=policy.last_auto_stop, candidate=stop)
        policy.effective_stop = policy.last_auto_stop; policy.effective_source = f"completed_rth_ema10:{source}"
        policy.last_completed_session = session_date; policy.updated_at = now

    def _audit_position(self, session: Session, position: PositionView, protections, policy,
                        now: datetime, seen: set[str]) -> None:
        if position.identity.is_cash:
            return
        day = product_day(us_equity_related=position.identity.is_us_equity_related,
                          now=now.replace(tzinfo=timezone.utc))
        full = [row for row in protections if full_cover(position, row)]
        def event(kind: str, severity: str, details: dict, daily: bool = False):
            fp = _fingerprint(position.position_key, kind, day if daily else None)
            seen.add(fp); active_event(session, position_key=position.position_key, event_type=kind,
                                       severity=severity, fingerprint=fp, now=now,
                                       details={"inst_id": position.identity.inst_id, **details})
        if not full:
            event("missing_protection", "critical", {"quantity": str(position.quantity)})
        elif len(full) > 1:
            event("manual_review", "warning", {"reason": "multiple_full_cover_stops", "count": len(full)})
        if policy.effective_stop is None:
            event("manual_review", "warning", {"reason": "discipline_line_missing"})
        elif full:
            protective = max(full, key=lambda row: row.trigger_price or Decimal("0")) if position.side == "long" else min(
                full, key=lambda row: row.trigger_price or Decimal("999999999"))
            trigger = protective.trigger_price
            weak = trigger is None or (position.side == "long" and trigger < policy.effective_stop) or (
                position.side == "short" and trigger > policy.effective_stop)
            if weak:
                event("weak_protection", "critical", {
                    "effective_stop": str(policy.effective_stop), "okx_trigger": str(trigger),
                })
            if position.identity.inst_type != "SPOT" and (protective.trigger_price_type or "last") != "mark":
                event("manual_review", "warning", {
                    "reason": "non_mark_trigger", "trigger_type": protective.trigger_price_type or "last",
                })
        price = _price(position)
        if price is not None and policy.effective_stop is not None:
            crossed = price <= policy.effective_stop if position.side == "long" else price >= policy.effective_stop
            if crossed:
                event("stop_line_breached", "critical", {
                    "price": str(price), "effective_stop": str(policy.effective_stop),
                    "observation_only": position.identity.is_us_equity_related and not is_us_rth_bar(
                        now.replace(tzinfo=timezone.utc)),
                }, daily=True)
        if price and position.liquidation_price and price > 0:
            distance = abs(price - position.liquidation_price) / price * Decimal("100")
            if distance <= Decimal(str(config.OKX_LIQUIDATION_ALERT_PCT)):
                event("liquidation_near", "critical", {
                    "distance_pct": str(round(distance, 3)), "liquidation_price": str(position.liquidation_price),
                })

    def _observe_open_position(self, session: Session, position: PositionView, policy: OkxPositionPolicy,
                               now: datetime, seen: set[str]) -> None:
        day = product_day(us_equity_related=position.identity.is_us_equity_related,
                          now=now.replace(tzinfo=timezone.utc))
        model = session.get(OkxPositionState, position.position_key)
        if model is None:
            model = OkxPositionState(position_key=position.position_key, product_day=day)
        runtime = _to_runtime(model, day)
        runtime, event_type = observe_quantity(runtime, quantity=position.quantity,
                                               effective_stop=policy.effective_stop, now=now)
        details = {"inst_id": position.identity.inst_id, "side": position.side,
                   "inst_type": position.identity.inst_type,
                   "us_equity_related": position.identity.is_us_equity_related}
        _store_runtime(model, runtime, now, details); session.add(model)
        if event_type == "reentry_filled":
            fp = _fingerprint(position.position_key, event_type, day)
            seen.add(fp); active_event(session, position_key=position.position_key, event_type=event_type,
                                       severity="info", fingerprint=fp, now=now, details=details)

    def _observe_missing_positions(self, session: Session, seen_positions: set[str], now: datetime,
                                   seen_events: set[str]) -> None:
        active = session.exec(select(OkxPositionState).where(OkxPositionState.active_quantity > 0)).all()
        missing = [row for row in active if row.position_key not in seen_positions]
        if not missing:
            return
        histories: list[dict[str, Any]] = []
        for inst_type in {str(row.details.get("inst_type") or "") for row in missing} - {""}:
            histories.extend(self.client.recent_orders(inst_type))
        for model in missing:
            inst_id = str(model.details.get("inst_id") or model.position_key.split(":")[1])
            stop_confirmed = any(str(row.get("instId")) == inst_id and str(row.get("source")) == "7"
                                 and str(row.get("state")) == "filled" for row in histories)
            policy = session.get(OkxPositionPolicy, model.position_key)
            day = product_day(us_equity_related=bool(model.details.get("us_equity_related")),
                              now=now.replace(tzinfo=timezone.utc))
            runtime = _to_runtime(model, day)
            runtime, event_type = observe_quantity(
                runtime, quantity=Decimal("0"), effective_stop=policy.effective_stop if policy else None,
                now=now, cooldown_minutes=config.OKX_REENTRY_COOLDOWN_MINUTES,
                stop_exit_confirmed=stop_confirmed,
            )
            _store_runtime(model, runtime, now, model.details); session.add(model)
            if event_type:
                fp = _fingerprint(model.position_key, event_type, day)
                seen_events.add(fp); active_event(
                    session, position_key=model.position_key, event_type=event_type,
                    severity="info", fingerprint=fp, now=now,
                    details={**model.details, "stop_exit_confirmed": stop_confirmed},
                )

    def _advance_reentry(self, session: Session, now: datetime, seen: set[str]) -> None:
        states = session.exec(select(OkxPositionState).where(
            OkxPositionState.state.in_(["cooldown", "awaiting_confirmation"])
        )).all()
        for model in states:
            runtime = advance_cooldown(_to_runtime(model, model.product_day or now.date().isoformat()), now)
            if runtime.state == "awaiting_confirmation" and runtime.frozen_line is not None:
                inst_id = str(model.details.get("inst_id") or "")
                confirmed = []
                if self.market_stream:
                    confirmed = [(row.closed_at, row.close)
                                 for row in self.market_stream.completed_bars(inst_id, model.last_bar_at)]
                else:
                    candles = self.client.candles(inst_id, bar="5m", limit=10) if inst_id else []
                    for row in candles:
                        if not isinstance(row, list) or len(row) < 9 or str(row[8]) != "1":
                            continue
                        opened = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
                        closed = opened + timedelta(minutes=5)
                        if model.last_bar_at is None or closed.replace(tzinfo=None) > model.last_bar_at:
                            confirmed.append((closed, decimal_or_none(row[4])))
                for closed, close in sorted(confirmed):
                    if close is None:
                        continue
                    eligible = not bool(model.details.get("us_equity_related")) or is_us_rth_bar(closed)
                    runtime, ready = observe_confirmed_bar(
                        runtime, side=str(model.details.get("side") or "long"), close=close,
                        eligible_session=eligible,
                    )
                    model.last_bar_at = closed.replace(tzinfo=None)
                    if ready:
                        fp = _fingerprint(model.position_key, "reentry_ready", runtime.product_day)
                        seen.add(fp); active_event(
                            session, position_key=model.position_key, event_type="reentry_ready",
                            severity="info", fingerprint=fp, now=now,
                            details={**model.details, "frozen_line": str(runtime.frozen_line),
                                     "attempt_number": runtime.attempts + 1},
                        )
                        break
            _store_runtime(model, runtime, now, model.details); session.add(model)
