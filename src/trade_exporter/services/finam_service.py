from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from trade_exporter.brokers.finam_reports import FinamReports
from trade_exporter.brokers.portfolio import load_cash_flows
from trade_exporter.exporters.analytics import analysis_metadata
from trade_exporter.exporters.excel import write_workbook
from trade_exporter.exporters.portfolio import portfolio_period_sheet, portfolio_period_sheet_options, risk_sheet, risk_sheet_options
from trade_exporter.services.analytics import AnalysisReport, analyze_results
from trade_exporter.services.finam_report import parse_finam_margins
from trade_exporter.services.matcher import FifoMatcher
from trade_exporter.services.risk import analyze_result_risk
from trade_exporter.services.performance import attach_portfolio_performance


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
    "статус комиссии": pl.String,
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


def _unsupported_price(error: RuntimeError, symbol: str) -> bool:
    message = str(error)
    return (
        message.startswith(f"Finam: для {symbol} (")
        and message.endswith("задай подтверждённый price_multipliers; денежная цена не определена")
    ) or message == f"Finam: {symbol} содержит НКД; расчёт P&L с НКД не поддерживается"


def _display(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Decimal)
    )


def _commission(broker, raw: dict) -> tuple[Decimal | None, str]:
    fee = raw.get("commission")
    if not isinstance(fee, dict):
        return None, ""
    value = fee.get("value")
    if value is None or isinstance(value, dict) and value.get("value") is None:
        return None, str(fee.get("currency") or "").upper()
    return broker._decimal(value, "commission.value").copy_abs(), str(fee.get("currency") or "").upper()


def _fee_events(broker, account, start, end, display_timezone) -> tuple[list[dict], list[dict], bool, list[str]]:
    try:
        history = load_cash_flows(broker, account, start, end, require_inception=False)
    except Exception:
        return [], [], False, [
            "Комиссии исполнений не переданы API; денежные операции за выбранный период недоступны. Результат показан без неизвестных расходов.",
        ]
    reasons = list(history.reasons)
    rows = []
    grouped = defaultdict(lambda: [ZERO, ZERO])
    for flow in history.flows:
        if flow.kind != "fee" or not start <= flow.timestamp < end:
            continue
        if flow.currency != "RUB":
            reasons.append(f"Комиссии в {flow.currency} не пересчитаны в RUB и не включены в результат.")
            continue
        rows.append({
            "дата": flow.timestamp.astimezone(display_timezone).date(),
            "время": flow.timestamp.isoformat(),
            "сумма": flow.amount,
            "валюта": flow.currency,
            "идентификатор операции": flow.id,
        })
        key = flow.timestamp.astimezone(display_timezone).date()
        with localcontext() as context:
            context.prec = 80
            grouped[key][0] += max(flow.amount, ZERO)
            grouped[key][1] += max(-flow.amount, ZERO)
    events = [{
        "date": day, "ticker": "КОМИССИИ ФИНАМ", "entry_price": None, "exit_price": None,
        "gross_pnl": _money_text(amounts[0]), "commission": _money_text(amounts[1]),
        "event_type": "broker_fee",
    } for day, amounts in sorted(grouped.items()) if any(amounts)]
    complete = history.complete and not reasons
    if not complete:
        reasons.append("Результат уменьшен только на известные рублёвые расходы; полные комиссии не подтверждены.")
    return events, rows, complete, list(dict.fromkeys(reasons))


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
    commission, fee_currency = _commission(broker, raw)
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
        "комиссия": float(commission) if commission is not None else None,
        "статус комиссии": "передана API" if commission is not None else "не передана API",
        "валюта цены": currency,
        "валюта комиссии": fee_currency,
        "идентификатор сделки": raw["trade_id"],
    }


