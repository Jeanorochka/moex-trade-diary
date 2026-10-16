from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from pathlib import Path

import orjson
import polars as pl

from trade_exporter.exporters.excel import write_workbook
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
    "max_daily_drawdown": "Просадка по итогам дней",
    "commission_load": "Комиссии / сумма |gross P&L|",
    "starting_capital": "Начальный капитал",
    "return_on_capital": "Net P&L / начальный капитал",
    "max_drawdown_pct": "Максимальная просадка / пик капитала",
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
    "equity": "капитал",
    "drawdown": "просадка",
}


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _display_frame(frame: pl.DataFrame, *, monthly=False) -> pl.DataFrame:
    frame = frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Decimal)
    )
    if "date" in frame.columns:
        frame = frame.with_columns(pl.col("date").dt.strftime("%m.%Y" if monthly else "%d.%m.%Y"))
    return frame.rename({name: HEADERS.get(name, name) for name in frame.columns})


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


def export_analysis(report: AnalysisReport, output_dir: str | Path, prefix="analytics"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = output_dir / f"{prefix}_{stamp}"
    workbook_path = stem.with_suffix(".xlsx")
    json_path = stem.with_suffix(".json")
    labels = list(report.summary)
    summary = pl.DataFrame({
        "показатель": [SUMMARY_LABELS.get(key, key) for key in labels],
        "значение": [
            None if report.summary[key] is None else float(report.summary[key])
            for key in labels
        ],
    })
    sheets = {
        "Аналитика": summary,
        "Сделки": _display_frame(report.trades),
        "Дни": _display_frame(report.daily),
        "Месяцы": _display_frame(report.monthly, monthly=True),
        "Тикеры": _display_frame(report.by_ticker),
    }
    options = {}
    formats = {
        "*финрез*": {"num_format": "#,##0.00"},
        "комиссия": {"num_format": "#,##0.00"},
        "накопленный net P&L": {"num_format": "#,##0.00"},
        "капитал": {"num_format": "#,##0.00"},
        "просадка": {"num_format": "#,##0.00"},
        "цена*": {"num_format": "0.######"},
        "win rate": {"num_format": "0.00%"},
        "дата": {"num_format": "dd.mm.yyyy"},
    }
    for name, frame in sheets.items():
        options[name] = {"column_formats": {
            pattern: style for pattern, style in formats.items()
            if any(fnmatchcase(column, pattern) for column in frame.columns)
        }}
    options["Аналитика"] = {
        "column_widths": {0: 48, 1: 22},
        "column_formats": {"значение": {"num_format": "#,##0.00"}},
        "cells": {
            f"B{labels.index(key) + 2}": {
                "value": float(report.summary[key]), "num_format": "0.00%",
            }
            for key in ("win_rate", "commission_load", "return_on_capital", "max_drawdown_pct")
            if report.summary.get(key) is not None
        },
    }
    if not report.daily.is_empty():
        options["Дни"]["charts"] = {
            "K2": _chart(report.daily, "Дни", "cumulative_pnl", "Накопленный net P&L"),
            "K22": _chart(report.daily, "Дни", "drawdown", "Просадка по итогам дней"),
        }
    if not report.monthly.is_empty():
        options["Месяцы"]["charts"] = {
            "H2": _chart(report.monthly, "Месяцы", "net_pnl", "Net P&L по месяцам", "column"),
        }
    if not report.by_ticker.is_empty():
        options["Тикеры"]["charts"] = {
            "K2": _chart(report.by_ticker, "Тикеры", "net_pnl", "Net P&L по тикерам", "bar"),
        }
    write_workbook(workbook_path, sheets, sheet_options=options)
    payload = {
        "summary": report.summary,
        "daily": report.daily.to_dicts(),
        "monthly": report.monthly.to_dicts(),
        "by_ticker": report.by_ticker.to_dicts(),
    }
    json_path.write_bytes(orjson.dumps(payload, default=_json_default, option=orjson.OPT_INDENT_2))
    return workbook_path, json_path
