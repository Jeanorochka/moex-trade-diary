from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import os

import requests

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account, Fill, Side
from trade_exporter.utils.money import money_currency, quotation_to_decimal


BUY_TYPES = {
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_BUY_CARD",
    "OPERATION_TYPE_BUY_MARGIN",
    "OPERATION_TYPE_DELIVERY_BUY",
}
SELL_TYPES = {
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_SELL_CARD",
    "OPERATION_TYPE_SELL_MARGIN",
    "OPERATION_TYPE_DELIVERY_SELL",
}


class TBankBroker(BrokerAdapter):
    name = "tbank"

    USERS_GET_ACCOUNTS = (
        "tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
    )
    OPERATIONS_BY_CURSOR = (
        "tinkoff.public.invest.api.contract.v1."
        "OperationsService/GetOperationsByCursor"
    )
    FUTURES_MARGIN = (
        "tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/GetFuturesMargin"
    )

    def __init__(self, settings: dict):
        self.settings = settings
        self.base_url = str(
            settings.get("base_url", "https://invest-public-api.tbank.ru/rest")
        ).rstrip("/")
        self.timeout = int(settings.get("request_timeout_seconds", 30))
        self.normalize_futures = bool(
            settings.get("normalize_futures_to_points", True)
        )

        token_env = str(settings.get("token_env", "TBANK_INVEST_TOKEN"))
        self.token = os.getenv(token_env, "").strip()
        if not self.token:
            raise RuntimeError(
                f"Не найден токен T-Bank. Укажи {token_env} в окружении или .env."
            )

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
        )
        self._future_factor_cache: dict[str, Decimal] = {}

    def _post(self, method: str, payload: dict) -> dict:
        response = self.session.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=self.timeout,
            verify=False,
        )
        if not response.ok:
            raise RuntimeError(
                f"T-Bank API {response.status_code}: {response.text}"
            )
        return response.json()

    @staticmethod
    def _iso_utc(dt: datetime) -> str:
        if dt.tzinfo is None:
            raise ValueError("datetime должен быть timezone-aware")
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def list_accounts(self) -> list[Account]:
        data = self._post(
            self.USERS_GET_ACCOUNTS,
            {"status": "ACCOUNT_STATUS_OPEN"},
        )
        result = []
        for item in data.get("accounts", []):
            opened = item.get("openedDate")
            result.append(
                Account(
                    id=str(item.get("id", "")),
                    name=str(item.get("name") or "Без названия"),
                    broker=self.name,
                    account_type=str(item.get("type", "")),
                    status=str(item.get("status", "")),
                    opened_at=self._parse_timestamp(opened) if opened else None,
                )
            )
        return result

    def get_fills(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[Fill]:
        operations = self._load_operations(account_id, date_from, date_to)
        fills: list[Fill] = []

        for operation in operations:
            op_type = str(operation.get("type", ""))
            if op_type in BUY_TYPES:
                side = Side.BUY
            elif op_type in SELL_TYPES:
                side = Side.SELL
            else:
                continue

            quantity_done = int(
                operation.get("quantityDone") or operation.get("quantity") or 0
            )
            if quantity_done <= 0:
                continue

            total_commission = abs(
                quotation_to_decimal(operation.get("commission"))
            )
            commission_per_unit = total_commission / Decimal(quantity_done)

            instrument_id = str(
                operation.get("instrumentUid") or operation.get("figi") or ""
            )
            ticker = str(
                operation.get("ticker")
                or operation.get("figi")
                or instrument_id
                or "UNKNOWN"
            )
            instrument_type = str(operation.get("instrumentType") or "")
            operation_price = operation.get("price") or {}
            currency = money_currency(operation_price)

            trades = ((operation.get("tradesInfo") or {}).get("trades") or [])
            if not trades:
                trades = [
                    {
                        "date": operation.get("date"),
                        "quantity": quantity_done,
                        "price": operation_price,
                    }
                ]

            for trade in trades:
                qty = int(trade.get("quantity") or 0)
                if qty <= 0:
                    continue

                money_price = quotation_to_decimal(trade.get("price"))
                display_price = self._display_price(
                    instrument_id=instrument_id,
                    instrument_type=instrument_type,
                    money_price=money_price,
                )
                executed_at = self._parse_timestamp(
                    str(trade.get("date") or operation.get("date"))
                )

                fills.append(
                    Fill(
                        executed_at=executed_at,
                        instrument_id=instrument_id,
                        ticker=ticker,
                        instrument_type=instrument_type,
                        side=side,
                        quantity=qty,
                        pnl_price=money_price,
                        display_price=display_price,
                        commission=commission_per_unit * Decimal(qty),
                        currency=currency,
                    )
                )

        fills.sort(key=lambda item: item.executed_at)
        return fills

    def _load_operations(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict]:
        cursor = ""
        items: list[dict] = []

        while True:
            payload = {
                "accountId": account_id,
                "from": self._iso_utc(date_from),
                "to": self._iso_utc(date_to),
                "limit": 1000,
                "state": "OPERATION_STATE_EXECUTED",
                "withoutCommissions": False,
                "withoutTrades": False,
                "withoutOvernights": True,
            }
            if cursor:
                payload["cursor"] = cursor

            data = self._post(self.OPERATIONS_BY_CURSOR, payload)
            items.extend(data.get("items", []))

            if not data.get("hasNext"):
                break
            cursor = str(data.get("nextCursor") or "")
            if not cursor:
                break

        return items

    def _display_price(
        self,
        instrument_id: str,
        instrument_type: str,
        money_price: Decimal,
    ) -> Decimal:
        if (
            not self.normalize_futures
            or "future" not in instrument_type.lower()
            or not instrument_id
        ):
            return money_price

        rubles_per_point = self._future_rubles_per_point(instrument_id)
        if rubles_per_point == 0:
            return money_price
        return money_price / rubles_per_point

    def _future_rubles_per_point(self, instrument_id: str) -> Decimal:
        if instrument_id in self._future_factor_cache:
            return self._future_factor_cache[instrument_id]

        data = self._post(self.FUTURES_MARGIN, {"instrumentId": instrument_id})
        min_increment = quotation_to_decimal(data.get("minPriceIncrement"))
        increment_amount = quotation_to_decimal(
            data.get("minPriceIncrementAmount")
        )
        factor = (
            Decimal("1")
            if min_increment == 0
            else increment_amount / min_increment
        )
        self._future_factor_cache[instrument_id] = factor
        return factor
