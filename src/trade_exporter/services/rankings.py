from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from trade_exporter.domain.portfolio import PortfolioReport
    from trade_exporter.services.analytics import AnalysisReport


MONEY = pl.Decimal(38, 18)
ASSET_LOSSES = "Убыточные активы: закрытия и ВМ"
ASSET_GAINS = "Прибыльные активы: закрытия и ВМ"
TRADE_LOSSES = "Худшие закрытия FIFO"
TRADE_GAINS = "Лучшие закрытия FIFO"
OPEN_LOSSES = "Открытые позиции: убыток"
OPEN_GAINS = "Открытые позиции: прибыль"
UNKNOWN_OPEN = "Открытые позиции: результат неизвестен"
UNKNOWN_ASSETS = "Активы: результат неизвестен"
SCHEMA = {
    "раздел": pl.String,
    "место": pl.UInt32,
    "дата": pl.Date,
    "тикер": pl.String,
    "результат": MONEY,
    "цена входа": MONEY,
    "цена выхода": MONEY,
    "строк результата": pl.UInt32,
    "основа": pl.String,
    "примечание": pl.String,
}
EVENTS = {
    "stock_fifo": "fifo", "stock_closed": "fifo", "fifo_closed": "fifo",
    "закрытие акций fifo": "fifo", "futures_margin": "margin",
    "вариационная маржа и комиссии": "margin", "futures_fee": "future_fee",
    "комиссии фьючерсов": "future_fee", "broker_fee": "broker_fee",
    "денежные расходы и возвраты финам": "broker_fee",
}


def _number(value, *, optional=False):
    if value is None and optional:
        return None
    try:
        amount = Decimal(str(value))
        if value is None or isinstance(value, bool) or not amount.is_finite() or amount.copy_abs() >= Decimal("1e20"):
            raise ValueError
        normalized = amount.quantize(Decimal("1e-18"))
        if normalized != amount:
            raise ValueError
    except (ValueError, InvalidOperation) as error:
        raise ValueError("Рейтинг содержит некорректный денежный результат") from error
    return normalized


def _day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError as error:
        raise ValueError("Рейтинг содержит некорректную дату") from error


def _event(value):
    return EVENTS.get(value.strip().casefold()) if isinstance(value, str) else None


def _row(ticker, amount, basis, note, *, day=None, entry=None, exit=None, count=1, order=0):
    return {
        "дата": day, "тикер": ticker, "результат": amount,
        "цена входа": entry, "цена выхода": exit, "строк результата": count,
        "основа": basis, "примечание": note, "_order": order,
    }


def _rank(rows, negative, positive, limit):
    result = []
    for section, sign in ((negative, -1), (positive, 1)):
        candidates = [row for row in rows if row["результат"] * sign > 0]
        candidates.sort(key=lambda row: (-sign * row["результат"], row["тикер"], row["дата"] or date.min, row["_order"]))
        for place, row in enumerate(candidates[:limit], 1):
            result.append({"раздел": section, "место": place, **{key: value for key, value in row.items() if key != "_order"}})
    return result


