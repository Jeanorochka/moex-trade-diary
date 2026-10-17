from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from urllib.parse import quote

from trade_exporter.domain.portfolio import (
    CashBalance,
    CashFlow,
    PortfolioPosition,
    PortfolioSnapshot,
)


ZERO = Decimal("0")
OPERATIONS = "tinkoff.public.invest.api.contract.v1.OperationsService/"
FINAM_CATEGORIES = {
    0: "OTHERS", 1: "DEPOSIT", 2: "WITHDRAW", 5: "INCOME", 7: "COMMISSION",
    8: "TAX", 9: "INHERITANCE", 11: "TRANSFER", 12: "CONTRACT_TERMINATION",
    13: "OUTCOMES", 15: "FINE", 19: "LOAN",
}
TBANK_TYPES = {
    0: "UNSPECIFIED", 1: "INPUT", 2: "BOND_TAX", 3: "OUTPUT_SECURITIES",
    4: "OVERNIGHT", 5: "TAX", 6: "BOND_REPAYMENT_FULL", 7: "SELL_CARD",
    8: "DIVIDEND_TAX", 9: "OUTPUT", 10: "BOND_REPAYMENT", 11: "TAX_CORRECTION",
    12: "SERVICE_FEE", 13: "BENEFIT_TAX", 14: "MARGIN_FEE", 15: "BUY",
    16: "BUY_CARD", 17: "INPUT_SECURITIES", 18: "SELL_MARGIN", 19: "BROKER_FEE",
    20: "BUY_MARGIN", 21: "DIVIDEND", 22: "SELL", 23: "COUPON", 24: "SUCCESS_FEE",
    25: "DIVIDEND_TRANSFER", 26: "ACCRUING_VARMARGIN", 27: "WRITING_OFF_VARMARGIN",
    28: "DELIVERY_BUY", 29: "DELIVERY_SELL", 30: "TRACK_MFEE", 31: "TRACK_PFEE",
    32: "TAX_PROGRESSIVE", 33: "BOND_TAX_PROGRESSIVE", 34: "DIVIDEND_TAX_PROGRESSIVE",
    35: "BENEFIT_TAX_PROGRESSIVE", 36: "TAX_CORRECTION_PROGRESSIVE",
    37: "TAX_REPO_PROGRESSIVE", 38: "TAX_REPO", 39: "TAX_REPO_HOLD",
    40: "TAX_REPO_REFUND", 41: "TAX_REPO_HOLD_PROGRESSIVE", 42: "TAX_REPO_REFUND_PROGRESSIVE",
    43: "DIV_EXT", 44: "TAX_CORRECTION_COUPON", 45: "CASH_FEE", 46: "OUT_FEE",
    47: "OUT_STAMP_DUTY", 50: "OUTPUT_SWIFT", 51: "INPUT_SWIFT",
    53: "OUTPUT_ACQUIRING", 54: "INPUT_ACQUIRING", 55: "OUTPUT_PENALTY",
    56: "ADVICE_FEE", 57: "TRANS_IIS_BS", 58: "TRANS_BS_BS", 59: "OUT_MULTI",
    60: "INP_MULTI", 61: "OVER_PLACEMENT", 62: "OVER_COM", 63: "OVER_INCOME",
    64: "OPTION_EXPIRATION", 65: "FUTURE_EXPIRATION",
}
TBANK_INPUT = {"INPUT", "INPUT_SWIFT", "INPUT_ACQUIRING", "INP_MULTI"}
TBANK_OUTPUT = {"OUTPUT", "OUTPUT_SWIFT", "OUTPUT_ACQUIRING", "OUT_MULTI"}
TBANK_SECURITY_TRANSFERS = {"INPUT_SECURITIES", "OUTPUT_SECURITIES", "TRANS_IIS_BS", "TRANS_BS_BS"}
TBANK_TRADES = {"BUY", "SELL", "BUY_MARGIN", "SELL_MARGIN", "DELIVERY_BUY", "DELIVERY_SELL"}
TBANK_INCOME = {"OVERNIGHT", "OVER_INCOME", "DIVIDEND", "COUPON", "DIVIDEND_TRANSFER"}
TBANK_OTHER = {
    "BOND_REPAYMENT_FULL", "BOND_REPAYMENT", "ACCRUING_VARMARGIN",
    "WRITING_OFF_VARMARGIN", "OVER_PLACEMENT", "OPTION_EXPIRATION", "FUTURE_EXPIRATION",
}