def _closed_rows(closed, display_timezone, *, allocated_fees=True) -> pl.DataFrame:
    rows = []
    for trade in closed:
        rows.append({
            "дата": trade.exit_at.astimezone(display_timezone).strftime("%d.%m.%y"),
            "тикер": trade.instrument_id,
            "цена входа": float(trade.entry_price),
            "цена выхода": float(trade.exit_price),
            "финрез": float(trade.gross_pnl),
            "комиссия": float(trade.commission) if allocated_fees else None,
            "направление": trade.direction,
            "количество": trade.quantity,
            "финрез net": float(trade.net_pnl) if allocated_fees else None,
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
    portfolio_report=None,
    write_output: bool = True,
    source_report_dir: str | Path | None = None,
) -> tuple[Path | None, AnalysisReport, list[Path]]:
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
    selected_symbols = {
        trade.get("symbol") for trade in raw
        if period_from <= broker._timestamp(trade.get("timestamp")) < period_to_exclusive
    }
    transaction_fees = any(_commission(broker, trade)[0] is None for trade in raw)
    synthetic_ids = getattr(broker, "history_quality", {}).get(account.id, {}).get("synthetic_trade_ids", 0)
    execution_quality = [
        f"Брокер не передал ID для {synthetic_ids} исполнений; назначены локальные ID, одинаковые исполнения сохранены.",
    ] if synthetic_ids else []
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
    excluded_symbols = {}
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
        elif symbol in selected_symbols and symbol not in excluded_symbols:
            matcher_trade = trade
            if transaction_fees:
                matcher_trade = {**trade, "commission": {
                    "value": {"value": "0"}, "currency": str(trade.get("currency") or "").upper(),
                }}
            try:
                fill = broker._fill(account.id, matcher_trade)
            except RuntimeError as error:
                if not _unsupported_price(error, symbol):
                    raise
                excluded_symbols[symbol] = str(error)
            else:
                stock_fills.append(fill)
                stock_currencies[symbol].add(fill.currency.upper())
        if selected:
            execution_rows.append(_execution(broker, trade, display_timezone))
            if _future(asset) and not transaction_fees:
                fee = trade["commission"]
                commission = broker._decimal(fee.get("value"), "commission.value").copy_abs()
                if commission and str(fee.get("currency") or "").upper() != "RUB":
                    raise RuntimeError("Finam: комиссии фьючерсов должны быть в RUB")
                key = (timestamp.astimezone(display_timezone).date(), symbol)
                with localcontext() as context:
                    context.prec = 80
                    future_fees[key] += commission

    result_quality = [
        f"{symbol}: исполнения сохранены, но весь инструмент исключён из P&L. {reason}. Общий результат рассчитан частично."
        for symbol, reason in sorted(excluded_symbols.items())
    ]
    stock_fills = [fill for fill in stock_fills if fill.instrument_id not in excluded_symbols]
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
        baseline = datetime.combine(local_start.date() - timedelta(days=1), time(), timezone.utc)
        report_end = datetime.combine(local_end.date(), time(), timezone.utc)
        downloader = FinamReports(broker, source_report_dir if source_report_dir is not None else exporter.output_dir)
        report_paths = downloader.download(account.id, baseline, report_end, chunk_days=1)
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

    fee_rows = []
    commission_complete = True
    quality = []
    if transaction_fees:
        fee_events, fee_rows, commission_complete, quality = _fee_events(
            broker, account, period_from, period_to_exclusive, display_timezone,
        )
        events.extend(fee_events)
    report = analyze_results(pl.DataFrame(events, schema=RESULT_SCHEMA), starting_capital)
    report.summary.update({
        "commission_complete": commission_complete,
        "commission_basis": "transactions_period" if transaction_fees else "execution_fifo_and_futures",
        "commission_quality": quality,
        "net_pnl_basis": "gross_less_known_fees" if not commission_complete else "gross_less_fees",
        "synthetic_trade_ids": synthetic_ids,
        "execution_quality": execution_quality,
        "result_complete": not result_quality,
        "result_quality": result_quality,
        "excluded_instruments": sorted(excluded_symbols),
    })
    risk = analyze_result_risk(report.daily, starting_capital, period_from, period_to_exclusive)
    if not commission_complete:
        reason = "Комиссии неполны: расчёт основан на результате после известных расходов."
        risk["quality"].append(reason)
        for metric in risk["metrics"]:
            metric["quality_reason"] = " ".join(filter(None, (metric.get("quality_reason"), reason)))
    for reason in result_quality:
        risk["quality"].append(reason)
        for metric in risk["metrics"]:
            metric["quality_reason"] = " ".join(filter(None, (metric.get("quality_reason"), reason)))
    report.summary["risk"] = risk
    attach_portfolio_performance(report, portfolio_report)
    risk = report.summary["risk"]
    portfolio_period = report.summary.get("portfolio_period")
    margin_frame = pl.DataFrame(margin_rows) if margin_rows else pl.DataFrame(schema={
        "date": pl.Date, "ticker": pl.String, "gross_pnl": pl.Decimal(38, 18),
        "currency": pl.String, "source_id": pl.String, "source_file": pl.String,
    })
    closed_frame = _closed_rows(closed, display_timezone, allocated_fees=not transaction_fees)
    execution_frame = pl.DataFrame(execution_rows, schema=EXECUTION_SCHEMA)
    fee_frame = pl.DataFrame(fee_rows, schema={
        "дата": pl.Date, "время": pl.String, "сумма": pl.Decimal(38, 18),
        "валюта": pl.String, "идентификатор операции": pl.String,
    })
    report.details.update({
        "executions": execution_frame,
        "raw_executions": [
            trade for trade in raw
            if period_from <= broker._timestamp(trade.get("timestamp")) < period_to_exclusive
        ],
        "closed_trades": closed_frame,
        "closed_records": [{
            **asdict(trade),
            "commission": trade.commission if not transaction_fees else None,
            "net_pnl": trade.net_pnl if not transaction_fees else None,
        } for trade in closed],
        "futures_margin": margin_frame,
        "broker_fees": fee_frame,
        "source_reports": list(report_paths),
    })
    if not write_output:
        return None, report, report_paths
    if not has_futures and not transaction_fees and not execution_quality and not result_quality:
        options = {"risk": risk}
        if portfolio_period is not None:
            options["portfolio_period"] = portfolio_period
        path = exporter.export(
            closed, broker.name, account.id, period_from,
            period_to_exclusive - timedelta(microseconds=1), display_timezone, **options,
        )
        return path, report, report_paths

    path = exporter._new_file_path(
        broker.name, account.id, period_from, period_to_exclusive - timedelta(microseconds=1),
    )
    basis_rows = [
        ("Акции", "Валовой реализованный FIFO P&L закрытых сделок." if transaction_fees else "Реализованный FIFO P&L закрытых сделок после комиссий."),
        ("Фьючерсы", "Вариационная маржа, включая открытые позиции: оплаченная плюс изменение неоплаченной; комиссии учтены отдельно."),
        ("Торговый результат", "Закрытия по исторической себестоимости и ВМ фьючерсов. Переоценка открытых акций, выплаты по бумагам и изменение себестоимости к началу периода здесь не моделируются. Это не общая прибыль портфеля."),
    ]
    if transaction_fees:
        basis_rows.extend([
            ("Комиссии", "Денежные расходы и возвраты расходов из Transactions за выбранный период, включая неторговые сборы. Комиссии исполнений повторно не вычитаются."),
            ("Комиссии отдельных сделок", "API не передал все комиссии исполнений. Расходы из денежных операций не распределены по отдельным сделкам; их net P&L не определён."),
            ("Полнота расходов", "Подтверждена история денежных операций за выбранный период." if commission_complete else "Не подтверждена. Результат после известных комиссий является частичным."),
        ])
        basis_rows.extend(("Качество данных", reason) for reason in quality)
    basis_rows.extend(("Идентификаторы исполнений", reason) for reason in execution_quality)
    basis_rows.extend(("Полнота результата", reason) for reason in result_quality)
    explanation = pl.DataFrame(basis_rows, schema={"раздел": pl.String, "основа расчёта": pl.String}, orient="row")
    result_frame = _display(report.trades)
    if transaction_fees:
        result_frame = result_frame.with_columns(
            pl.when(pl.col("event_type") == "stock_fifo").then(pl.lit("валовой FIFO; расходы отдельными строками"))
            .when(pl.col("event_type") == "broker_fee").then(pl.lit("известные расходы и возвраты из Transactions"))
            .otherwise(pl.lit("вариационная маржа; расходы отдельными строками")).alias("основа комиссии"),
            pl.lit("полные расходы" if commission_complete else "только известные расходы").alias("полнота расходов"),
        )
    if result_quality:
        result_frame = result_frame.with_columns(pl.lit("частичный; неподдержанные инструменты исключены").alias("полнота результата"))
    sheets = {
        "Сделки": closed_frame,
        "Исполнения ФИНАМ": execution_frame,
        "Фьючерсы": _display(margin_frame),
        "Торговый результат": _display(report.daily),
        "Результаты": result_frame,
        "Основа расчёта": explanation,
        "Параметры расчёта": analysis_metadata(report.summary),
    }
    if transaction_fees:
        sheets["Денежные комиссии"] = _display(fee_frame)
    sheets["Риск и доходность"] = risk_sheet(risk)
    sheet_options = {"Риск и доходность": risk_sheet_options(risk)}
    if portfolio_period is not None:
        sheets = {"Портфель за период": portfolio_period_sheet(portfolio_period), **sheets}
        sheet_options["Портфель за период"] = portfolio_period_sheet_options(portfolio_period)
    write_workbook(path, sheets, sheet_options=sheet_options)
    return path, report, report_paths
