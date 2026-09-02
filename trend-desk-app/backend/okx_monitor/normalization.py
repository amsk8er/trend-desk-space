from __future__ import annotations

from decimal import Decimal
from typing import Any

from backend.okx_monitor.classification import (
    STABLECOINS, classify_balance, classify_instrument, normalize_position, position_key,
)
from backend.okx_monitor.contracts import PositionView, ProtectionView, decimal_or_none


def normalize_contract_positions(
    rows: list[dict[str, Any]], instrument_map: dict[str, dict[str, Any]],
) -> list[PositionView]:
    result: list[PositionView] = []
    for row in rows:
        inst_id = str(row.get("instId") or "")
        identity = classify_instrument(instrument_map.get(inst_id) or {
            "instId": inst_id, "instType": row.get("instType"),
        })
        normalized = normalize_position(row, identity)
        if normalized is None:
            continue
        side, quantity = normalized
        result.append(PositionView(
            position_key(inst_id, side), identity, side, quantity,
            available_quantity=decimal_or_none(row.get("availPos")),
            avg_price=decimal_or_none(row.get("avgPx")),
            mark_price=decimal_or_none(row.get("markPx")),
            last_price=decimal_or_none(row.get("last")),
            liquidation_price=decimal_or_none(row.get("liqPx")),
            leverage=decimal_or_none(row.get("lever")),
            unrealized_pnl=decimal_or_none(row.get("upl")), raw=row,
        ))
    return result


def normalize_balances(
    account_rows: list[dict[str, Any]],
    instrument_rows: list[dict[str, Any]],
    *,
    dust_notional_usd: Decimal = Decimal("0.01"),
) -> list[PositionView]:
    by_base: dict[str, list[dict[str, Any]]] = {}
    for item in instrument_rows:
        if str(item.get("instType") or "").upper() == "SPOT" and str(item.get("state") or "") == "live":
            by_base.setdefault(str(item.get("baseCcy") or "").upper(), []).append(item)
    details: list[dict[str, Any]] = []
    for account in account_rows:
        if isinstance(account.get("details"), list):
            details.extend(row for row in account["details"] if isinstance(row, dict))
    result: list[PositionView] = []
    for row in details:
        ccy = str(row.get("ccy") or "").upper()
        quantity = decimal_or_none(row.get("cashBal")) or decimal_or_none(row.get("eq")) or Decimal("0")
        if quantity <= 0:
            continue
        instruments = by_base.get(ccy) or []
        preferred = next((item for item in instruments if str(item.get("quoteCcy")) in {"USD", "USDT"}), None)
        if ccy in STABLECOINS:
            identity = classify_balance(ccy)
        elif preferred:
            identity = classify_instrument(preferred)
        else:
            identity = classify_balance(ccy)
        last_price = decimal_or_none(row.get("last"))
        if (
            not identity.is_cash
            and last_price is not None
            and quantity * last_price < dust_notional_usd
        ):
            continue
        result.append(PositionView(
            position_key(identity.inst_id, "long"), identity, "long", quantity,
            available_quantity=decimal_or_none(row.get("availBal")),
            avg_price=decimal_or_none(row.get("avgPx")),
            last_price=last_price, raw=row,
        ))
    return result


def is_spot_dust(position: PositionView, *, dust_notional_usd: Decimal) -> bool:
    """Identify non-cash spot remnants after live prices have been merged."""
    if position.identity.is_cash or position.identity.inst_type != "SPOT":
        return False
    price = position.mark_price or position.last_price
    return price is not None and position.quantity * price < dust_notional_usd


def _order_trigger(row: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    for price_key, type_key in (
        ("slTriggerPx", "slTriggerPxType"), ("triggerPx", "triggerPxType"),
        ("activePx", "triggerPxType"),
    ):
        price = decimal_or_none(row.get(price_key))
        if price is not None and price > 0:
            return price, str(row.get(type_key) or "last")
    return None, None


def normalize_protections(rows: list[dict[str, Any]]) -> list[ProtectionView]:
    result: list[ProtectionView] = []
    for row in rows:
        trigger, trigger_type = _order_trigger(row)
        if trigger is None:
            continue
        inst_id = str(row.get("instId") or "")
        order_id = str(row.get("ordId") or "") or None
        algo_id = str(row.get("algoId") or "") or None
        side = str(row.get("side") or "").lower()
        key = algo_id or order_id or f"{inst_id}:{side}:{trigger}"
        close_fraction = decimal_or_none(row.get("closeFraction"))
        result.append(ProtectionView(
            order_key=key, inst_id=inst_id, side=side,
            order_type=str(row.get("ordType") or "conditional"),
            status=str(row.get("state") or "live"), quantity=decimal_or_none(row.get("sz")),
            trigger_price=trigger, trigger_price_type=trigger_type,
            reduce_only=str(row.get("reduceOnly") or "false").lower() == "true" or close_fraction is not None,
            order_id=order_id, algo_id=algo_id, close_fraction=close_fraction, raw=row,
        ))
    return result


def protection_for_position(position: PositionView, orders: list[ProtectionView]) -> list[ProtectionView]:
    closing_side = "sell" if position.side == "long" else "buy"
    return [row for row in orders if row.inst_id == position.identity.inst_id and row.side == closing_side]


def full_cover(position: PositionView, protection: ProtectionView) -> bool:
    if protection.status not in {"live", "partially_effective", "effective"}:
        return False
    if position.identity.inst_type != "SPOT" and not protection.reduce_only:
        return False
    if protection.close_fraction is not None and protection.close_fraction >= Decimal("1"):
        return True
    return protection.quantity is not None and protection.quantity >= position.quantity
