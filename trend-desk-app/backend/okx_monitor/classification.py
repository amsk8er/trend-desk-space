from __future__ import annotations

from decimal import Decimal
from typing import Any

from backend.okx_monitor.contracts import ProductIdentity, decimal_or_none


STABLECOINS = {
    "USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "USDP", "PYUSD", "USDE", "USDG",
}


def _text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def classify_instrument(row: dict[str, Any]) -> ProductIdentity:
    inst_id = _text(row, "instId")
    inst_type = _text(row, "instType").upper()
    category = _text(row, "instCategory")
    rule_type = _text(row, "ruleType").lower()
    base = _text(row, "baseCcy").upper()
    underlying = _text(row, "uly").upper()

    if inst_type == "SPOT" and category == "3" and base.startswith("X") and len(base) > 1:
        return ProductIdentity(inst_id, inst_type, "us_stock_spot", base[1:], True)
    if inst_type == "FUTURES" and rule_type == "xperp" and category == "3":
        symbol = underlying.split("-")[0] if underlying else inst_id.split("-")[0]
        return ProductIdentity(inst_id, inst_type, "us_stock_xperp", symbol or None, True)
    if inst_type == "SPOT":
        return ProductIdentity(inst_id, inst_type, "crypto_spot", base or inst_id.split("-")[0], False)
    if inst_type in {"SWAP", "FUTURES", "OPTION"}:
        symbol = underlying.split("-")[0] if underlying else inst_id.split("-")[0]
        return ProductIdentity(inst_id, inst_type, "crypto_derivative", symbol or None, False)
    return ProductIdentity(inst_id, inst_type or "UNKNOWN", "unknown", None, False)


def classify_balance(currency: str) -> ProductIdentity:
    ccy = currency.strip().upper()
    cash = ccy in STABLECOINS
    return ProductIdentity(
        inst_id=f"{ccy}-BALANCE",
        inst_type="BALANCE",
        product_kind="cash" if cash else "crypto_spot",
        underlying_symbol=ccy,
        is_us_equity_related=False,
        is_cash=cash,
    )


def position_key(inst_id: str, side: str, *, account: str = "main") -> str:
    return f"{account}:{inst_id}:{side.lower()}"


def normalize_position(row: dict[str, Any], identity: ProductIdentity):
    """Return normalized side and positive quantity, or None for an empty row."""
    quantity = decimal_or_none(row.get("pos")) or Decimal("0")
    pos_side = _text(row, "posSide").lower()
    if pos_side in {"long", "short"}:
        side = pos_side
        quantity = abs(quantity)
    else:
        side = "short" if quantity < 0 else "long"
        quantity = abs(quantity)
    if quantity == 0:
        return None
    return side, quantity


def is_xstock_spot(row: dict[str, Any]) -> bool:
    return classify_instrument(row).product_kind == "us_stock_spot"
