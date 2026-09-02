"""H6 美股真实趋势退出：危险/温转平全退，沸/开香槟分别减 25%。"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable
from uuid import uuid4

from sqlmodel import Session, select

from backend.db import (
    UsExitDecision,
    UsHoldingSignalSnapshot,
    UsManualExecution,
    UsManualPlanItem,
    UsPositionLot,
)
from backend.us_manual import repository
from backend.us_manual.bitget_public import fetch_public_quote
from backend.us_manual.contracts import (
    HOLDING_EXIT_FIELDS,
    US_MANUAL_RULES_VERSION,
    UsManualError,
    as_int,
    decimal_text,
    serialize,
    sha256,
)
from backend.us_manual.risk_anchor import execution_schedule, next_us_session


EXIT_TEMPERATURES = {"平", "凉", "寒", "冻"}
HOLD_TEMPERATURES = {"温", "热", "沸"}


def _floor(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-precision), rounding=ROUND_DOWN)


def _venue_for_lot(session: Session, lot: UsPositionLot) -> dict[str, Any]:
    if lot.opened_by_execution_id is None:
        return {}
    execution = session.get(UsManualExecution, lot.opened_by_execution_id)
    if execution is None or execution.plan_item_id is None:
        return {}
    item = session.get(UsManualPlanItem, execution.plan_item_id)
    return dict(item.venue_metadata_json or {}) if item is not None else {}


def decide_exit(
    *,
    remaining_quantity: Decimal,
    temperature_curr: str | None,
    danger: bool | None,
    boiling: bool | None,
    champagne: bool | None,
    quantity_precision: Any,
    min_trade_usdt: Any | None,
    public_quote_usdt: Decimal | None,
) -> dict[str, Any]:
    """Pure H6 decision. Missing booleans are data failures, never implicit False."""
    if temperature_curr not in (EXIT_TEMPERATURES | HOLD_TEMPERATURES):
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": Decimal("0"),
            "planned_quantity": Decimal("0"), "reason_codes": ["temperature_missing"],
        }
    if any(value is None for value in (danger, boiling, champagne)):
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": Decimal("0"),
            "planned_quantity": Decimal("0"), "reason_codes": ["exit_fields_incomplete"],
        }
    if danger or temperature_curr in EXIT_TEMPERATURES:
        reasons = []
        if danger:
            reasons.append("danger")
        if temperature_curr in EXIT_TEMPERATURES:
            reasons.append("temperature_flat_or_below")
        return {
            "action": "exit_all", "priority": 1, "sell_ratio": Decimal("1"),
            "planned_quantity": remaining_quantity, "reason_codes": reasons,
        }
    profit_count = int(bool(boiling)) + int(bool(champagne))
    if profit_count == 0:
        return {
            "action": "hold", "priority": 4, "sell_ratio": Decimal("0"),
            "planned_quantity": Decimal("0"), "reason_codes": ["trend_hold"],
        }
    try:
        precision = int(str(quantity_precision))
    except (TypeError, ValueError):
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": Decimal("0"),
            "planned_quantity": Decimal("0"), "reason_codes": ["quantity_precision_missing"],
        }
    if precision < 0 or precision > 12:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": Decimal("0"),
            "planned_quantity": Decimal("0"), "reason_codes": ["quantity_precision_invalid"],
        }
    ratio = Decimal("0.25") * profit_count
    quantity = _floor(remaining_quantity * ratio, precision)
    if min_trade_usdt is None:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity, "reason_codes": ["minimum_trade_amount_missing"],
        }
    try:
        minimum = Decimal(str(min_trade_usdt))
    except Exception:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity, "reason_codes": ["minimum_trade_amount_invalid"],
        }
    if not minimum.is_finite() or minimum < 0:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity, "reason_codes": ["minimum_trade_amount_invalid"],
        }
    if public_quote_usdt is None:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity, "reason_codes": ["partial_exit_quote_unavailable"],
        }
    if not public_quote_usdt.is_finite() or public_quote_usdt <= 0:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity, "reason_codes": ["partial_exit_quote_invalid"],
        }
    if quantity <= 0 or quantity * public_quote_usdt < minimum:
        return {
            "action": "manual_review", "priority": 0, "sell_ratio": ratio,
            "planned_quantity": quantity,
            "reason_codes": ["partial_exit_below_minimum"],
        }
    return {
        "action": "reduce_50" if profit_count == 2 else "reduce_25",
        "priority": 2 if profit_count == 2 else 3,
        "sell_ratio": ratio,
        "planned_quantity": quantity,
        "reason_codes": [
            *( ["boiling"] if boiling else [] ),
            *( ["champagne"] if champagne else [] ),
        ],
    }


def _previous_snapshot(session: Session, lot_id: int, as_of_date: str) -> UsHoldingSignalSnapshot | None:
    return session.exec(select(UsHoldingSignalSnapshot).where(
        UsHoldingSignalSnapshot.lot_id == lot_id,
        UsHoldingSignalSnapshot.as_of_date < as_of_date,
    ).order_by(UsHoldingSignalSnapshot.as_of_date.desc()).limit(1)).first()


def _decision_was_executed(session: Session, decision_id: str) -> bool:
    decision = session.get(UsExitDecision, decision_id)
    return decision is not None and decision.status == "completed"


def record_holding_signals(
    session: Session,
    *,
    run_id: str,
    as_of_date: str,
    rows: list[dict[str, Any]],
    quote_fetcher: Callable[[str], dict[str, Any]] = fetch_public_quote,
) -> list[UsExitDecision]:
    lots = repository.open_lots(session)
    h6_lots = [lot for lot in lots if lot.rules_version == US_MANUAL_RULES_VERSION and lot.tm_id is not None]
    by_tm: dict[int, dict[str, Any]] = {}
    for row in rows:
        tm_id = as_int(row.get("tmId"))
        if tm_id is None or tm_id in by_tm:
            raise UsManualError("exit_snapshot_contract_error", "持仓退出快照含无效或重复 tmId", 409)
        if row.get("asOfDate") != as_of_date:
            raise UsManualError("exit_snapshot_date_mismatch", "持仓退出快照日期不一致", 409)
        by_tm[tm_id] = row
    wanted = {int(lot.tm_id) for lot in h6_lots}
    if set(by_tm) != wanted:
        raise UsManualError(
            "exit_snapshot_incomplete",
            "持仓退出快照未唯一覆盖所有 H6 开放持仓",
            409,
            {"wanted_tm_ids": sorted(wanted), "returned_tm_ids": sorted(by_tm)},
        )
    contract_hash = sha256({"fields": HOLDING_EXIT_FIELDS, "rules_version": US_MANUAL_RULES_VERSION})
    decisions: list[UsExitDecision] = []
    for lot in h6_lots:
        raw = by_tm[int(lot.tm_id)]
        snapshot = repository.save_holding_snapshot(session, UsHoldingSignalSnapshot(
            snapshot_id=f"us-holding-{lot.lot_id}-{as_of_date.replace('-', '')}-{uuid4().hex[:8]}",
            run_id=run_id,
            as_of_date=as_of_date,
            lot_id=int(lot.lot_id),
            tm_id=int(lot.tm_id),
            ticker_symbol=lot.ticker_symbol,
            temperature_curr=raw.get("trendTemperatureCurr"),
            danger=raw.get("stopwinFlagByDangerSignal"),
            boiling=raw.get("stopwinFlagByBoilingTemperature"),
            champagne=raw.get("stopwinFlagByPopChampagne"),
            status="ready",
            contract_hash=contract_hash,
            raw_json=serialize(raw),
            raw_sha256=sha256(raw),
        ))
        existing_decision = repository.exit_decision_for_snapshot(session, snapshot.snapshot_id)
        if existing_decision is not None:
            decisions.append(existing_decision)
            continue
        venue = _venue_for_lot(session, lot)
        quote: Decimal | None = None
        quote_error: dict[str, Any] | None = None
        if bool(snapshot.boiling) or bool(snapshot.champagne):
            try:
                quote = Decimal(str(quote_fetcher(lot.venue_instrument)["reference_price"]))
            except Exception as exc:
                quote_error = {"type": type(exc).__name__, "message": str(exc)[:300]}
        decision = decide_exit(
            remaining_quantity=lot.remaining_quantity,
            temperature_curr=snapshot.temperature_curr,
            danger=snapshot.danger,
            boiling=snapshot.boiling,
            champagne=snapshot.champagne,
            quantity_precision=venue.get("quantity_precision"),
            min_trade_usdt=venue.get("min_trade_usdt"),
            public_quote_usdt=quote,
        )
        previous = _previous_snapshot(session, int(lot.lot_id), as_of_date)
        missed_danger: dict[str, Any] | None = None
        if previous is not None and previous.danger is True and snapshot.danger is False:
            prior_decision = session.exec(select(UsExitDecision).where(
                UsExitDecision.snapshot_id == previous.snapshot_id,
            ).limit(1)).first()
            if prior_decision is not None and not _decision_was_executed(session, prior_decision.decision_id):
                missed_danger = {
                    "violation": "missed_danger_exit",
                    "expired_without_catchup": True,
                    "prior_decision_id": prior_decision.decision_id,
                }
        signal_hash = sha256({
            "snapshot_id": snapshot.snapshot_id,
            "remaining_quantity": decimal_text(lot.remaining_quantity),
            "decision": serialize(decision),
        })
        row = UsExitDecision(
            decision_id=f"us-exit-decision-{lot.lot_id}-{uuid4().hex[:12]}",
            snapshot_id=snapshot.snapshot_id,
            lot_id=int(lot.lot_id),
            as_of_date=as_of_date,
            action=decision["action"],
            priority=int(decision["priority"]),
            sell_ratio=decision["sell_ratio"],
            remaining_quantity_before=lot.remaining_quantity,
            planned_quantity=decision["planned_quantity"],
            reason_codes=decision["reason_codes"],
            signal_hash=signal_hash,
            status="pending" if decision["action"] not in {"hold", "manual_review"} else decision["action"],
            intended_execution_date=next_us_session(as_of_date),
            evidence_json={
                "snapshot": repository.model_payload(snapshot),
                "venue": venue,
                "public_quote_usdt": decimal_text(quote),
                "quote_error": quote_error,
                "missed_danger": missed_danger,
                "rules": {
                    "full_exit": ["danger", "temperature_flat_or_below"],
                    "partial_each": "25% of pre-execution remaining",
                    "same_day_additive": True,
                    "holding_days_authority": False,
                    "risk_anchor_exit_authority": False,
                },
            },
        )
        decisions.append(repository.save_exit_decision(session, row))
    return decisions


def position_actions(session: Session, *, as_of_date: str | None) -> list[dict[str, Any]]:
    decisions = repository.latest_exit_decisions(session, as_of_date=as_of_date)
    latest_by_lot: dict[int, UsExitDecision] = {}
    for row in decisions:
        latest_by_lot.setdefault(row.lot_id, row)
    output: list[dict[str, Any]] = []
    for lot in repository.open_lots(session):
        if lot.rules_version != US_MANUAL_RULES_VERSION:
            output.append({
                "lot": repository.model_payload(lot),
                "action": "legacy_read_only",
                "reason": "historical_h1_h5_position",
                "priority": 99,
                "legacy_read_only": True,
                "notice": "H1–H5 历史持仓只读；不能按 H6 伪造趋势退出证据。",
            })
            continue
        decision = latest_by_lot.get(int(lot.lot_id))
        if decision is None:
            output.append({
                "lot": repository.model_payload(lot),
                "action": "manual_review",
                "reason": "exit_data_missing",
                "priority": 0,
                "notice": "缺少当日趋势退出字段，请人工查看趋势动物；系统不会默认为持有。",
            })
            continue
        decision_payload = repository.model_payload(decision)
        decision_payload["execution_schedule"] = execution_schedule(
            signal_date=decision.as_of_date,
            intended_execution_date=decision.intended_execution_date,
            generated_at=decision.created_at,
        )
        output.append({
            "lot": repository.model_payload(lot),
            "decision": decision_payload,
            "as_of_date": decision.as_of_date,
            "action": decision.action,
            "reason": ",".join(decision.reason_codes),
            "priority": decision.priority,
            "notice": "只生成 Bitget 手工卖出清单，不发送订单；成交后必须回填。",
        })
    return sorted(output, key=lambda row: (row["priority"], row["lot"]["ticker_symbol"], row["lot"]["lot_id"]))


def mark_stop_triggered(session: Session, *, lot_id: int) -> dict[str, Any]:
    del session, lot_id
    raise UsManualError(
        "structure_stop_retired",
        "H6 前期重要低点只用于反推仓位，不是退出止损；真实退出只看趋势温度纪律",
        410,
    )
