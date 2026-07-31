"""人工成交、现金和散股持仓台账。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from backend.db import UsManualAccountSnapshot, UsManualExecution, UsManualPlanItem, UsPositionLot
from backend.us_manual import repository
from backend.us_manual.contracts import (
    US_MANUAL_RULES_VERSION,
    UsManualError,
    decimal_text,
    parse_decimal,
    serialize,
    utc_now,
)
from backend.us_manual.rules import POLICY


def _gross(price: Decimal, quantity: Decimal) -> Decimal:
    return price * quantity


def _open_cost(lots: list[UsPositionLot]) -> Decimal:
    return sum((lot.remaining_quantity * lot.average_cost_usdt for lot in lots), Decimal("0"))


def account_state(session: Session) -> dict[str, Any]:
    executions = repository.confirmed_executions(session)
    ledger_cash = POLICY["account_usdt"]
    for row in executions:
        if row.side == "buy":
            ledger_cash -= row.gross_usdt + row.fee_usdt
        elif row.side == "sell":
            ledger_cash += row.gross_usdt - row.fee_usdt
        else:
            raise UsManualError("ledger_side_invalid", "本地台账存在无法解释的成交方向")
    lots = repository.open_lots(session)
    by_ticker: dict[str, dict[str, Any]] = {}
    for lot in lots:
        item = by_ticker.setdefault(lot.ticker_symbol, {
            "ticker_symbol": lot.ticker_symbol,
            "ticker_name": lot.ticker_name,
            "quantity": Decimal("0"),
            "cost_usdt": Decimal("0"),
        })
        item["quantity"] += lot.remaining_quantity
        item["cost_usdt"] += lot.remaining_quantity * lot.average_cost_usdt
    risk = sum(
        (max(Decimal("0"), lot.average_cost_usdt - lot.stop_price) * lot.remaining_quantity)
        for lot in lots if lot.stop_price is not None
    )
    cash = ledger_cash
    effective_positions = dict(by_ticker)
    effective_open_cost = _open_cost(lots)
    ocr_snapshot = repository.latest_confirmed_account_ocr_snapshot(session)
    latest_execution_at = max(
        (row.confirmed_at for row in executions if row.confirmed_at is not None),
        default=None,
    )
    ocr_stale = bool(
        ocr_snapshot is not None
        and latest_execution_at is not None
        and ocr_snapshot.confirmed_at < latest_execution_at
    )
    reconciliations: list[dict[str, Any]] = []
    reported_equity: Decimal | None = None
    if ocr_snapshot is not None:
        reported_equity = ocr_snapshot.starting_equity_usdt
    if ocr_snapshot is not None and not ocr_stale:
        cash = min(ledger_cash, ocr_snapshot.cash_usdt)
        observed_rows = (
            ocr_snapshot.derivation_json.get("positions", [])
            if isinstance(ocr_snapshot.derivation_json, dict) else []
        )
        observed: dict[str, dict[str, Any]] = {}
        for raw in observed_rows if isinstance(observed_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            ticker = str(raw.get("ticker_symbol") or "").upper()
            if not ticker:
                continue
            try:
                quantity = Decimal(str(raw.get("quantity")))
                cost = Decimal(str(raw.get("cost_usdt")))
            except (ArithmeticError, ValueError, TypeError):
                continue
            if not quantity.is_finite() or not cost.is_finite() or quantity <= 0 or cost < 0:
                continue
            observed[ticker] = {**raw, "quantity": quantity, "cost_usdt": cost}
        for ticker in sorted(set(by_ticker) | set(observed)):
            local = by_ticker.get(ticker)
            external = observed.get(ticker)
            if local and external:
                quantity_match = local["quantity"] == external["quantity"]
                status = "matched" if quantity_match else "quantity_mismatch"
                effective_positions[ticker] = {
                    **local,
                    "cost_usdt": max(local["cost_usdt"], external["cost_usdt"]),
                    "observed_quantity": external["quantity"],
                    "source": "ledger_and_account_ocr",
                    "reconciliation_status": status,
                }
            elif external:
                status = "ocr_only"
                effective_positions[ticker] = {
                    "ticker_symbol": ticker,
                    "ticker_name": external.get("ticker_name"),
                    "quantity": external["quantity"],
                    "cost_usdt": external["cost_usdt"],
                    "source": "account_ocr_only",
                    "reconciliation_status": status,
                }
            else:
                status = "ledger_only"
                effective_positions[ticker] = {
                    **local,
                    "source": "manual_ledger_only",
                    "reconciliation_status": status,
                }
            reconciliations.append({
                "ticker_symbol": ticker,
                "status": status,
                "ledger_quantity": decimal_text(local["quantity"] if local else None),
                "observed_quantity": decimal_text(external["quantity"] if external else None),
            })
        effective_open_cost = sum(
            (row["cost_usdt"] for row in effective_positions.values()), Decimal("0"),
        )
    elif ocr_snapshot is not None:
        reconciliations.append({
            "status": "snapshot_stale_after_execution",
            "message": "截图确认后已有新成交，容量已回到人工成交台账；请重新上传账户截图。",
        })
    reconciliation_status = "not_uploaded"
    if ocr_snapshot is not None:
        reconciliation_status = "stale" if ocr_stale else (
            "matched" if all(row.get("status") == "matched" for row in reconciliations)
            else "review_required"
        )
    return {
        "starting_equity_usdt": POLICY["account_usdt"],
        "reported_equity_usdt": reported_equity,
        "cash_usdt": cash,
        "ledger_cash_usdt": ledger_cash,
        "open_cost_usdt": effective_open_cost,
        "open_risk_usdt": risk,
        "open_risk_status": (
            "partial_legacy_stop_only" if any(lot.stop_price is None for lot in lots) else "complete"
        ),
        "h6_risk_anchor_is_exit_stop": False,
        "position_count": len(effective_positions),
        "open_positions": [
            {
                **row,
                "quantity": decimal_text(row.get("quantity")),
                "observed_quantity": decimal_text(row.get("observed_quantity")),
                "cost_usdt": decimal_text(row.get("cost_usdt")),
            }
            for _, row in sorted(effective_positions.items())
        ],
        "execution_count": len(executions),
        "account_source": (
            "ledger_plus_confirmed_ocr" if ocr_snapshot is not None and not ocr_stale
            else "manual_ledger"
        ),
        "ocr_snapshot_id": ocr_snapshot.snapshot_id if ocr_snapshot is not None else None,
        "ocr_snapshot_as_of_date": ocr_snapshot.as_of_date if ocr_snapshot is not None else None,
        "ocr_snapshot_stale": ocr_stale,
        "reconciliation_status": reconciliation_status,
        "reconciliations": reconciliations,
    }


def record_account_snapshot(session: Session, *, as_of_date: str) -> UsManualAccountSnapshot:
    state = account_state(session)
    row = UsManualAccountSnapshot(
        as_of_date=as_of_date,
        starting_equity_usdt=state["starting_equity_usdt"],
        cash_usdt=state["cash_usdt"],
        open_cost_usdt=state["open_cost_usdt"],
        open_risk_usdt=state["open_risk_usdt"],
        position_count=state["position_count"],
        derivation_json=serialize(state),
    )
    return repository.save_account_snapshot(session, row)


def _plan_for_item(session: Session, item: UsManualPlanItem):
    return repository.get_plan(session, item.plan_id)


def _item_executed_quantity(session: Session, item_id: int, *, side: str) -> Decimal:
    rows = session.exec(select(UsManualExecution).where(
        UsManualExecution.plan_item_id == item_id,
        UsManualExecution.confirmed.is_(True),
        UsManualExecution.side == side,
    )).all()
    return sum((row.quantity for row in rows), Decimal("0"))


def _payload_values(payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, str, str]:
    price = parse_decimal(payload.get("price_usdt"), field="price_usdt", positive=True)
    quantity = parse_decimal(payload.get("quantity"), field="quantity", positive=True)
    fee = parse_decimal(payload.get("fee_usdt", "0"), field="fee_usdt", non_negative=True)
    trade_date = str(payload.get("trade_date") or "")
    executed_at = str(payload.get("executed_at") or "")
    if not trade_date or not executed_at:
        raise UsManualError("execution_time_required", "成交日期和成交时间均为必填", 422)
    return price, quantity, fee, trade_date, executed_at


def preview_execution(session: Session, *, item_id: int, payload: dict[str, Any],
                      side_override: str | None = None) -> dict[str, Any]:
    item = repository.get_plan_item(session, item_id)
    plan = _plan_for_item(session, item)
    if plan.rules_version != US_MANUAL_RULES_VERSION:
        raise UsManualError(
            "historical_plan_read_only",
            "H1–H5 历史计划只读，不能回填成交或冲正",
            409,
        )
    if plan.status not in {"locked", "partially_executed", "completed"}:
        raise UsManualError("plan_not_locked", "只有已锁定清单可以回填人工成交", 409)
    side = side_override or item.side
    if side not in {"buy", "sell"}:
        raise UsManualError("execution_side_invalid", "计划项成交方向必须是买入或卖出", 422)
    price, quantity, fee, trade_date, executed_at = _payload_values(payload)
    gross = _gross(price, quantity)
    state = account_state(session)
    if side == "buy":
        planned = item.target_quantity
        if planned is None:
            raise UsManualError("planned_quantity_missing", "计划项缺少目标数量", 422)
        filled = _item_executed_quantity(session, item_id, side="buy")
        if filled + quantity > planned:
            raise UsManualError("planned_quantity_exceeded", "本次成交数量超过计划剩余数量", 422,
                                {"planned_quantity": decimal_text(planned), "filled_quantity": decimal_text(filled)})
        open_same_ticker = repository.open_lots(session, ticker_symbol=item.ticker_symbol)
        account_tickers = {
            str(row.get("ticker_symbol") or "").upper()
            for row in state["open_positions"]
        }
        if item.ticker_symbol.upper() in account_tickers and not open_same_ticker:
            raise UsManualError(
                "existing_position_duplicate",
                "账户截图显示已持有同一标的，H6 不允许重复开仓或摊平",
                422,
            )
        for lot in open_same_ticker:
            opened = (
                session.get(UsManualExecution, lot.opened_by_execution_id)
                if lot.opened_by_execution_id is not None else None
            )
            if opened is None or opened.plan_item_id != item.item_id:
                raise UsManualError(
                    "existing_position_duplicate",
                    "已持有同一标的，H6 不允许加仓或摊平",
                    422,
                )
        if not open_same_ticker and state["position_count"] >= int(POLICY["max_open_positions"]):
            raise UsManualError("position_capacity_full", "确认后会超过 20 个不同标的上限", 422)
        # Locked plans reserve cash/notional; replace this item's own remaining
        # reservation with the actual fill before accepting slippage.
        from backend.us_manual.allocation import locked_buy_reservations

        reservations = locked_buy_reservations(session)
        confirmed_gross = sum((row.gross_usdt for row in session.exec(select(UsManualExecution).where(
            UsManualExecution.plan_item_id == item_id,
            UsManualExecution.confirmed.is_(True),
            UsManualExecution.side == "buy",
        )).all()), Decimal("0"))
        own_remaining_reservation = max(
            Decimal("0"), (item.target_notional_usdt or Decimal("0")) - confirmed_gross,
        )
        other_reservations = max(
            Decimal("0"),
            reservations["all_locked_unfilled_usdt"] - own_remaining_reservation,
        )
        if gross + fee + other_reservations > state["cash_usdt"]:
            raise UsManualError("cash_insufficient", "本地实验账户现金不足，已阻断成交回填", 422,
                                {"cash_usdt": decimal_text(state["cash_usdt"]),
                                 "required_usdt": decimal_text(gross + fee + other_reservations)})
        if state["open_cost_usdt"] + gross + other_reservations > POLICY["max_total_notional_usdt"]:
            raise UsManualError(
                "total_notional_capacity_exceeded",
                "确认成交后的持仓成本与其他锁定买入会超过 1000U",
                422,
            )
        cash_after = state["cash_usdt"] - gross - fee
        available = None
    else:
        available = sum((lot.remaining_quantity for lot in repository.open_lots(
            session, ticker_symbol=item.ticker_symbol)), Decimal("0"))
        if quantity > available:
            raise UsManualError("oversell_blocked", "卖出数量超过本地已确认持仓，已阻断", 422,
                                {"available_quantity": decimal_text(available)})
        cash_after = state["cash_usdt"] + gross - fee
    return {
        "preview_only": True,
        "item_id": item_id,
        "plan_id": item.plan_id,
        "side": side,
        "ticker_symbol": item.ticker_symbol,
        "venue_instrument": item.venue_instrument,
        "price_usdt": decimal_text(price),
        "quantity": decimal_text(quantity),
        "fee_usdt": decimal_text(fee),
        "gross_usdt": decimal_text(gross),
        "cash_before_usdt": decimal_text(state["cash_usdt"]),
        "cash_after_usdt": decimal_text(cash_after),
        "available_quantity": decimal_text(available),
        "planned_quantity": decimal_text(item.target_quantity),
        "notice": "预览不会写入现金、持仓或成交台账。确认后仍只记录人工成交，不会向 Bitget 发送订单。",
    }


def _apply_sell_fifo(session: Session, *, ticker_symbol: str, quantity: Decimal) -> None:
    remaining = quantity
    lots = repository.open_lots(session, ticker_symbol=ticker_symbol)
    for lot in lots:
        used = min(lot.remaining_quantity, remaining)
        lot.remaining_quantity -= used
        remaining -= used
        lot.updated_at = utc_now()
        if lot.remaining_quantity == 0:
            lot.status = "closed"
            lot.closed_at = utc_now()
        session.add(lot)
        if remaining == 0:
            break
    if remaining > 0:
        raise UsManualError("oversell_blocked", "卖出数量超过可用持仓", 422)


def _apply_sell_for_item(session: Session, *, item: UsManualPlanItem, quantity: Decimal) -> None:
    if item.exit_decision_id is None:
        _apply_sell_fifo(session, ticker_symbol=item.ticker_symbol, quantity=quantity)
        return
    decision = repository.get_exit_decision(session, item.exit_decision_id)
    lot = repository.get_lot(session, decision.lot_id)
    if lot.status != "open" or lot.ticker_symbol != item.ticker_symbol or quantity > lot.remaining_quantity:
        raise UsManualError("exit_quantity_stale", "退出决定绑定的持仓数量已变化", 409)
    lot.remaining_quantity -= quantity
    lot.updated_at = utc_now()
    if lot.remaining_quantity == 0:
        lot.status = "closed"
        lot.closed_at = utc_now()
    session.add(lot)


def _refresh_item_status(session: Session, item: UsManualPlanItem) -> None:
    side = item.side
    if side not in {"buy", "sell"} or item.target_quantity is None:
        return
    filled = _item_executed_quantity(session, item.item_id or 0, side=side)
    if filled >= item.target_quantity:
        item.status = "completed"
    elif filled > 0:
        item.status = "partially_executed"
    session.add(item)


def confirm_execution(session: Session, *, item_id: int, payload: dict[str, Any],
                      correction_of_id: int | None = None, side_override: str | None = None) -> dict[str, Any]:
    key = str(payload.get("idempotency_key") or "").strip()
    if not key or len(key) > 160:
        raise UsManualError("idempotency_key_required", "请提供长度不超过 160 的幂等键", 422)
    existing = repository.execution_by_idempotency(session, key)
    if existing is not None:
        return {"idempotent_replay": True, "execution": repository.model_payload(existing),
                "account": serialize(account_state(session))}
    preview = preview_execution(session, item_id=item_id, payload=payload, side_override=side_override)
    item = repository.get_plan_item(session, item_id)
    side = str(preview["side"])
    price = parse_decimal(preview["price_usdt"], field="price_usdt", positive=True)
    quantity = parse_decimal(preview["quantity"], field="quantity", positive=True)
    fee = parse_decimal(preview["fee_usdt"], field="fee_usdt", non_negative=True)
    gross = parse_decimal(preview["gross_usdt"], field="gross_usdt", positive=True)
    execution = UsManualExecution(
        plan_item_id=item.item_id,
        correction_of_id=correction_of_id,
        idempotency_key=key,
        trade_date=str(payload["trade_date"]),
        executed_at=str(payload["executed_at"]),
        side=side,
        ticker_symbol=item.ticker_symbol,
        venue_instrument=item.venue_instrument,
        price_usdt=price,
        quantity=quantity,
        fee_usdt=fee,
        gross_usdt=gross,
        source="manual_correction" if correction_of_id is not None else "manual",
        note=str(payload.get("note") or "") or None,
        confirmed=True,
        confirmed_at=utc_now(),
    )
    try:
        session.add(execution)
        session.flush()
        if side == "buy":
            candidate = (
                repository.get_candidate(session, int(item.candidate_id))
                if item.candidate_id is not None else None
            )
            if candidate is None:
                raise UsManualError("candidate_not_found", "H6 买入计划缺少候选证据", 409)
            lot = UsPositionLot(
                ticker_symbol=item.ticker_symbol,
                ticker_name=item.ticker_name,
                asset_type=item.asset_type,
                venue_instrument=item.venue_instrument,
                opened_by_execution_id=execution.execution_id,
                opened_on_data_date=repository.get_plan(session, item.plan_id).signal_date,
                initial_quantity=quantity,
                remaining_quantity=quantity,
                average_cost_usdt=price,
                stop_price=item.stop_price,
                stop_source=item.stop_source,
                stop_suggestion_id=item.stop_suggestion_id,
                stop_review_id=item.stop_review_id,
                stop_anchor_type=item.stop_anchor_type,
                stop_anchor_date=item.stop_anchor_date,
                stop_evidence_json=item.stop_evidence_json,
                tm_id=candidate.tm_id,
                rules_version=US_MANUAL_RULES_VERSION,
                risk_anchor_id=item.risk_anchor_id,
                environment_id=item.environment_id,
            )
            session.add(lot)
        else:
            _apply_sell_for_item(session, item=item, quantity=quantity)
        _refresh_item_status(session, item)
        if side == "sell" and item.exit_decision_id is not None:
            decision = repository.get_exit_decision(session, item.exit_decision_id)
            decision.status = "completed" if item.status == "completed" else "partially_executed"
            session.add(decision)
        session.commit()
        session.refresh(execution)
    except Exception:
        session.rollback()
        raise
    plan = repository.get_plan(session, item.plan_id)
    repository.mark_plan_execution_status(session, plan)
    snapshot = record_account_snapshot(session, as_of_date=execution.trade_date)
    return {
        "idempotent_replay": False,
        "execution": repository.model_payload(execution),
        "account": repository.model_payload(snapshot),
        "notice": "已记录人工确认成交；Trend Desk 没有向 Bitget 发送订单。",
    }


def correct_execution(session: Session, *, execution_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    original = repository.get_execution(session, execution_id)
    if not original.confirmed or original.plan_item_id is None:
        raise UsManualError("correction_not_allowed", "只能冲正确认过且关联计划项的人工成交", 422)
    if original.side not in {"buy", "sell"}:
        raise UsManualError("correction_not_allowed", "原成交方向不支持自动冲正", 422)
    reverse = "sell" if original.side == "buy" else "buy"
    filled_payload = {
        "price_usdt": payload.get("price_usdt", decimal_text(original.price_usdt)),
        "quantity": payload.get("quantity", decimal_text(original.quantity)),
        "fee_usdt": payload.get("fee_usdt", decimal_text(original.fee_usdt)),
        "trade_date": payload.get("trade_date", original.trade_date),
        "executed_at": payload.get("executed_at"),
        "note": payload.get("note", f"冲正 execution_id={execution_id}"),
        "idempotency_key": payload.get("idempotency_key"),
    }
    if not filled_payload["executed_at"]:
        raise UsManualError("execution_time_required", "冲正也必须提供新的成交时间", 422)
    return confirm_execution(session, item_id=original.plan_item_id, payload=filled_payload,
                             correction_of_id=original.execution_id, side_override=reverse)
