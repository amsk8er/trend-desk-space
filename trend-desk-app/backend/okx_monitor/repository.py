from __future__ import annotations

from datetime import datetime
from sqlmodel import Session, select

from backend.db import (
    OkxInstrument, OkxMonitorEvent, OkxMonitorHeartbeat,
    OkxPositionPolicy, OkxPositionSnapshot, OkxProtectionOrderSnapshot,
)
from backend.okx_monitor.classification import classify_instrument
from backend.okx_monitor.contracts import PositionView, ProtectionView, decimal_or_none


def upsert_instruments(session: Session, rows: list[dict], now: datetime) -> None:
    # The production database is remote. Loading the current catalogue once
    # avoids one network round-trip per instrument (roughly 2,000 on OKX).
    existing = {
        model.inst_id: model
        for model in session.exec(select(OkxInstrument)).all()
    }
    for row in rows:
        inst_id = str(row.get("instId") or "")
        if not inst_id:
            continue
        identity = classify_instrument(row)
        model = existing.get(inst_id) or OkxInstrument(
            inst_id=inst_id, inst_type=identity.inst_type, product_kind=identity.product_kind,
        )
        model.inst_type = identity.inst_type
        model.inst_family = row.get("instFamily") or None
        model.underlying = row.get("uly") or None
        model.base_ccy = row.get("baseCcy") or None
        model.quote_ccy = row.get("quoteCcy") or None
        model.settle_ccy = row.get("settleCcy") or None
        model.contract_value_ccy = row.get("ctValCcy") or None
        model.category = str(row.get("instCategory") or "") or None
        model.rule_type = row.get("ruleType") or None
        model.state = row.get("state") or None
        model.lot_size = decimal_or_none(row.get("lotSz"))
        model.tick_size = decimal_or_none(row.get("tickSz"))
        model.product_kind = identity.product_kind
        model.underlying_symbol = identity.underlying_symbol
        model.raw = row
        model.updated_at = now
        session.add(model)


def save_position(session: Session, sync_id: str, now: datetime, row: PositionView) -> None:
    session.add(OkxPositionSnapshot(
        sync_id=sync_id, captured_at=now, position_key=row.position_key,
        inst_id=row.identity.inst_id, inst_type=row.identity.inst_type,
        product_kind=row.identity.product_kind, underlying_symbol=row.identity.underlying_symbol,
        side=row.side, quantity=row.quantity, available_quantity=row.available_quantity,
        avg_price=row.avg_price, mark_price=row.mark_price, last_price=row.last_price,
        liquidation_price=row.liquidation_price, leverage=row.leverage,
        unrealized_pnl=row.unrealized_pnl, is_cash=row.identity.is_cash, raw=row.raw,
    ))


def save_protection(session: Session, sync_id: str, now: datetime, row: ProtectionView,
                    position_key: str | None) -> None:
    session.add(OkxProtectionOrderSnapshot(
        sync_id=sync_id, captured_at=now, order_key=row.order_key, position_key=position_key,
        inst_id=row.inst_id, order_id=row.order_id, algo_id=row.algo_id,
        order_type=row.order_type, side=row.side, quantity=row.quantity,
        trigger_price=row.trigger_price, trigger_price_type=row.trigger_price_type,
        status=row.status, reduce_only=row.reduce_only, close_fraction=row.close_fraction, raw=row.raw,
    ))


def ensure_policy(session: Session, position: PositionView, protections: list[ProtectionView],
                  now: datetime) -> OkxPositionPolicy:
    policy = session.get(OkxPositionPolicy, position.position_key)
    if policy is not None:
        return policy
    mode = "auto_ema10" if position.identity.is_us_equity_related else "manual"
    manual_stop = None
    full = [row for row in protections if row.trigger_price is not None]
    if mode == "manual" and len(full) == 1:
        manual_stop = full[0].trigger_price
    policy = OkxPositionPolicy(
        position_key=position.position_key, mode=mode, manual_stop=manual_stop,
        manual_reason="Imported from one unambiguous OKX protection" if manual_stop else None,
        effective_stop=manual_stop, effective_source="okx_import" if manual_stop else None,
        updated_at=now,
    )
    session.add(policy)
    return policy


def heartbeat(session: Session, *, now: datetime, enabled: bool, shadow: bool,
              status: str, sync_at: datetime | None = None, error: str | None = None,
              details: dict | None = None, price_at: datetime | None = None) -> OkxMonitorHeartbeat:
    model = session.get(OkxMonitorHeartbeat, "main") or OkxMonitorHeartbeat()
    model.enabled = enabled; model.shadow_mode = shadow; model.status = status; model.last_tick_at = now
    if sync_at is not None:
        model.last_sync_at = sync_at
    if price_at is not None:
        model.last_price_at = price_at
    if error:
        model.last_error_at = now
    model.error = error
    if details is not None:
        model.details = details
    session.add(model)
    return model


def active_event(session: Session, *, position_key: str | None, event_type: str, severity: str,
                 fingerprint: str, now: datetime, details: dict) -> OkxMonitorEvent:
    event = session.exec(select(OkxMonitorEvent).where(OkxMonitorEvent.fingerprint == fingerprint)).first()
    if event is None:
        event = OkxMonitorEvent(position_key=position_key, event_type=event_type, severity=severity,
                                fingerprint=fingerprint, details=details, first_seen_at=now)
    event.status = "active"; event.last_seen_at = now; event.resolved_at = None; event.details = details
    session.add(event)
    return event


def resolve_unseen_events(session: Session, seen: set[str], now: datetime) -> None:
    for event in session.exec(select(OkxMonitorEvent).where(OkxMonitorEvent.status == "active")).all():
        if event.fingerprint not in seen:
            event.status = "resolved"; event.resolved_at = now; event.last_seen_at = now
            session.add(event)
