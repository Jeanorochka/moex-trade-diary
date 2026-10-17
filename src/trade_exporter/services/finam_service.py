from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from trade_exporter.brokers.finam_reports import FinamReports
from trade_exporter.exporters.excel import write_workbook
from trade_exporter.services.analytics import AnalysisReport, analyze_results
from trade_exporter.services.finam_report import parse_finam_margins
from trade_exporter.services.matcher import FifoMatcher


ZERO = Decimal("0")
RESULT_SCHEMA = {
    "date": pl.Date,
    "ticker": pl.String,
    "entry_price": pl.String,
    "exit_price": pl.String,
    "gross_pnl": pl.String,
    "commission": pl.String,
    "event_type": pl.String,
}
EXECUTION_SCHEMA = {
    "дата": pl.String,
    "время": pl.String,
    "инструмент": pl.String,
    "сторона": pl.String,
    "количество": pl.Int64,
    "цена исполнения": pl.Float64,
    "комиссия": pl.Float64,
    "валюта цены": pl.String,
    "валюта комиссии": pl.String,
    "идентификатор сделки": pl.String,
}


def _money_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 80
        return str(value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN))


def _label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _future(asset: dict) -> bool:
    kind = str(asset.get("type", "")).lower()
    return "future" in kind or "фьючер" in kind


def _display(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Decimal)
    )


def _execution(broker, raw: dict, display_timezone) -> dict:
    symbol = raw["symbol"]
    timestamp = broker._timestamp(raw.get("timestamp"))
    side = {"SIDE_BUY": "BUY", "SIDE_SELL": "SELL"}.get(raw.get("side"))
    if side is None:
        raise RuntimeError(f"Finam: неизвестная сторона исполнения {symbol}")
    quantity = broker._decimal(raw.get("size"), "size")
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise RuntimeError(f"Finam: количество {symbol} должно быть положительным целым числом штук")
    price = broker._decimal(raw.get("price"), "price")
    currency = str(raw.get("currency") or "").upper()
    fee = raw.get("commission")
    if not isinstance(fee, dict):
        raise RuntimeError(f"Finam: отсутствует комиссия исполнения {symbol}")
    commission = broker._decimal(fee.get("value"), "commission.value").copy_abs()
    fee_currency = str(fee.get("currency") or "").upper()
    if not currency or (commission and not fee_currency):
        raise RuntimeError(f"Finam: отсутствует валюта исполнения или комиссии {symbol}")
    local = timestamp.astimezone(display_timezone)
    return {
        "дата": local.strftime("%d.%m.%Y"),
        "время": local.isoformat(),
        "инструмент": symbol,
        "сторона": side,
        "количество": int(quantity),
        "цена исполнения": float(price),
        "комиссия": float(commission),
        "валюта цены": currency,
        "валюта комиссии": fee_currency,
        "идентификатор сделки": raw["trade_id"],
    }


def _closed_rows(closed, display_timezone) -> pl.DataFrame:
    rows = []
    for trade in closed:
        rows.append({
            "дата": trade.exit_at.astimezone(display_timezone).strftime("%d.%m.%y"),
            "тикер": trade.instrument_id,
            "цена входа": float(trade.entry_price),
            "цена выхода": float(trade.exit_price),
            "финрез": float(trade.gross_pnl),
            "комиссия": float(trade.commission),
            "направление": trade.direction,
            "количество": trade.quantity,
            "финрез net": float(trade.net_pnl),
        })
    return pl.DataFrame(rows, schema={
        "дата": pl.String, "тикер": pl.String, "цена входа": pl.Float64,
        "цена выхода": pl.Float64, "финрез": pl.Float64, "комиссия": pl.Float64,
        "направление": pl.String, "количество": pl.Int64, "финрез net": pl.Float64,
    })


