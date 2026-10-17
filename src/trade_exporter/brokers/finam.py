from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math
import os
import re
import time
from urllib.parse import quote

import primp

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account, Fill, Side


class FinamBroker(BrokerAdapter):
    name = "finam"

    def __init__(self, settings: dict):
        self.settings = settings
        self.base_url = str(settings.get("base_url", "https://api.finam.ru/v1")).rstrip("/")
        self.timeout = int(settings.get("request_timeout_seconds", 30))
        self.request_interval = float(settings.get("request_interval_seconds", 0.32))
        if not math.isfinite(self.request_interval) or self.request_interval < 0:
            raise ValueError("Finam: request_interval_seconds должен быть конечным неотрицательным числом")
        self._last_request = None
        self.trade_limit = min(int(settings.get("history_limit", settings.get("trade_limit", 50))), 50)
        self.history_window_days = int(settings.get("history_batch_days", settings.get("history_window_days", 30)))
        if self.timeout <= 0 or self.trade_limit <= 0 or self.history_window_days <= 0:
            raise ValueError("Finam: timeout, trade_limit и history_window_days должны быть положительными")
        token_env = str(settings.get("token_env", "FINAM_API_TOKEN"))
        self.token = str(settings.get("token") or os.getenv(token_env, "")).strip()
        if not self.token and token_env == "FINAM_API_TOKEN":
            self.token = os.getenv("FINAM_TOKEN", "").strip()
        if not self.token:
            raise RuntimeError(f"Не найден токен Finam. Укажи {token_env} в окружении или .env.")
        is_jwt = self.token.startswith("eyJ") and self.token.count(".") == 2
        self._secret = None if is_jwt else self.token
        self._jwt = self.token if is_jwt else None
        self._expires_at: datetime | None = None
        self.session = primp.Client(
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
            # 2026 itogi
            verify=False,
        )
        self._asset_cache: dict[tuple[str, str], dict] = {}
        self._account_positions: dict[str, list[dict]] = {}
        raw_multipliers = settings.get("price_multipliers", {})
        if not isinstance(raw_multipliers, dict):
            raise ValueError("Finam price_multipliers должен быть таблицей symbol@MIC -> множитель")
        self._price_multipliers = {}
        for symbol, value in raw_multipliers.items():
            multiplier = self._decimal(value, f"price_multipliers.{symbol}")
            if multiplier <= 0 or "@" not in symbol:
                raise ValueError("Finam price_multipliers: нужен symbol@MIC и положительный множитель")
            self._price_multipliers[symbol] = multiplier

    @staticmethod
    def _timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"Finam: некорректная дата {value!r}") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError(f"Finam: дата без часового пояса {value!r}")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime должен быть timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @classmethod
    def _timestamp_key(cls, value: str) -> tuple[datetime, int]:
        parsed = cls._timestamp(value)
        match = re.search(r"\.(\d+)(?:Z|[+-]\d{2}:\d{2})$", value)
        fraction = match.group(1) if match else ""
        if len(fraction) > 9:
            raise RuntimeError("Finam: точность времени превышает наносекунды")
        return parsed, int(fraction.ljust(9, "0")) % 1000

    @staticmethod
    def _decimal(value, field: str) -> Decimal:
        if isinstance(value, dict):
            value = value.get("value")
        try:
            if value is None or isinstance(value, bool):
                raise ValueError
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError) as error:
            raise RuntimeError(f"Finam: некорректное число в {field}") from error
        return result

    def _error(self, response) -> RuntimeError:
        message = response.text
        for token in (self.token, self._jwt):
            if token:
                message = message.replace(token, "***")
        return RuntimeError(f"Finam API {response.status_code}: {message}")

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request is not None:
            delay = self.request_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
        self._last_request = time.monotonic()

    def _authenticate(self) -> None:
        if not self._secret:
            raise RuntimeError("JWT Finam истёк или недействителен. Укажи новый JWT или API-токен.")
        self._pace()
        response = self.session.post(
            f"{self.base_url}/sessions",
            json={"secret": self._secret},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise self._error(response)
        data = response.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("Finam: в ответе авторизации отсутствует JWT")
        self._jwt = token.strip()
        self._expires_at = None

    def _request(self, method: str, path: str, *, params=None, token_details=False, json=None) -> dict:
        if not self._jwt or (
            self._secret
            and self._expires_at is not None
            and self._expires_at <= datetime.now(timezone.utc) + timedelta(seconds=30)
        ):
            self._authenticate()
        refreshed = False
        limited = 0
        while True:
            options = {"timeout": self.timeout}
            if not token_details:
                options["headers"] = {"Authorization": f"Bearer {self._jwt}"}
            if params is not None:
                options["params"] = params
            if json is not None:
                options["json"] = json
            if token_details:
                options["json"] = {"token": self._jwt}
            self._pace()
            response = getattr(self.session, method.lower())(f"{self.base_url}/{path}", **options)
            if response.status_code == 401 and not refreshed and self._secret:
                self._authenticate()
                refreshed = True
                continue
            if response.status_code == 429 and limited < 3:
                headers = {str(key).lower(): str(value) for key, value in getattr(response, "headers", {}).items()}
                try:
                    delay = float(headers.get("retry-after", "60"))
                except ValueError:
                    delay = 60
                time.sleep(delay if math.isfinite(delay) and 0 <= delay <= 60 else 60)
                limited += 1
                continue
            if response.status_code >= 400:
                raise self._error(response)
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Finam: API вернул JSON без объекта")
            return data

    def list_accounts(self) -> list[Account]:
        details = self._request("POST", "sessions/details", token_details=True)
        expires = details.get("expires_at")
        if expires:
            self._expires_at = self._timestamp(expires)
        account_ids = details.get("account_ids", [])
        if not isinstance(account_ids, list) or any(not isinstance(item, str) or not item for item in account_ids):
            raise RuntimeError("Finam: некорректный список account_ids")
        accounts = []
        for account_id in dict.fromkeys(account_ids):
            data = self._request("GET", f"accounts/{quote(account_id, safe='')}")
            if data.get("account_id", account_id) != account_id:
                raise RuntimeError("Finam: получен другой счёт вместо запрошенного")
            opened = data.get("open_account_date") or data.get("first_trade_date")
            positions = data.get("positions", [])
            if not isinstance(positions, list) or any(not isinstance(item, dict) for item in positions):
                raise RuntimeError("Finam: некорректный список позиций счёта")
            self._account_positions[account_id] = positions
            accounts.append(Account(
                id=account_id,
                name=str(data.get("name") or f"Finam {account_id}"),
                broker=self.name,
                account_type=str(data.get("type") or ""),
                status=str(data.get("status") or ""),
                opened_at=self._timestamp(opened) if opened else None,
            ))
        return accounts

    def _load_window(self, account_id: str, start: datetime, end: datetime) -> list[dict]:
        data = self._request("GET", f"accounts/{quote(account_id, safe='')}/trades", params={
            "limit": self.trade_limit,
            "interval.start_time": self._iso(start),
            "interval.end_time": self._iso(end),
        })
        trades = data.get("trades", [])
        if not isinstance(trades, list) or any(not isinstance(item, dict) for item in trades):
            raise RuntimeError("Finam: некорректный список сделок")
        for trade in trades:
            executed_at = self._timestamp(trade.get("timestamp"))
            if not start <= executed_at < end:
                raise RuntimeError("Finam вернул сделку за пределами запрошенного интервала")
            if trade.get("account_id") != account_id:
                raise RuntimeError("Finam вернул сделку другого счёта")
        if len(trades) < self.trade_limit:
            return trades
        middle = start + (end - start) / 2
        if middle <= start or middle >= end:
            raise RuntimeError("Finam: лимит истории внутри одной микросекунды; полная выгрузка не подтверждена")
        return self._load_window(account_id, start, middle) + self._load_window(account_id, middle, end)

    def _asset(self, account_id: str, symbol: str) -> dict:
        key = (account_id, symbol)
        if key not in self._asset_cache:
            data = self._request("GET", f"assets/{quote(symbol, safe='')}", params={"account_id": account_id})
            if not isinstance(data.get("type"), str) or not data["type"].strip():
                raise RuntimeError(f"Finam: неизвестный тип инструмента {symbol}")
            ticker, mic = symbol.rsplit("@", 1)
            if data.get("ticker", ticker) != ticker or data.get("mic", mic) != mic:
                raise RuntimeError(f"Finam: метаданные не соответствуют {symbol}")
            self._asset_cache[key] = data
        return self._asset_cache[key]

    def _price_factor(self, symbol: str, asset: dict, executed_at: datetime) -> Decimal:
        instrument_type = asset["type"]
        kind = instrument_type.lower()
        special = any(word in kind for word in ("future", "option", "bond", "фьючер", "опцион", "облига"))
        multiplier = self._price_multipliers.get(symbol)
        if special and multiplier is None:
            raise RuntimeError(f"Finam: для {symbol} ({instrument_type}) задай подтверждённый price_multipliers; денежная цена не определена")
        return multiplier or Decimal("1")

    def _price_currency(self, symbol: str, asset: dict, executed_at: datetime, trade_currency: str) -> str:
        return trade_currency

    def _fill(self, account_id: str, trade: dict) -> Fill:
        symbol = trade.get("symbol")
        if not isinstance(symbol, str) or "@" not in symbol or not all(symbol.rsplit("@", 1)):
            raise RuntimeError("Finam: ожидается инструмент symbol@MIC")
        side = {"SIDE_BUY": Side.BUY, "SIDE_SELL": Side.SELL}.get(trade.get("side"))
        if side is None:
            raise RuntimeError(f"Finam: неизвестная сторона сделки {trade.get('side')!r}")
        quantity = self._decimal(trade.get("size"), "size")
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise RuntimeError(f"Finam: количество {quantity} не является положительным целым числом штук")
        price = self._decimal(trade.get("price"), "price")
        currency = str(trade.get("currency") or "").upper()
        if not currency:
            raise RuntimeError("Finam: отсутствует валюта исторической сделки")
        fee = trade.get("commission")
        if not isinstance(fee, dict):
            raise RuntimeError("Finam: в исторической сделке отсутствует комиссия")
        commission = self._decimal(fee.get("value"), "commission.value").copy_abs()
        fee_currency = str(fee.get("currency") or "").upper()
        asset = self._asset(account_id, symbol)
        instrument_type = asset["type"]
        executed_at = self._timestamp(trade.get("timestamp"))
        multiplier = self._price_factor(symbol, asset, executed_at)
        currency = self._price_currency(symbol, asset, executed_at, currency).upper()
        if commission and fee_currency != currency:
            raise RuntimeError("Finam: валюта комиссии отличается от валюты цены; требуется пересчёт валют")
        accrued = trade.get("accrued_interest")
        if accrued is not None and self._decimal(accrued, "accrued_interest") != 0:
            raise RuntimeError(f"Finam: {symbol} содержит НКД; расчёт P&L с НКД не поддерживается")
        with localcontext() as context:
            context.prec = 80
            pnl_price = price * multiplier
        return Fill(
            executed_at=executed_at,
            instrument_id=symbol,
            ticker=str(asset.get("ticker") or symbol.rsplit("@", 1)[0]),
            instrument_type=instrument_type,
            side=side,
            quantity=int(quantity),
            pnl_price=pnl_price,
            display_price=price,
            commission=commission,
            currency=currency.lower(),
        )

    def load_trades(self, account_id: str, date_from: datetime, date_to: datetime) -> list[dict]:
        start, end = self._utc(date_from), self._utc(date_to)
        if start > end:
            raise ValueError("Начало периода должно быть раньше окончания")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("Finam: требуется account_id")
        raw = []
        while start < end:
            stop = min(start + timedelta(days=self.history_window_days), end)
            raw.extend(self._load_window(account_id, start, stop))
            start = stop
        unique = {}
        for trade in raw:
            trade_id = trade.get("trade_id")
            if not isinstance(trade_id, str) or not trade_id:
                raise RuntimeError("Finam: отсутствует trade_id, полнота истории не подтверждена")
            key = (trade.get("symbol"), trade_id)
            if key in unique and unique[key] != trade:
                raise RuntimeError("Finam: противоречивые данные для одного trade_id")
            unique.setdefault(key, trade)
        return sorted(unique.values(), key=lambda trade: self._timestamp_key(trade["timestamp"]))

    def get_fills(self, account_id: str, date_from: datetime, date_to: datetime) -> list[Fill]:
        raw = self.load_trades(account_id, date_from, date_to)
        fills = [self._fill(account_id, trade) for trade in raw]
        if len({fill.currency for fill in fills}) > 1:
            raise RuntimeError("Finam: история содержит разные валюты; общий P&L без пересчёта валют недопустим")
        return sorted(fills, key=lambda fill: fill.executed_at)
