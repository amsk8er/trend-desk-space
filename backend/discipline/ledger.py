"""Durable account ledger derived from confirmed executions and closing prices."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from sqlmodel import Session, select

from backend.db import (
    DailyDataset,
    Execution,
    FeeSchedule,
    LedgerAdjustment,
    PortfolioSnapshot,
    PositionLot,
    TradingDayConfirmation,
    TushareDailyFact,
)


def _bare(code: str | None) -> str:
    return str(code or "").upper().split(".")[0]


def _asset_type(code: str | None) -> str:
    bare = _bare(code)
    # 沪市 55 开头的 ETF（例如 551030）也应按 ETF/LOF 费率处理。
    return "etf" if bare.startswith(("15", "16", "51", "55", "56", "58")) else "stock"


def get_fee_schedule(session: Session) -> FeeSchedule:
    row = session.get(FeeSchedule, "default")
    if row is None:
        row = FeeSchedule()
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update_fee_schedule(session: Session, payload: dict) -> FeeSchedule:
    row = get_fee_schedule(session)
    for field in (
        "commission_rate",
        "minimum_commission",
        "etf_commission_rate",
        "etf_minimum_commission",
        "transfer_fee_rate",
        "stamp_duty_rate",
        "safety_multiplier",
    ):
        if field in payload:
            value = float(payload[field])
            if value < 0:
                raise ValueError(f"invalid_{field}")
            setattr(row, field, value)
    if row.safety_multiplier < 1:
        raise ValueError("safety_multiplier_must_be_at_least_one")
    row.configured = bool(payload.get("configured", True))
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def estimate_execution_fee(
    schedule: FeeSchedule,
    *,
    code: str,
    side: str,
    gross_amount: float,
) -> float:
    if not schedule.configured:
        raise ValueError("fee_schedule_not_configured")
    is_etf = _asset_type(code) == "etf"
    commission_rate = (
        schedule.etf_commission_rate
        if is_etf and schedule.etf_commission_rate is not None
        else schedule.commission_rate
    )
    minimum_commission = (
        schedule.etf_minimum_commission
        if is_etf and schedule.etf_minimum_commission is not None
        else schedule.minimum_commission
    )
    commission = max(minimum_commission, gross_amount * commission_rate)
    transfer = 0.0 if is_etf else gross_amount * schedule.transfer_fee_rate
    stamp = (
        gross_amount * schedule.stamp_duty_rate
        if side == "sell" and not is_etf
        else 0.0
    )
    conservative = (commission + transfer + stamp) * schedule.safety_multiplier
    return math.ceil(conservative * 100) / 100


def confirm_no_execution(session: Session, trade_date: str, note: str | None = None) -> dict:
    existing_execution = session.exec(select(Execution).where(
        Execution.trade_date == trade_date,
        Execution.confirmed.is_(True),
    )).first()
    if existing_execution is not None:
        raise ValueError("confirmed_execution_exists")
    row = session.exec(select(TradingDayConfirmation).where(
        TradingDayConfirmation.trade_date == trade_date)).first()
    if row is None:
        row = TradingDayConfirmation(
            trade_date=trade_date,
            status="no_execution",
            source="manual",
            note=note,
        )
    else:
        row.status = "no_execution"
        row.source = "manual"
        row.note = note
        row.import_id = None
        row.confirmed_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


def confirm_execution_day(
    session: Session,
    *,
    trade_date: str,
    import_id: int,
    source: str,
) -> TradingDayConfirmation:
    row = session.exec(select(TradingDayConfirmation).where(
        TradingDayConfirmation.trade_date == trade_date)).first()
    if row is None:
        row = TradingDayConfirmation(
            trade_date=trade_date,
            status="executions_confirmed",
            source=source,
            import_id=import_id,
        )
    else:
        row.status = "executions_confirmed"
        row.source = source
        row.import_id = import_id
        row.confirmed_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def add_adjustment(session: Session, payload: dict) -> LedgerAdjustment:
    adjustment_type = str(payload.get("adjustment_type") or "").strip()
    allowed = {
        "deposit", "withdrawal", "dividend", "tax", "share_adjustment", "correction",
    }
    if adjustment_type not in allowed:
        raise ValueError("invalid_adjustment_type")
    row = LedgerAdjustment(
        trade_date=str(payload["trade_date"]),
        adjustment_type=adjustment_type,
        instrument_id=(str(payload.get("instrument_id") or "").strip() or None),
        cash_amount=float(payload.get("cash_amount") or 0),
        share_delta=int(payload.get("share_delta") or 0),
        note=str(payload.get("note") or "").strip(),
        confirmed=bool(payload.get("confirmed", True)),
    )
    if not row.note:
        raise ValueError("adjustment_note_required")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _input_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def roll_forward(session: Session, trade_date: str) -> PortfolioSnapshot:
    confirmation = session.exec(select(TradingDayConfirmation).where(
        TradingDayConfirmation.trade_date == trade_date)).first()
    if confirmation is None:
        raise ValueError("trading_day_not_confirmed")
    dataset = session.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date)).first()
    if dataset is None or dataset.status not in {"ready", "ready_degraded"}:
        raise ValueError("dataset_not_ready")
    authoritative = session.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.confirmed.is_(True),
        PortfolioSnapshot.trade_date == trade_date,
        PortfolioSnapshot.source == "broker_ocr",
    ).order_by(PortfolioSnapshot.synced_at.desc())).first()

    executions = session.exec(select(Execution).where(
        Execution.trade_date == trade_date,
        Execution.confirmed.is_(True),
    ).order_by(Execution.execution_id)).all()
    if confirmation.status == "no_execution" and executions:
        raise ValueError("no_execution_conflicts_with_ledger")
    if confirmation.status == "executions_confirmed" and not executions:
        raise ValueError("execution_confirmation_is_empty")
    adjustments = session.exec(select(LedgerAdjustment).where(
        LedgerAdjustment.trade_date == trade_date,
        LedgerAdjustment.confirmed.is_(True),
    ).order_by(LedgerAdjustment.adjustment_id)).all()
    for adjustment in adjustments:
        if not adjustment.share_delta or adjustment.applied_at is not None:
            continue
        if not adjustment.instrument_id:
            raise ValueError("share_adjustment_requires_instrument")
        if adjustment.share_delta > 0:
            session.add(PositionLot(
                instrument_id=adjustment.instrument_id,
                name=adjustment.instrument_id,
                asset_type=_asset_type(adjustment.instrument_id),
                opened_on=trade_date,
                initial_shares=adjustment.share_delta,
                remaining_shares=adjustment.share_delta,
                avg_cost=0,
                source="ledger_adjustment",
                as_of_date=trade_date,
            ))
        else:
            remaining = abs(adjustment.share_delta)
            adjustment_lots = session.exec(select(PositionLot).where(
                PositionLot.instrument_id == adjustment.instrument_id,
                PositionLot.remaining_shares > 0,
            ).order_by(PositionLot.opened_on, PositionLot.lot_id)).all()
            for lot in adjustment_lots:
                take = min(lot.remaining_shares, remaining)
                lot.remaining_shares -= take
                lot.as_of_date = trade_date
                remaining -= take
                session.add(lot)
                if remaining == 0:
                    break
            if remaining:
                raise ValueError("position_adjustment_exceeds_holding")
        adjustment.applied_at = datetime.utcnow()
        session.add(adjustment)
    session.flush()
    lots = session.exec(select(PositionLot).where(
        PositionLot.remaining_shares > 0)).all()
    facts = session.exec(select(TushareDailyFact).where(
        TushareDailyFact.dataset_id == dataset.dataset_id)).all()
    close_by_code = {_bare(row.ts_code): row.close for row in facts if row.close is not None}
    missing_prices = sorted({
        lot.instrument_id for lot in lots if _bare(lot.instrument_id) not in close_by_code
    })
    if missing_prices:
        raise ValueError("missing_closing_prices:" + ",".join(missing_prices))

    market_value = round(sum(
        lot.remaining_shares * float(close_by_code[_bare(lot.instrument_id)])
        for lot in lots
    ), 2)
    for lot in lots:
        lot.as_of_date = trade_date
        session.add(lot)
    estimated = any(row.fee_source == "conservative_estimate" for row in executions)
    base_material = {
        "confirmation": confirmation.model_dump(),
        "executions": [row.model_dump() for row in executions],
        "adjustments": [row.model_dump() for row in adjustments],
        "positions": [
            {"lot_id": row.lot_id, "shares": row.remaining_shares}
            for row in lots
        ],
        "dataset_hash": dataset.dataset_hash,
    }

    # A confirmed same-day broker screenshot is an end-of-day account truth,
    # not an opening balance.  Validate it against official closes and retain
    # it instead of deriving a conflicting snapshot from yesterday's cash.
    if authoritative is not None:
        if abs(authoritative.market_value - market_value) > 0.05:
            raise ValueError(
                f"account_snapshot_market_value_mismatch:"
                f"{authoritative.market_value:.2f}:{market_value:.2f}"
            )
        expected_nav = round(authoritative.cash + market_value, 2)
        if abs(authoritative.nav - expected_nav) > 0.05:
            raise ValueError(
                f"account_snapshot_nav_mismatch:{authoritative.nav:.2f}:{expected_nav:.2f}"
            )
        authoritative.price_date = trade_date
        authoritative.reconciliation_status = "broker_reconciled"
        authoritative.derivation = {
            **(authoritative.derivation or {}),
            "roll_forward_input_hash": _input_hash(base_material),
            "confirmation_id": confirmation.confirmation_id,
            "execution_ids": [row.execution_id for row in executions],
            "adjustment_ids": [row.adjustment_id for row in adjustments],
            "estimated_fees": estimated,
        }
        session.add(authoritative)
        session.commit()
        session.refresh(authoritative)
        return authoritative

    previous = session.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.confirmed.is_(True),
        PortfolioSnapshot.trade_date < trade_date,
    ).order_by(PortfolioSnapshot.trade_date.desc(), PortfolioSnapshot.synced_at.desc())).first()
    if previous is None:
        raise ValueError("opening_snapshot_missing")
    cash = previous.cash
    for execution in executions:
        gross = execution.gross_amount or execution.price * execution.shares
        if execution.side == "buy":
            cash -= gross + execution.fees
        elif execution.side == "sell":
            cash += gross - execution.fees
        else:
            raise ValueError("invalid_execution_side")
    cash += sum(row.cash_amount for row in adjustments)
    if cash < -0.01:
        raise ValueError("negative_cash")
    cash = max(0.0, round(cash, 2))
    material = {
        "previous": previous.snapshot_id,
        **base_material,
    }
    digest = _input_hash(material)
    existing = session.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.trade_date == trade_date,
        PortfolioSnapshot.source == "derived_ledger",
    ).order_by(PortfolioSnapshot.synced_at.desc())).first()
    if existing and (existing.derivation or {}).get("input_hash") == digest:
        return existing
    row = PortfolioSnapshot(
        trade_date=trade_date,
        nav=round(cash + market_value, 2),
        cash=cash,
        market_value=market_value,
        source="derived_ledger",
        confirmed=True,
        as_of_date=trade_date,
        prior_snapshot_id=previous.snapshot_id,
        price_date=trade_date,
        reconciliation_status="fee_estimated" if estimated else "derived_confirmed",
        derivation={
            "input_hash": digest,
            "confirmation_id": confirmation.confirmation_id,
            "execution_ids": [row.execution_id for row in executions],
            "adjustment_ids": [row.adjustment_id for row in adjustments],
            "estimated_fees": estimated,
        },
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def ledger_status(session: Session, trade_date: str | None = None) -> dict:
    snapshot_query = select(PortfolioSnapshot).where(
        PortfolioSnapshot.confirmed.is_(True))
    if trade_date:
        snapshot_query = snapshot_query.where(PortfolioSnapshot.trade_date <= trade_date)
    snapshot = session.exec(snapshot_query.order_by(
        PortfolioSnapshot.trade_date.desc(), PortfolioSnapshot.synced_at.desc())).first()
    confirmation = None
    if trade_date:
        confirmation = session.exec(select(TradingDayConfirmation).where(
            TradingDayConfirmation.trade_date == trade_date)).first()
    lots = session.exec(select(PositionLot).where(PositionLot.remaining_shares > 0)).all()
    fee = get_fee_schedule(session)
    return {
        "trade_date": trade_date,
        "snapshot": snapshot.model_dump() if snapshot else None,
        "confirmation": confirmation.model_dump() if confirmation else None,
        "positions": [
            {
                "code": row.instrument_id,
                "name": row.name,
                "asset_type": row.asset_type,
                "shares": row.remaining_shares,
                "avg_cost": row.avg_cost,
                "as_of_date": row.as_of_date,
            }
            for row in lots
        ],
        "fee_schedule": fee.model_dump(exclude={"schedule_id"}),
        "ready_for_roll_forward": bool(snapshot and confirmation and fee.configured),
    }