@dataclass(frozen=True, slots=True)
class CashFlowHistory:
    flows: tuple[CashFlow, ...]
    complete: bool
    reasons: tuple[str, ...]
    started_at: datetime | None


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}: требуется время с часовым поясом")
    return value.astimezone(timezone.utc)


def _timestamp(value, field: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")), field)
    except ValueError as error:
        raise RuntimeError(f"Портфель: некорректное время {field}") from error


def _decimal(value, field: str, *, optional=False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
    except (ValueError, InvalidOperation) as error:
        raise RuntimeError(f"Портфель: некорректное число {field}") from error
    return result


def _currency(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{3}", value):
        raise RuntimeError(f"Портфель: отсутствует или некорректна валюта {field}")
    return value.upper()


def _base(broker) -> str:
    return _currency(broker.settings.get("base_currency", "RUB"), "base_currency")


def _quotation(value, field: str) -> Decimal:
    if not isinstance(value, dict):
        raise RuntimeError(f"Портфель: отсутствует {field}")
    units = _decimal(value.get("units", 0), f"{field}.units")
    nanos = _decimal(value.get("nanos", value.get("nano", 0)), f"{field}.nanos")
    if units != units.to_integral_value() or nanos != nanos.to_integral_value() or abs(nanos) >= 1_000_000_000:
        raise RuntimeError(f"Портфель: некорректный формат {field}")
    if units and nanos and (units < 0) != (nanos < 0):
        raise RuntimeError(f"Портфель: противоречивый знак {field}")
    with localcontext() as context:
        context.prec = 80
        return units + nanos / Decimal("1000000000")


def _money(value, field: str) -> tuple[Decimal, str]:
    amount = _quotation(value, field)
    currency = _currency(value.get("currency_code", value.get("currency")), field)
    return amount, currency


def _rows(data, field: str) -> list[dict]:
    if not isinstance(data, dict):
        raise RuntimeError("Портфель: API вернул ответ без объекта")
    values = data.get(field, [])
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise RuntimeError(f"Портфель: API вернул некорректный список {field}")
    return values


def _cash(values: list[dict], field: str) -> tuple[CashBalance, ...]:
    balances = defaultdict(lambda: ZERO)
    for value in values:
        amount, currency = _money(value, field)
        with localcontext() as context:
            context.prec = 80
            balances[currency] += amount
    return tuple(CashBalance(currency, amount) for currency, amount in sorted(balances.items()))


def _future(kind: str) -> bool:
    return "future" in kind.lower() or "фьючер" in kind.lower()


def _share(kind: str) -> bool:
    return kind.lower() in {"share", "shares", "equities", "stock", "stocks", "etf", "etfs", "fund", "funds"}


def _finam_pending_orders(broker, account_id: str) -> set[str] | None:
    terminal_numbers = {3, 5, 9, 13, 16, 19, 20, 22, 23, 28, 31}
    terminal_names = {
        "FILLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED", "DENIED_BY_BROKER",
        "REJECTED_BY_EXCHANGE", "EXECUTED", "DISABLED", "SL_EXECUTED", "TP_EXECUTED",
    }
    try:
        data = broker._request("GET", f"accounts/{quote(account_id, safe='')}/orders")
        pending = set()
        for order in _rows(data, "orders"):
            status = order.get("status")
            if isinstance(status, int) and not isinstance(status, bool):
                terminal = status in terminal_numbers
            elif str(status).isdigit():
                terminal = int(status) in terminal_numbers
            else:
                terminal = str(status).removeprefix("ORDER_STATUS_") in terminal_names
            if terminal:
                continue
            details = order.get("order") or order.get("sltp_order")
            symbol = details.get("symbol") if isinstance(details, dict) else None
            if not isinstance(symbol, str) or "@" not in symbol or not all(symbol.rsplit("@", 1)):
                return None
            pending.add(symbol)
        return pending
    except Exception:
        return None


def _finam_portfolio(broker, account, captured_at: datetime | None) -> PortfolioSnapshot:
    orders_before = _finam_pending_orders(broker, account.id)
    data = broker._request("GET", f"accounts/{quote(account.id, safe='')}")
    if data.get("account_id") != account.id:
        raise RuntimeError("Finam: получен портфель другого счёта")
    orders_after = _finam_pending_orders(broker, account.id)
    unknown_orders = orders_before is None or orders_after is None
    pending_symbols = (orders_before or set()) | (orders_after or set())
    pending_orders = unknown_orders or bool(pending_symbols)
    cache = getattr(broker, "_portfolio_account_data", {})
    cache[account.id] = data
    broker._portfolio_account_data = cache
    captured_at = _utc(captured_at or datetime.now(timezone.utc), "captured_at")
    base = _base(broker)
    is_mct = "portfolio_mct" in data or str(data.get("type", "")).upper() == "MCT"
    if is_mct and ("base_currency" not in broker.settings or base != "USD"):
        raise RuntimeError("Finam: для американского счёта укажи base_currency=USD как валюту учёта; API не пересчитывает equity")
    if not is_mct and base != "RUB":
        raise RuntimeError("Finam: валюта учёта MC/FORTS — RUB; base_currency не выполняет пересчёт equity")
    positions = []
    for raw in _rows(data, "positions"):
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or "@" not in symbol or not all(symbol.rsplit("@", 1)):
            raise RuntimeError("Finam: отсутствует symbol@MIC позиции")
        asset = broker._asset(account.id, symbol)
        kind = str(asset.get("type") or "")
        if not kind:
            raise RuntimeError(f"Finam: отсутствует тип позиции {symbol}")
        position_pending = unknown_orders or symbol in pending_symbols
        price_currency = _currency(
            raw.get("current_price_currency") or asset.get("quote_currency") or base,
            f"{symbol}.current_price_currency",
        )
        average_currency = raw.get("average_price_currency") or price_currency
        average_currency = _currency(average_currency, f"{symbol}.average_price_currency")
        unrealized = _decimal(raw.get("unrealized_pnl"), f"{symbol}.unrealized_pnl", optional=True)
        if price_currency != base or (raw.get("average_price") is not None and average_currency != price_currency):
            unrealized = None
        positions.append(PortfolioPosition(
            instrument_id=symbol,
            ticker=str(asset.get("ticker") or symbol.rsplit("@", 1)[0]),
            asset_type=kind,
            quantity=_decimal(raw.get("quantity"), f"{symbol}.quantity"),
            average_price=_decimal(raw.get("average_price"), f"{symbol}.average_price", optional=True),
            current_price=_decimal(raw.get("current_price"), f"{symbol}.current_price", optional=True),
            price_currency=price_currency,
            average_price_currency=average_currency,
            market_value=ZERO if _future(kind) else None,
            unrealized_pnl=unrealized,
            valuation_currency=base,
            valuation_source=(
                "Finam GetAccount: количество может включать неисполненные заявки; стоимость из количества не выводится"
                if position_pending else "Finam GetAccount; до и после снимка не обнаружены активные заявки по инструменту"
            ),
            unrealized_source=(
                "Finam Position.unrealized_pnl; валюта принята равной базовой валюте MOEX-счёта"
                if unrealized is not None else "Finam: денежная доходность в базовой валюте не определена"
            ),
            quantity_includes_pending_orders=position_pending,
        ))
    return PortfolioSnapshot(
        account.id, broker.name, captured_at, base,
        _decimal(data.get("equity"), "Finam.equity"),
        _cash(_rows(data, "cash"), "Finam.cash"), tuple(positions),
        source="Finam GetAccount.equity; валюта учёта счёта=" + base + (
            " из настроек, без конвертации" if "base_currency" in broker.settings else " по умолчанию для MOEX"
        ),
        positions_include_pending_orders=pending_orders,
    )


def _tbank_portfolio(broker, account, captured_at: datetime | None) -> PortfolioSnapshot:
    base = _base(broker)
    if base not in {"RUB", "USD", "EUR"}:
        raise RuntimeError("T-Bank: GetPortfolio поддерживает базовую валюту RUB, USD или EUR")
    data = broker._post(OPERATIONS + "GetPortfolio", {"accountId": account.id, "currency": base})
    balances = broker._post(OPERATIONS + "GetPositions", {"accountId": account.id})
    for payload in (data, balances):
        if not isinstance(payload, dict) or payload.get("accountId", account.id) != account.id:
            raise RuntimeError("T-Bank: получен портфель другого счёта")
    if balances.get("limitsLoadingInProgress"):
        raise RuntimeError("T-Bank: загрузка позиций ещё не завершена, повтори запрос позже")
    equity, equity_currency = _money(data.get("totalAmountPortfolio"), "T-Bank.totalAmountPortfolio")
    if equity_currency != base:
        raise RuntimeError("T-Bank: валюта стоимости портфеля не совпадает с запрошенной")
    captured_at = _utc(captured_at or datetime.now(timezone.utc), "captured_at")
    positions = []
    for raw in _rows(data, "positions"):
        kind = str(raw.get("instrumentType") or "")
        if kind == "currency":
            continue
        identity = raw.get("instrumentUid") or raw.get("figi") or raw.get("positionUid")
        if not isinstance(identity, str) or not identity or not kind:
            raise RuntimeError("T-Bank: отсутствует идентификатор или тип позиции")
        quantity = _quotation(raw.get("quantity"), f"{identity}.quantity")
        current = raw.get("currentPrice")
        average = raw.get("averagePositionPriceFifo") or raw.get("averagePositionPrice")
        price, price_currency = _money(current, f"{identity}.currentPrice") if current is not None else (None, base)
        average_price, average_currency = _money(average, f"{identity}.averagePositionPrice") if average is not None else (None, "")
        value = cost = None
        with localcontext() as context:
            context.prec = 80
            if _future(kind):
                value = ZERO
            elif _share(kind):
                value = quantity * price if price is not None else None
                if average_price is not None and average_currency == price_currency:
                    cost = quantity * average_price
        expected = raw.get("expectedYieldFifo") if "expectedYieldFifo" in raw else raw.get("expectedYield")
        unrealized = _quotation(expected, f"{identity}.expectedYield") if expected is not None and current is not None else None
        positions.append(PortfolioPosition(
            instrument_id=identity, ticker=str(raw.get("ticker") or raw.get("figi") or identity),
            asset_type=kind, quantity=quantity, average_price=average_price, current_price=price,
            price_currency=price_currency, average_price_currency=average_currency,
            market_value=value, cost_basis=cost, unrealized_pnl=unrealized,
            valuation_currency=price_currency,
            valuation_source=(
                "T-Bank: активная стоимость фьючерса равна 0; номинал и varMargin не прибавляются к equity"
                if _future(kind) else "T-Bank GetPortfolio.currentPrice × quantity; количество в штуках"
            ),
            unrealized_source="T-Bank GetPortfolio.expectedYieldFifo/expectedYield; денежная доходность в валюте инструмента",
        ))
    return PortfolioSnapshot(
        account.id, broker.name, captured_at, base, equity,
        _cash(_rows(balances, "money"), "T-Bank.money"), tuple(positions),
        source="T-Bank GetPortfolio.totalAmountPortfolio; GetPositions.money без повторного добавления отдельно переданного blocked",
    )


def load_portfolio(broker, account, captured_at=None) -> PortfolioSnapshot:
    if broker.name == "finam":
        return _finam_portfolio(broker, account, captured_at)
    if broker.name == "tbank":
        return _tbank_portfolio(broker, account, captured_at)
    raise ValueError(f"Портфель брокера {broker.name!r} не поддерживается")


def _finam_transactions(broker, account_id: str, start: datetime, end: datetime) -> list[dict]:
    limit = min(int(broker.settings.get("transaction_limit", 50)), 50)
    if limit <= 0:
        raise ValueError("Finam: transaction_limit должен быть положительным")
    iso = lambda value: value.isoformat().replace("+00:00", "Z")
    data = broker._request("GET", f"accounts/{quote(account_id, safe='')}/transactions", params={
        "limit": limit, "interval.start_time": iso(start), "interval.end_time": iso(end),
    })
    items = _rows(data, "transactions")
    selected = []
    for item in items:
        executed_at = _timestamp(item.get("timestamp"), "Finam.transaction.timestamp")
        if not start <= executed_at <= end:
            raise RuntimeError("Finam: транзакция вне запрошенного интервала, полнота истории не подтверждена")
        if item.get("account_id", account_id) != account_id:
            raise RuntimeError("Finam: транзакция другого счёта")
        if executed_at < end:
            selected.append(item)
    if len(items) < limit:
        return selected
    middle = start + (end - start) / 2
    if not start < middle < end:
        raise RuntimeError("Finam: лимит транзакций внутри одной микросекунды, полнота истории не подтверждена")
    return _finam_transactions(broker, account_id, start, middle) + _finam_transactions(broker, account_id, middle, end)


def _tbank_operations(broker, account_id: str, start: datetime, end: datetime) -> list[dict]:
    cursor = ""
    cursors = set()
    items = []
    while True:
        payload = {
            "accountId": account_id, "from": start.isoformat().replace("+00:00", "Z"),
            "to": end.isoformat().replace("+00:00", "Z"), "limit": 1000,
            "state": "OPERATION_STATE_EXECUTED", "withoutCommissions": False,
            "withoutTrades": False, "withoutOvernights": False,
        }
        if cursor:
            payload["cursor"] = cursor
        data = broker._post(OPERATIONS + "GetOperationsByCursor", payload)
        for item in _rows(data, "items"):
            executed_at = _timestamp(item.get("date"), "T-Bank.operation.date")
            if not start <= executed_at <= end:
                raise RuntimeError("T-Bank: операция вне запрошенного интервала")
            if item.get("brokerAccountId", account_id) != account_id:
                raise RuntimeError("T-Bank: операция другого счёта")
            if item.get("state") not in {"OPERATION_STATE_EXECUTED", 1}:
                raise RuntimeError("T-Bank: в истории присутствует операция с неподтверждённым исполнением")
            if executed_at < end:
                items.append(item)
        has_next = data.get("hasNext", False)
        if not isinstance(has_next, bool):
            raise RuntimeError("T-Bank: некорректный признак продолжения истории")
        if not has_next:
            return items
        cursor = data.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise RuntimeError("T-Bank: отсутствует или повторяется курсор, полнота истории не подтверждена")
        cursors.add(cursor)


def _enum(value, mapping: dict[int, str], prefix="") -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return mapping.get(value, str(value))
    raw = str(value or "").upper()
    if raw.isdigit():
        return mapping.get(int(raw), raw)
    return raw.removeprefix(prefix)


def _finam_flow(item: dict, base: str, reasons: list[str]) -> CashFlow:
    category = _enum(item.get("transaction_category", item.get("category", "OTHERS")), FINAM_CATEGORIES)
    if item.get("change") is None and category == "TRANSFER":
        amount, currency = ZERO, base
    else:
        amount, currency = _money(item.get("change"), "Finam.transaction.change")
    kind = "other"
    if category in {"DEPOSIT", "WITHDRAW"}:
        if (category == "DEPOSIT" and amount < 0) or (category == "WITHDRAW" and amount > 0):
            raise RuntimeError("Finam: знак ввода/вывода противоречит категории транзакции")
        kind = "external"
    elif category == "INCOME":
        kind = "income"
    elif category in {"COMMISSION", "OUTCOMES", "FINE"}:
        kind = "fee"
    elif category == "TAX":
        kind = "tax"
    elif category == "TRANSFER":
        quantity = _decimal(item.get("change_qty"), "Finam.transaction.change_qty", optional=True)
        if quantity:
            reasons.append("Finam: перевод ценных бумаг требует оценки внешнего потока")
        elif amount:
            reasons.append("Finam: денежный TRANSFER не различает внутренний и внешний перевод")
        else:
            reasons.append("Finam: TRANSFER с нулевым денежным изменением не подтверждает отсутствие перевода бумаг")
    elif category == "OTHERS" and isinstance(item.get("trade"), dict):
        pass
    elif category == "OTHERS" and re.search(
        r"вариацион|фандинг|funding|покупк|продаж|сделк|конверт|своп|репо|процент",
        str(item.get("transaction_name") or ""), flags=re.IGNORECASE,
    ):
        pass
    elif amount or item.get("change_qty"):
        reasons.append(f"Finam: категория транзакции {category or 'не указана'} не классифицирована как внутренний или внешний поток")
    if kind == "external" and currency != base:
        reasons.append(f"Внешний поток в {currency} не пересчитан в {base}")
    return CashFlow(_timestamp(item.get("timestamp"), "Finam.transaction.timestamp"), amount, currency, kind, item["id"])


def _tbank_flow(item: dict, base: str, reasons: list[str]) -> CashFlow:
    category = _enum(item.get("type"), TBANK_TYPES, "OPERATION_TYPE_")
    security_transfer = category in TBANK_SECURITY_TRANSFERS
    if item.get("payment") is None and security_transfer:
        amount, currency = ZERO, base
    else:
        amount, currency = _money(item.get("payment"), "T-Bank.operation.payment")
    kind = "other"
    if category in TBANK_INPUT | TBANK_OUTPUT:
        if (category in TBANK_INPUT and amount < 0) or (category in TBANK_OUTPUT and amount > 0):
            raise RuntimeError("T-Bank: знак ввода/вывода противоречит типу операции")
        kind = "external"
    elif security_transfer:
        reasons.append("T-Bank: перевод ценных бумаг требует оценки внешнего потока")
    elif category in {"BUY_CARD", "SELL_CARD", "DIV_EXT"}:
        reasons.append(f"T-Bank: {category} совмещает операцию с внешним переводом и требует сверки")
    elif category in TBANK_INCOME:
        kind = "income"
    elif "TAX" in category:
        kind = "tax"
    elif "FEE" in category or category in {"OVER_COM", "OUTPUT_PENALTY", "OUT_STAMP_DUTY"}:
        kind = "fee"
    elif category not in TBANK_TRADES | TBANK_OTHER and amount:
        reasons.append(f"T-Bank: тип операции {category or 'не указан'} не классифицирован как внутренний или внешний поток")
    if kind == "external" and currency != base:
        reasons.append(f"Внешний поток в {currency} не пересчитан в {base}")
    return CashFlow(_timestamp(item.get("date"), "T-Bank.operation.date"), amount, currency, kind, item["id"])


def load_cash_flows(broker, account, start, end, *, require_inception=True) -> CashFlowHistory:
    end = _utc(end, "end")
    if broker.name not in {"finam", "tbank"}:
        raise ValueError(f"Потоки брокера {broker.name!r} не поддерживаются")
    if account.opened_at is None and require_inception:
        return CashFlowHistory(
            (), False, ("Неизвестна дата открытия счёта; полнота внешних потоков не подтверждена",),
            _utc(start, "start") if start is not None else None,
        )
    start = _utc(start if start is not None else account.opened_at, "start")
    if start >= end:
        raise ValueError("Начало истории потоков должно быть раньше окончания")
    reasons = []
    opened = _utc(account.opened_at, "account.opened_at") if account.opened_at is not None else None
    if require_inception and start > opened:
        reasons.append("История потоков начинается позже открытия счёта")
    if broker.name == "finam":
        metadata = getattr(broker, "_portfolio_account_data", {}).get(account.id)
        if metadata is None:
            metadata = broker._request("GET", f"accounts/{quote(account.id, safe='')}")
        if not isinstance(metadata, dict) or metadata.get("account_id") != account.id:
            raise RuntimeError("Finam: получены данные другого счёта при проверке начала истории")
        actual_opened = metadata.get("open_account_date")
        if require_inception and actual_opened is None:
            reasons.append("Finam: API не подтвердил дату открытия счёта; дата первой сделки не заменяет историю пополнений")
        elif require_inception and start > _timestamp(actual_opened, "Finam.open_account_date"):
            reasons.append("Finam: история потоков начинается позже подтверждённого открытия счёта")
        first_non_trade = metadata.get("first_non_trade_date")
        if require_inception and first_non_trade and start > _timestamp(first_non_trade, "Finam.first_non_trade_date"):
            reasons.append("Finam: история пропускает первые неторговые операции счёта")
        raw = _finam_transactions(broker, account.id, start, end)
        first_trade = metadata.get("first_trade_date")
        if first_non_trade:
            first_non_trade_at = _timestamp(first_non_trade, "Finam.first_non_trade_date")
            if start <= first_non_trade_at < end and not any(
                _timestamp(item.get("timestamp"), "Finam.transaction.timestamp") <= first_non_trade_at
                for item in raw
            ):
                reasons.append("Finam: API не вернул первые неторговые операции; начальное финансирование до первой сделки не подтверждено")
        elif first_trade and start < _timestamp(first_trade, "Finam.first_trade_date"):
            reasons.append("Finam: Transactions может начинаться с первой сделки; дата начальных неторговых операций не подтверждена API")
    else:
        raw = _tbank_operations(broker, account.id, start, end)
    unique = {}
    for item in raw:
        identity = item.get("id")
        if not isinstance(identity, str) or not identity:
            raise RuntimeError(f"{broker.name}: отсутствует идентификатор денежной операции, полнота истории не подтверждена")
        if identity in unique and unique[identity] != item:
            raise RuntimeError(f"{broker.name}: противоречивые данные одной денежной операции")
        unique.setdefault(identity, item)
    base = _base(broker)
    transform = _finam_flow if broker.name == "finam" else _tbank_flow
    flows = [transform(item, base, reasons) for item in unique.values()]
    flows.sort(key=lambda flow: flow.timestamp)
    return CashFlowHistory(tuple(flows), not reasons, tuple(dict.fromkeys(reasons)), start)
