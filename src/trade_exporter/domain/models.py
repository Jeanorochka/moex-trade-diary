from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    name: str
    broker: str
    account_type: str = ""
    status: str = ""
    opened_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Нормализованное исполнение, не зависящее от конкретного брокера."""

    executed_at: datetime
    instrument_id: str
    ticker: str
    instrument_type: str
    side: Side
    quantity: int
    pnl_price: Decimal
    display_price: Decimal
    commission: Decimal
    currency: str = ""


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    exit_at: datetime
    instrument_id: str
    ticker: str
    direction: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    commission: Decimal

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.commission
