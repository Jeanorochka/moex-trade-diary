from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from pathlib import Path

import orjson
import polars as pl

from trade_exporter.exporters.excel import write_workbook
from trade_exporter.exporters.portfolio import portfolio_period_sheet, portfolio_period_sheet_options, risk_sheet, risk_sheet_options
from trade_exporter.services.analytics import AnalysisReport


SUMMARY_LABELS = {
    "trades": "Закрытых строк FIFO",
    "wins": "Прибыльных",
    "losses": "Убыточных",
    "breakeven": "Безубыточных",
    "gross_pnl": "Gross P&L",
    "commission": "Комиссии",
    "net_pnl": "Net P&L",
    "win_rate": "Win rate",
    "avg_trade": "Средний net P&L",
    "avg_win": "Средняя прибыль",
    "avg_loss": "Средний убыток",
    "profit_factor": "Profit factor",
    "payoff_ratio": "Payoff ratio",
    "best_trade": "Лучшая строка FIFO",
    "worst_trade": "Худшая строка FIFO",
    "max_win_streak": "Максимальная серия прибылей",
    "max_loss_streak": "Максимальная серия убытков",
    "trading_days": "Дней с закрытыми сделками",
    "max_drawdown": "Просадка по строкам FIFO",
    "max_daily_drawdown": "Просадка накопленного результата по дням",
    "commission_load": "Комиссии / сумма |gross P&L|",
    "starting_capital": "Начальный капитал",
    "return_on_capital": "Net P&L / начальный капитал",
    "max_drawdown_pct": "Просадка результата / пик условного капитала",
    "synthetic_trade_ids": "Исполнений с локальными ID",
}

HEADERS = {
    "date": "дата",
    "ticker": "тикер",
    "entry_price": "цена входа",
    "exit_price": "цена выхода",
    "trades": "строк FIFO",
    "wins": "прибыльных",
    "losses": "убыточных",
    "win_rate": "win rate",
    "gross_pnl": "финрез",
    "commission": "комиссия",
    "net_pnl": "финрез net",
    "cumulative_pnl": "накопленный net P&L",
    "equity": "условный капитал по результатам",
    "drawdown": "просадка",
    "event_type": "тип результата",
}


def analysis_metadata(summary: dict) -> pl.DataFrame:
    rows = []
    for key in ("commission_complete", "commission_basis", "commission_quality", "net_pnl_basis", "synthetic_trade_ids", "execution_quality", "result_complete", "result_quality", "excluded_instruments", "portfolio_period", "risk"):
        if key not in summary:
            continue
        value = orjson.dumps(summary[key], default=_json_default).decode()
        if len(value) <= 12000:
            rows.append((key, value))
        else:
            rows.extend((f"{key}__part_{index // 12000 + 1:04d}", value[index:index + 12000])
                        for index in range(0, len(value), 12000))
    return pl.DataFrame(rows, schema={"ключ": pl.String, "значение": pl.String}, orient="row")


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _display_frame(frame: pl.DataFrame, *, monthly=False, result_events=False, partial_fees=False) -> pl.DataFrame:
    frame = frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Decimal)
    )
    if "date" in frame.columns:
        frame = frame.with_columns(pl.col("date").dt.strftime("%m.%Y" if monthly else "%d.%m.%Y"))
    if "event_type" in frame.columns:
        frame = frame.with_columns(pl.col("event_type").replace({
            "stock_fifo": "закрытие акций FIFO",
            "stock_closed": "закрытие акций FIFO",
            "futures_margin": "вариационная маржа и комиссии",
            "futures_fee": "комиссии фьючерсов",
            "broker_fee": "денежные расходы и возвраты ФИНАМ",
        }))
    headers = HEADERS | ({"trades": "строк результата", "win_rate": "доля положительных"} if result_events else {})
    if partial_fees:
        headers = headers | {
            "net_pnl": "финрез после известных комиссий",
            "cumulative_pnl": "накопленный результат после известных комиссий",
        }
    return frame.rename({name: headers.get(name, name) for name in frame.columns})


