from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@dataclass(frozen=True)
class ProductIdentity:
    inst_id: str
    inst_type: str
    product_kind: str
    underlying_symbol: str | None
    is_us_equity_related: bool
    is_cash: bool = False


@dataclass(frozen=True)
class PositionView:
    position_key: str
    identity: ProductIdentity
    side: str
    quantity: Decimal
    available_quantity: Decimal | None = None
    avg_price: Decimal | None = None
    mark_price: Decimal | None = None
    last_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtectionView:
    order_key: str
    inst_id: str
    side: str
    order_type: str
    status: str
    quantity: Decimal | None
    trigger_price: Decimal | None
    trigger_price_type: str | None
    reduce_only: bool
    order_id: str | None = None
    algo_id: str | None = None
    close_fraction: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfirmedBar:
    closed_at: datetime
    close: Decimal