def _margin_rows(margins: pl.DataFrame, assets: dict[str, dict], first_day: date, last_day: date) -> list[dict]:
    aliases: dict[str, set[str]] = defaultdict(set)
    for symbol, asset in assets.items():
        for value in (symbol, symbol.rsplit("@", 1)[0], asset.get("ticker"), asset.get("name")):
            if isinstance(value, str) and value.strip():
                aliases[_label(value)].add(symbol)
    result = []
    for row in margins.iter_rows(named=True):
        day = row["date"]
        if not first_day <= day <= last_day:
            continue
        ticker = row["ticker"]
        candidates = aliases.get(_label(ticker), set())
        if len(candidates) != 1:
            raise RuntimeError(f"Finam: инструмент отчёта {ticker!r} нельзя однозначно сопоставить с историей исполнений")
        if str(row["currency"]).upper() != "RUB":
            raise RuntimeError("Finam: вариационная маржа отчёта должна быть в RUB")
        result.append({**row, "ticker": next(iter(candidates))})
    return result


def execute_finam(
    broker,
    exporter,
    account,
    period_from: datetime,
    period_to_exclusive: datetime,
    display_timezone,
    starting_capital: Decimal = ZERO,
    reconstruction_mode: str = "full",
    lookback_days: int = 30,
) -> tuple[Path, AnalysisReport, list[Path]]:
    if isinstance(display_timezone, str):
        display_timezone = ZoneInfo(display_timezone)
    if period_from.tzinfo is None or period_to_exclusive.tzinfo is None:
        raise ValueError("datetime должен быть timezone-aware")
    if period_from >= period_to_exclusive:
        raise ValueError("Начало периода должно быть раньше окончания")
    if reconstruction_mode not in {"full", "lookback"}:
        raise ValueError("Неизвестный режим восстановления истории")
    if lookback_days < 0:
        raise ValueError("lookback_days не может быть отрицательным")
    if reconstruction_mode == "full" and account.opened_at is None:
        raise ValueError("Finam: для полной истории нужна дата открытия счёта или первой сделки; выбери lookback явно")
    history_from = (
        account.opened_at
        if reconstruction_mode == "full" and account.opened_at is not None
        else period_from - timedelta(days=lookback_days)
    )
    raw = broker.load_trades(account.id, history_from, period_to_exclusive)
    assets = {}
    future_positions = set()
    positions = getattr(broker, "_account_positions", {}).get(account.id, [])
    for position in positions:
        quantity = broker._decimal(position.get("quantity"), "positions.quantity")
        if quantity == 0:
            continue
        symbol = position.get("symbol")
        if not isinstance(symbol, str) or "@" not in symbol:
            raise RuntimeError("Finam: позиция должна содержать инструмент symbol@MIC")
        asset = broker._asset(account.id, symbol)
        assets[symbol] = asset
        if _future(asset):
            future_positions.add(symbol)
    stock_fills = []
    futures = []
    stock_currencies = defaultdict(set)
    execution_rows = []
    future_fees: dict[tuple[date, str], Decimal] = defaultdict(lambda: ZERO)
    for trade in raw:
        symbol = trade.get("symbol")
        if not isinstance(symbol, str) or "@" not in symbol:
            raise RuntimeError("Finam: ожидается инструмент symbol@MIC")
        if symbol not in assets:
            assets[symbol] = broker._asset(account.id, symbol)
        asset = assets[symbol]
        timestamp = broker._timestamp(trade.get("timestamp"))
        selected = period_from <= timestamp < period_to_exclusive
        if _future(asset):
            futures.append(trade)
        else:
            fill = broker._fill(account.id, trade)
            stock_fills.append(fill)
            stock_currencies[symbol].add(fill.currency.upper())
        if selected:
            execution_rows.append(_execution(broker, trade, display_timezone))
            if _future(asset):
                fee = trade["commission"]
                commission = broker._decimal(fee.get("value"), "commission.value").copy_abs()
                if commission and str(fee.get("currency") or "").upper() != "RUB":
                    raise RuntimeError("Finam: комиссии фьючерсов должны быть в RUB")
                key = (timestamp.astimezone(display_timezone).date(), symbol)
                with localcontext() as context:
                    context.prec = 80
                    future_fees[key] += commission

    closed = [
        trade for trade in FifoMatcher().match(stock_fills)
        if period_from <= trade.exit_at < period_to_exclusive
    ]
    events = []
    for trade in closed:
        if stock_currencies[trade.instrument_id] != {"RUB"}:
            raise RuntimeError("Finam: закрытые сделки с акциями должны быть в RUB для общего результата")
        events.append({
            "date": trade.exit_at.astimezone(display_timezone).date(),
            "ticker": trade.instrument_id,
            "entry_price": _money_text(trade.entry_price),
            "exit_price": _money_text(trade.exit_price),
            "gross_pnl": _money_text(trade.gross_pnl),
            "commission": _money_text(trade.commission),
            "event_type": "stock_fifo",
        })

    report_paths = []
    margin_rows = []
    has_futures = bool(futures or future_positions)
    if has_futures:
        local_start = period_from.astimezone(display_timezone)
        local_end = period_to_exclusive.astimezone(display_timezone)
        if local_start.time() != time() or local_end.time() != time():
            raise ValueError("Finam: результат фьючерсов по отчётам доступен за полные календарные дни")
        baseline = datetime.combine(local_start.date() - timedelta(days=1), time(), display_timezone)
        downloader = FinamReports(broker, exporter.output_dir)
        report_paths = downloader.download(account.id, baseline, period_to_exclusive, chunk_days=1)
        margins = parse_finam_margins(report_paths, report_dates=downloader.report_dates)
        future_assets = {symbol: asset for symbol, asset in assets.items() if _future(asset)}
        margin_rows = _margin_rows(
            margins, future_assets, local_start.date(), local_end.date() - timedelta(days=1),
        )
        grouped: dict[tuple[date, str], Decimal] = defaultdict(lambda: ZERO)
        for row in margin_rows:
            key = (row["date"], row["ticker"])
            gross = Decimal(str(row["gross_pnl"]))
            if not gross.is_finite():
                raise RuntimeError("Finam: отчёт содержит некорректную вариационную маржу")
            with localcontext() as context:
                context.prec = 80
                grouped[key] += gross
        for day, symbol in sorted(grouped.keys() | future_fees.keys()):
            key = (day, symbol)
            has_margin = key in grouped
            events.append({
                "date": day,
                "ticker": symbol,
                "entry_price": None,
                "exit_price": None,
                "gross_pnl": _money_text(grouped[key]),
                "commission": _money_text(future_fees[key]),
                "event_type": "futures_margin" if has_margin else "futures_fee",
            })

    report = analyze_results(pl.DataFrame(events, schema=RESULT_SCHEMA), starting_capital)
    if not has_futures:
        path = exporter.export(
            closed, broker.name, account.id, period_from,
            period_to_exclusive - timedelta(microseconds=1), display_timezone,
        )
        return path, report, report_paths

    path = exporter._new_file_path(
        broker.name, account.id, period_from, period_to_exclusive - timedelta(microseconds=1),
    )
    margin_frame = pl.DataFrame(margin_rows) if margin_rows else pl.DataFrame(schema={
        "date": pl.Date, "ticker": pl.String, "gross_pnl": pl.Decimal(38, 18),
        "currency": pl.String, "source_id": pl.String, "source_file": pl.String,
    })
    explanation = pl.DataFrame({
        "раздел": ["Акции", "Фьючерсы", "Итог портфеля"],
        "основа расчёта": [
            "Реализованный FIFO P&L закрытых сделок после комиссий.",
            "Вариационная маржа, включая открытые позиции: оплаченная плюс изменение неоплаченной; комиссии исполнений отдельно.",
            "Рублёвый результат акций и фьючерсов. Вводы, выводы, дивиденды, проценты и налоги в этот итог не входят.",
        ],
    })
    write_workbook(path, {
        "Сделки": _closed_rows(closed, display_timezone),
        "Исполнения ФИНАМ": pl.DataFrame(execution_rows, schema=EXECUTION_SCHEMA),
        "Фьючерсы": _display(margin_frame),
        "Итог портфеля": _display(report.daily),
        "Результаты": _display(report.trades),
        "Основа расчёта": explanation,
    })
    return path, report, report_paths