def _column_ref(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _chart(frame, sheet, column, title, chart_type="line"):
    end = frame.height + 1
    value_col = _column_ref(frame.columns.index(column))
    return {
        "type": chart_type,
        "values_range": f"'{sheet}'!${value_col}$2:${value_col}${end}",
        "categories_range": f"'{sheet}'!$A$2:$A${end}",
        "title": title,
        "width": 760,
        "height": 360,
        "show_legend": False,
    }


def export_analysis(report: AnalysisReport, output_dir: str | Path, prefix="analytics", *, portfolio_period=None, risk=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = output_dir / f"{prefix}_{stamp}"
    workbook_path = stem.with_suffix(".xlsx")
    json_path = stem.with_suffix(".json")
    portfolio_period = portfolio_period if portfolio_period is not None else report.summary.get("portfolio_period")
    risk = risk if risk is not None else report.summary.get("risk")
    metadata_summary = report.summary | ({"portfolio_period": portfolio_period} if portfolio_period is not None else {}) | ({"risk": risk} if risk is not None else {})
    labels = [key for key, value in report.summary.items()
              if key not in {"headline_profit", "headline_return"}
              and (value is None or isinstance(value, (int, float, Decimal)) and not isinstance(value, bool))]
    result_events = "event_type" in report.trades.columns
    partial_fees = report.summary.get("commission_complete") is False
    summary_labels = SUMMARY_LABELS | ({
        "trades": "Строк результата: FIFO акций и ВМ фьючерсов",
        "wins": "Положительных строк результата",
        "losses": "Отрицательных строк результата",
        "breakeven": "Нулевых строк результата",
        "win_rate": "Доля положительных строк результата",
        "best_trade": "Лучшая строка результата",
        "worst_trade": "Худшая строка результата",
        "max_win_streak": "Серия положительных строк результата",
        "max_loss_streak": "Серия отрицательных строк результата",
        "trading_days": "Дней с результатом",
        "max_drawdown": "Просадка по строкам результата",
    } if result_events else {})
    if partial_fees:
        summary_labels = summary_labels | {
            "net_pnl": "Результат после известных комиссий; расходы неполны",
            "commission": "Известные комиссии; полный размер не подтверждён",
            "avg_trade": "Средний результат после известных комиссий",
            "return_on_capital": "Результат после известных комиссий / начальный капитал",
        }
    if report.summary.get("result_complete") is False:
        summary_labels = summary_labels | {
            "gross_pnl": "Частичный gross P&L; исключённые инструменты не учтены",
            "net_pnl": "Частичный результат после известных комиссий" if partial_fees else "Частичный net P&L; исключённые инструменты не учтены",
        }
    if portfolio_period is not None:
        summary_labels = summary_labels | {
            "gross_pnl": "Результат закрытий и ВМ до комиссий",
            "net_pnl": "Результат закрытий и ВМ после комиссий",
            "return_on_capital": "Результат закрытий и ВМ / начальный капитал",
            "starting_capital": "Начальный капитал торговой модели",
        }
        if partial_fees or report.summary.get("result_complete") is False:
            summary_labels["net_pnl"] = "Частичный результат закрытий и ВМ после известных комиссий"
    summary = pl.DataFrame({
        "показатель": [summary_labels.get(key, key) for key in labels],
        "значение": [
            None if report.summary[key] is None else float(report.summary[key])
            for key in labels
        ],
    })
    sheets = {
        "Аналитика": summary,
        "Результаты" if result_events else "Сделки": _display_frame(report.trades, result_events=result_events, partial_fees=partial_fees),
        "Дни": _display_frame(report.daily, result_events=result_events, partial_fees=partial_fees),
        "Месяцы": _display_frame(report.monthly, monthly=True, result_events=result_events, partial_fees=partial_fees),
        "Тикеры": _display_frame(report.by_ticker, result_events=result_events, partial_fees=partial_fees),
    }
    if portfolio_period is not None:
        sheets = {"Портфель за период": portfolio_period_sheet(portfolio_period), **sheets}
    if "commission_complete" in report.summary:
        fee_basis = report.summary.get("commission_basis", "Основа комиссий в исходном файле не указана.")
        basis = {
            "transactions_period": "Денежные расходы и возвраты из Transactions за выбранный период; по отдельным сделкам не распределены.",
            "execution_fifo_and_futures": "Комиссии исполнений: распределение FIFO для акций и комиссии фьючерсов выбранного периода.",
        }.get(fee_basis, str(fee_basis))
        sheets["Основа расчёта"] = pl.DataFrame([
            ("Комиссии", basis),
            ("Полнота расходов", "Не подтверждена; результат после известных комиссий является частичным." if partial_fees else "Подтверждена в рамках выбранной основы расчёта."),
            *(("Качество данных", reason) for reason in report.summary.get("commission_quality", [])),
            *(("Идентификаторы исполнений", reason) for reason in report.summary.get("execution_quality", [])),
            *(("Полнота результата", reason) for reason in report.summary.get("result_quality", [])),
        ], schema={"раздел": pl.String, "основа расчёта": pl.String}, orient="row")
    metadata = analysis_metadata(metadata_summary)
    if not metadata.is_empty():
        sheets["Параметры расчёта"] = metadata
    if risk is not None:
        sheets["Риск и доходность"] = risk_sheet(risk)
    options = {}
    formats = {
        "*финрез*": {"num_format": "#,##0.00"},
        "комиссия": {"num_format": "#,##0.00"},
        "накопленный net P&L": {"num_format": "#,##0.00"},
        "условный капитал по результатам": {"num_format": "#,##0.00"},
        "просадка": {"num_format": "#,##0.00"},
        "цена*": {"num_format": "0.######"},
        "win rate": {"num_format": "0.00%"},
        "доля положительных": {"num_format": "0.00%"},
        "дата": {"num_format": "dd.mm.yyyy"},
    }
    for name, frame in sheets.items():
        options[name] = {"column_formats": {
            pattern: style for pattern, style in formats.items()
            if any(fnmatchcase(column, pattern) for column in frame.columns)
        }}
    options["Аналитика"] = {
        "column_widths": {0: 64 if result_events else 48, 1: 22},
        "column_formats": {"значение": {"num_format": "#,##0.00"}},
        "cells": {
            f"B{labels.index(key) + 2}": {
                "value": float(report.summary[key]), "num_format": "0.00%",
            }
            for key in ("win_rate", "commission_load", "return_on_capital", "max_drawdown_pct")
            if report.summary.get(key) is not None
        },
    }
    if portfolio_period is not None:
        options["Портфель за период"] = portfolio_period_sheet_options(portfolio_period)
    if "Параметры расчёта" in sheets:
        options["Параметры расчёта"] = {"column_widths": {0: 40, 1: 80}}
    if risk is not None:
        options["Риск и доходность"] = risk_sheet_options(risk)
    if not report.daily.is_empty():
        options["Дни"]["charts"] = {
            "K2": _chart(report.daily, "Дни", "cumulative_pnl", "Результат после известных комиссий" if partial_fees else "Накопленный net P&L"),
            "K22": _chart(report.daily, "Дни", "drawdown", "Просадка накопленного результата по дням"),
        }
    if not report.monthly.is_empty():
        options["Месяцы"]["charts"] = {
            "H2": _chart(report.monthly, "Месяцы", "net_pnl", "После известных комиссий по месяцам" if partial_fees else "Net P&L по месяцам", "column"),
        }
    if not report.by_ticker.is_empty():
        options["Тикеры"]["charts"] = {
            "K2": _chart(report.by_ticker, "Тикеры", "net_pnl", "После известных комиссий по тикерам" if partial_fees else "Net P&L по тикерам", "bar"),
        }
    write_workbook(workbook_path, sheets, sheet_options=options)
    payload = {
        "summary": metadata_summary,
        "daily": report.daily.to_dicts(),
        "monthly": report.monthly.to_dicts(),
        "by_ticker": report.by_ticker.to_dicts(),
    }
    if result_events:
        payload["basis"] = "stock_fifo_and_reported_futures_variation_margin"
        payload["futures_basis"] = "paid_margin_plus_change_in_pending_margin_including_open_positions"
        payload["results"] = report.trades.to_dicts()
    json_path.write_bytes(orjson.dumps(payload, default=_json_default, option=orjson.OPT_INDENT_2))
    return workbook_path, json_path
