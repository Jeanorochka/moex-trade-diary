from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from time import monotonic, sleep
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import primp

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account, Fill, Side
from trade_exporter.domain.portfolio import CashBalance, PortfolioPosition, PortfolioSnapshot


HISTORY_START = datetime(2026, 1, 26, tzinfo=ZoneInfo("Europe/Moscow"))
TRADE_LIMIT = 1000
PAGE_SIZE = 100
SIDES = {"1": Side.BUY, "2": Side.SELL}
FUTURES = {"FUTURE", "FUTURES"}


def _amount(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except InvalidOperation:
        pass
    raise RuntimeError(f"БКС: некорректное числовое поле {field}")


def _rows(data: dict, field: str) -> list[dict]:
    rows = data.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"БКС: некорректный список {field}")
    return rows


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("БКС: дата должна содержать часовой пояс")
    return value.astimezone(timezone.utc)


class BcsBroker(BrokerAdapter):
    name = "bcs"
    AUTH = "trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"
    PORTFOLIO = "trade-api-bff-portfolio/api/v1/portfolio"
    TRADES = "trade-api-bff-trade-details/api/v1/trades/search"

    def __init__(self, settings: dict):
        self.base_url = str(settings.get("base_url", "https://be.broker.ru")).rstrip("/")
        endpoint = urlsplit(self.base_url)
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
            raise ValueError("БКС: base_url должен быть HTTPS-адресом API")
        token_env = str(settings.get("token_env", "B.INVEST_TOKEN"))
        self.token = os.getenv(token_env, "").strip()
        if not self.token:
            raise RuntimeError(f"БКС: укажите refresh-токен для чтения в {token_env} в окружении или .env.")
        self.configured_account = str(settings.get("account_id") or "").strip()
        self.session = primp.Client(timeout=int(settings.get("request_timeout_seconds", 30)), follow_redirects=False)
        self._jwt = ""
        self._expires_at = 0.0
        self._next_request = 0.0
        self._portfolio_rows: list[dict] | None = None
        self._account_id = ""
        self.fill_quality: tuple[str, ...] = ()
        self.commission_quality: tuple[str, ...] = ()

    def _request(self, method: str, path: str, **options) -> dict:
        wait = self._next_request - monotonic()
        if wait > 0:
            sleep(wait)
        self._next_request = monotonic() + 0.55
        try:
            response = getattr(self.session, method)(f"{self.base_url}/{path}", **options)
        except Exception:
            raise RuntimeError("БКС: запрос к Trade API не выполнен.") from None
        if response.status_code != 200:
            suffix = " Проверьте refresh-токен для чтения." if path == self.AUTH else ""
            raise RuntimeError(f"БКС API: HTTP {response.status_code}.{suffix}")
        try:
            data = json.loads(response.content, parse_float=Decimal)
        except (UnicodeError, ValueError):
            raise RuntimeError("БКС: API вернул некорректный JSON.") from None
        if not isinstance(data, dict):
            raise RuntimeError("БКС: API вернул неожиданный формат ответа.")
        return data

    def _api(self, method: str, path: str, **options) -> dict:
        if not self._jwt or monotonic() >= self._expires_at:
            data = self._request(
                "post", self.AUTH,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "client_id": "trade-api-read",
                    "grant_type": "refresh_token",
                    "refresh_token": self.token,
                }).encode(),
            )
            access = data.get("access_token")
            if not isinstance(access, str) or not access.strip():
                raise RuntimeError("БКС: в ответе авторизации отсутствует access-токен.")
            expires_in = _amount(data.get("expires_in"), "expires_in")
            self._jwt = access
            self._expires_at = monotonic() + max(0, float(expires_in) - 5)
        return self._request(method, path, headers={"Authorization": f"Bearer {self._jwt}"}, **options)

    def _read_portfolio(self) -> list[dict]:
        if self._portfolio_rows is None:
            data = self._api("get", self.PORTFOLIO)
            rows = _rows(data, "positions")
            accounts = {
                str(row["agreementId"]).strip() for row in [data, *rows]
                if row.get("agreementId") is not None and str(row["agreementId"]).strip()
            }
            if len(accounts) > 1 or (self.configured_account and accounts and self.configured_account not in accounts):
                raise RuntimeError("БКС: ответ API относится к другому счёту.")
            account_id = next(iter(accounts), self.configured_account)
            if not account_id:
                raise RuntimeError(
                    "БКС: API не вернул номер счёта. Для пустого портфеля укажите account_id в секции [bcs] файла config.toml."
                )
            self._account_id = account_id
            self._portfolio_rows = rows
        return self._portfolio_rows

    def list_accounts(self) -> list[Account]:
        self._read_portfolio()
        return [Account(self._account_id, "Брокерский счёт БКС", self.name)]

    def _check_account(self, account_id: str) -> None:
        self._read_portfolio()
        if account_id != self._account_id:
            raise ValueError("БКС: токен привязан к другому счёту.")

    def _valued_positions(self) -> dict[tuple[str, ...], tuple[int, dict]]:
        positions = {}
        for row in self._read_portfolio():
            key = tuple(str(row.get(field) or "") for field in
                        ("subAccountId", "account", "board", "exchange", "ticker", "type", "instrumentType", "currency"))
            term = str(row.get("term") or "T0").upper().removeprefix("T").removeprefix("Т").removeprefix("+")
            if not term.isdecimal():
                raise RuntimeError("БКС: в портфеле неизвестный срок расчётов.")
            rank = int(term)
            previous = positions.get(key)
            if previous is None or previous[0] < rank:
                positions[key] = (rank, row)
            elif previous[0] == rank and previous[1] != row:
                raise RuntimeError("БКС: в портфеле противоречивые данные по одной позиции.")
        return positions

    def get_portfolio_snapshot(self, account_id: str, captured_at=None) -> PortfolioSnapshot:
        self._check_account(account_id)
        positions = []
        cash: dict[str, Decimal] = defaultdict(Decimal)
        equity = Decimal(0)
        for _, row in self._valued_positions().values():
            ticker = str(row.get("ticker") or "").strip()
            kind = str(row.get("instrumentType") or row.get("type") or "").upper()
            currency = str(row.get("currency") or "").strip().upper().replace("RUR", "RUB")
            quantity = _amount(row.get("quantity"), "quantity")
            value = _amount(row.get("currentValueRub"), "currentValueRub")
            equity += value
            if kind in {"MONEY", "CASH"} or (kind == "CURRENCY" and len(ticker) == 3):
                cash[ticker.upper().replace("RUR", "RUB") if len(ticker) == 3 else currency] += quantity
                continue
            if not quantity:
                continue
            board = str(row.get("board") or row.get("exchange") or "").strip()
            average_price = row.get("balancePrice")
            current_price = row.get("currentPrice")
            # The response can include the same instrument across separate subaccounts.
            subaccount = str(row.get("subAccountId") or row.get("account") or "")
            positions.append(PortfolioPosition(
                instrument_id=f"{board}:{ticker}:{subaccount}:{kind}:{currency}",
                ticker=ticker, asset_type=kind.lower(), quantity=quantity,
                average_price=_amount(average_price, "balancePrice") if average_price is not None else None,
                current_price=_amount(current_price, "currentPrice") if current_price is not None else None,
                price_currency=currency, market_value=value,
                valuation_source="БКС Trade API: текущая стоимость позиции в RUB",
            ))
        return PortfolioSnapshot(
            account_id, self.name, captured_at or datetime.now(timezone.utc), "RUB", equity,
            tuple(CashBalance(currency, amount) for currency, amount in cash.items()),
            tuple(positions), "БКС Trade API: сумма оценок в RUB по последним срокам расчётов",
        )

    def get_fills(self, account_id: str, date_from: datetime, date_to: datetime) -> list[Fill]:
        self._check_account(account_id)
        first, end = _utc(date_from), _utc(date_to)
        reasons = []
        if first < HISTORY_START:
            reasons.append("БКС отдаёт сделки с 26.01.2026; предыдущие входы в позиции могут отсутствовать.")
        self.fill_quality = tuple(reasons)
        self.commission_quality = (
            "История сделок БКС не содержит комиссий. Торговый результат показан до комиссий.",
        )
        start = max(first, _utc(HISTORY_START))
        if end <= start:
            return []
        query = {
            "startDateTime": start.isoformat().replace("+00:00", "Z"),
            "endDateTime": end.isoformat().replace("+00:00", "Z"),
        }
        fills: dict[tuple[str, str, str], Fill] = {}
        unsupported = set()
        page = 0
        row_count = 0
        while True:
            params = urlencode({"page": page, "size": PAGE_SIZE, "sort": "tradeDateTime,desc"})
            data = self._api("post", f"{self.TRADES}?{params}", json=query)
            rows = _rows(data, "records")
            pages, total = data.get("totalPages"), data.get("totalRecords")
            if type(pages) is not int or type(total) is not int or min(pages, total) < 0:
                raise RuntimeError("БКС: история сделок содержит некорректные параметры страниц.")
            if not rows and page < pages:
                raise RuntimeError("БКС: при выгрузке получена пустая страница сделок.")
            row_count += len(rows)
            for row in rows:
                timestamp = _utc(datetime.fromisoformat(str(row.get("tradeDateTime", "")).replace("Z", "+00:00")))
                if not start <= timestamp < end:
                    continue
                side = SIDES.get(str(row.get("side")))
                ticker = str(row.get("ticker") or "").strip()
                board = str(row.get("classCode") or "").strip()
                kind = str(row.get("instrumentType") or "").upper()
                trade_num = row.get("tradeNum")
                if not ticker or not board or not kind or side is None or trade_num is None:
                    raise RuntimeError("БКС: в сделке отсутствует обязательное поле.")
                quantity = _amount(row.get("tradeQuantity"), "tradeQuantity")
                if quantity <= 0 or quantity != quantity.to_integral_value():
                    raise RuntimeError(f"БКС: журнал поддерживает только целое положительное количество ({ticker}).")
                currency = str(row.get("settlementCurrency") or row.get("priceCurrency") or "").upper().replace("RUR", "RUB")
                display = _amount(row.get("price"), "price")
                volume = row.get("volume")
                if kind in FUTURES or volume is None:
                    unsupported.add(f"{board}:{ticker}")
                    currency = ""
                pnl_price = display if not currency else abs(_amount(volume, "volume")) / quantity
                fill = Fill(timestamp, f"{board}:{ticker}", ticker, kind.lower(), side, int(quantity),
                            pnl_price, display, Decimal(0), currency)
                key = (board, ticker, str(trade_num))
                if key in fills:
                    raise RuntimeError("БКС: повторная сделка в ответе API. Повторите выгрузку.")
                fills[key] = fill
            page += 1
            if page >= pages or row_count >= TRADE_LIMIT:
                break
        if total >= TRADE_LIMIT or row_count >= TRADE_LIMIT:
            reasons.append("Достигнут лимит API БКС в 1000 сделок; более ранние исполнения могут отсутствовать.")
        elif row_count != total:
            raise RuntimeError("БКС: количество строк изменилось при выгрузке. Повторите выгрузку.")
        if unsupported:
            reasons.append("Денежная цена не подтверждена для инструментов: " + ", ".join(sorted(unsupported)) + ".")
        self.fill_quality = tuple(reasons)
        return sorted(fills.values(), key=lambda item: item.executed_at)
