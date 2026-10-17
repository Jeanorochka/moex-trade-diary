from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import polars as pl


def _decimal(value, field: str, *, optional=False) -> Decimal | None:
    if value is None and optional:
        return None
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        if not result.is_finite():
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field}: требуется конечное число") from error
    return result


def _timestamp(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}: требуется время с часовым поясом")


def _currency(value: str, field: str, *, optional=False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: требуется валюта")
    return value.strip().upper()


@dataclass(frozen=True, slots=True)
class CashBalance:
    currency: str
    amount: Decimal

    def __post_init__(self):
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))
        object.__setattr__(self, "amount", _decimal(self.amount, "amount"))


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    instrument_id: str
    ticker: str
    asset_type: str
    quantity: Decimal
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    price_currency: str = "RUB"
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    cost_basis: Decimal | None = None
    valuation_currency: str = "RUB"
    price_as_of: datetime | None = None
    valuation_source: str = ""
    unrealized_source: str = ""
    average_price_currency: str = ""
    quantity_includes_pending_orders: bool | None = None

    def __post_init__(self):
        for name in ("instrument_id", "ticker", "asset_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}: требуется непустая строка")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))
        for name in ("average_price", "current_price", "market_value", "unrealized_pnl", "cost_basis"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, optional=True))
        for name in ("price_currency", "valuation_currency"):
            object.__setattr__(self, name, _currency(getattr(self, name), name))
        object.__setattr__(self, "average_price_currency", _currency(self.average_price_currency, "average_price_currency", optional=True))
        if self.price_as_of is not None:
            _timestamp(self.price_as_of, "price_as_of")
        if self.cost_basis is not None and self.quantity and self.cost_basis * self.quantity < 0:
            raise ValueError("cost_basis: знак должен соответствовать направлению позиции")
        if self.quantity_includes_pending_orders is not None and not isinstance(self.quantity_includes_pending_orders, bool):
            raise ValueError("quantity_includes_pending_orders должен быть bool или None")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    account_id: str
    broker: str
    captured_at: datetime
    base_currency: str
    equity: Decimal
    cash: tuple[CashBalance, ...] = ()
    positions: tuple[PortfolioPosition, ...] = ()
    source: str = ""
    positions_include_pending_orders: bool = False

    def __post_init__(self):
        _timestamp(self.captured_at, "captured_at")
        if not self.account_id or not self.broker:
            raise ValueError("Требуются account_id и broker")
        object.__setattr__(self, "base_currency", _currency(self.base_currency, "base_currency"))
        object.__setattr__(self, "equity", _decimal(self.equity, "equity"))
        object.__setattr__(self, "cash", tuple(self.cash))
        object.__setattr__(self, "positions", tuple(self.positions))
        if any(not isinstance(value, CashBalance) for value in self.cash):
            raise ValueError("cash должен содержать CashBalance")
        if any(not isinstance(value, PortfolioPosition) for value in self.positions):
            raise ValueError("positions должен содержать PortfolioPosition")
        identifiers = [position.instrument_id for position in self.positions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Позиции одного инструмента должны быть объединены до анализа")


@dataclass(frozen=True, slots=True)
class CashFlow:
    timestamp: datetime
    amount: Decimal
    currency: str
    kind: str = "external"
    id: str = ""

    def __post_init__(self):
        _timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "amount", _decimal(self.amount, "amount"))
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))
        if self.kind not in {"external", "income", "fee", "tax", "other"}:
            raise ValueError("kind: ожидается external, income, fee, tax или other")
        if not isinstance(self.id, str):
            raise ValueError("id должен быть строкой")


@dataclass(frozen=True, slots=True)
class PortfolioReport:
    summary: dict
    positions: pl.DataFrame
    allocation: pl.DataFrame
    cash: pl.DataFrame
    flows: pl.DataFrame