def build_rankings(analysis_report: AnalysisReport | None, portfolio_report: PortfolioReport | None = None, limit: int = 10) -> pl.DataFrame:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("Размер рейтинга должен быть положительным целым числом")
    rows = []
    summary = analysis_report.summary if analysis_report is not None else {}
    portfolio_summary = portfolio_report.summary if portfolio_report is not None else {}
    currency = summary.get("base_currency") or (summary.get("portfolio_period") or {}).get("base_currency") or portfolio_summary.get("base_currency") or "RUB"
    with localcontext() as context:
        context.prec = 80
        if analysis_report is not None:
            frame = analysis_report.trades
            if not isinstance(frame, pl.DataFrame) or not {"date", "ticker", "gross_pnl", "net_pnl"} <= set(frame.columns):
                raise ValueError("Для рейтинга требуются строки результата с датой, тикером, gross_pnl и net_pnl")
            events = frame.to_dicts()
            unallocated = summary.get("commission_basis") == "transactions_period" or any(_event(row.get("event_type")) == "broker_fee" for row in events)
            gross = unallocated or summary.get("commission_complete") is False
            basis = f"gross до комиссий, {currency}" if gross else f"net после учтённых комиссий, {currency}"
            fee_note = "Расходы не распределены по активам и закрытиям; net-рейтинг не определён." if unallocated else "Полные расходы не подтверждены; рейтинг по gross." if gross else "Учтены комиссии, записанные в строках результата."
            partial = " Рейтинг частичный: есть исключённые инструменты." if summary.get("result_complete") is False or summary.get("excluded_instruments") else ""
            note = "Историческая себестоимость FIFO не равна прибыли управления за выбранный период. " + fee_note + partial
            groups = defaultdict(list)
            trades = []
            for index, event in enumerate(events, 1):
                kind = _event(event.get("event_type")) if "event_type" in frame.columns else "fifo"
                if kind not in {"fifo", "margin", "future_fee"}:
                    continue
                ticker = event["ticker"]
                if not isinstance(ticker, str) or not ticker.strip():
                    raise ValueError("В рейтинге отсутствует тикер")
                amount = _number(event["gross_pnl"] if gross else event["net_pnl"])
                groups[ticker].append((amount, kind))
                if kind == "fifo":
                    trades.append(_row(
                        ticker, amount, basis, f"Строка результатов №{index}. " + note,
                        day=_day(event["date"]), entry=_number(event.get("entry_price"), optional=True),
                        exit=_number(event.get("exit_price"), optional=True), order=index,
                    ))
            assets = []
            for ticker, values in groups.items():
                asset_note = note if any(kind == "fifo" for _, kind in values) else fee_note + partial
                if any(kind in {"margin", "future_fee"} for _, kind in values):
                    asset_note += " ВМ может относиться к открытым позициям; это не отдельная закрытая сделка."
                assets.append(_row(ticker, _number(sum((amount for amount, _ in values), Decimal(0))), basis, asset_note, count=len(values)))
            rows.extend(_rank(assets, ASSET_LOSSES, ASSET_GAINS, limit))
            rows.extend(_rank(trades, TRADE_LOSSES, TRADE_GAINS, limit))
            for ticker in sorted(set(summary.get("excluded_instruments", ())))[:limit]:
                rows.append({"раздел": UNKNOWN_ASSETS, "место": None, **{key: value for key, value in _row(
                    ticker, None, f"не определён, {currency}", "Инструмент исключён из расчёта P&L; отсутствующий результат не считается нулём.", count=0,
                ).items() if key != "_order"}})
        if portfolio_report is not None:
            positions = portfolio_report.positions
            if not isinstance(positions, pl.DataFrame) or not {"ticker", "unrealized_pnl", "valuation_currency"} <= set(positions.columns):
                raise ValueError("Для рейтинга открытых позиций требуются тикер, unrealized_pnl и valuation_currency")
            current_currency = portfolio_summary.get("base_currency") or currency
            captured = portfolio_summary.get("captured_at")
            known = []
            unknown = []
            for index, position in enumerate(positions.iter_rows(named=True), 1):
                quantity = _number(position.get("quantity"), optional=True)
                ticker = position.get("instrument_id") or position["ticker"]
                pending = position.get("quantity_includes_pending_orders", portfolio_summary.get("positions_include_pending_orders", False))
                if quantity == 0 and not pending:
                    continue
                amount = _number(position["unrealized_pnl"], optional=True)
                reason = ""
                if pending:
                    reason = "Количество может включать неисполненные заявки; сопоставимый открытый результат не подтверждён."
                elif position["valuation_currency"] != current_currency:
                    reason = f"Результат в {position['valuation_currency']} не пересчитан в {current_currency}."
                elif amount is None:
                    reason = "Денежный открытый результат отсутствует; он не считается нулём."
                elif position.get("price_status") == "stale":
                    reason = "Котировка помечена устаревшей; текущий сопоставимый результат не подтверждён."
                note = f"Текущий открытый результат на {captured or 'неуказанную дату'}; не вклад инструмента в прибыль выбранного периода."
                if reason:
                    unknown.append(_row(ticker, None, f"не определён, {current_currency}", reason + " " + note, day=_day(captured)))
                else:
                    known.append(_row(ticker, amount, f"открытый результат, {current_currency}", note, day=_day(captured), order=index))
            rows.extend(_rank(known, OPEN_LOSSES, OPEN_GAINS, limit))
            for row in sorted(unknown, key=lambda item: item["тикер"])[:limit]:
                rows.append({"раздел": UNKNOWN_OPEN, "место": None, **{key: value for key, value in row.items() if key != "_order"}})
    return pl.DataFrame(rows, schema=SCHEMA)
