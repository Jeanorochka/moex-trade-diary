from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import orjson
import polars as pl

from trade_exporter.exporters.analytics import _display_frame, analysis_metadata
from trade_exporter.exporters.excel import write_workbook
from trade_exporter.exporters.portfolio import (
    PERCENTAGES,
    HEADERS as PORTFOLIO_HEADERS,
    _display as display_portfolio,
    portfolio_period_sheet,
    portfolio_period_sheet_options,
    risk_sheet,
    risk_sheet_options,
)
from trade_exporter.services.analytics import analyze_trades
from trade_exporter.services.rankings import ASSET_GAINS, ASSET_LOSSES, build_rankings


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, pl.DataFrame):
        return value.to_dicts()
    if isinstance(value, Path):
        return value.name
    if isinstance(value, bytes):
        return {"encoding": "base64", "content": b64encode(value).decode("ascii")}
    raise TypeError(type(value).__name__)


def _sources(details):
    result = dict(details)
    if "source_reports" not in result:
        return result
    sources = []
    for source in result["source_reports"] or []:
        if isinstance(source, dict) and "content" in source:
            sources.append(source)
            continue
        path = Path(source)
        content = path.read_bytes()
        try:
            value = content.decode("utf-8")
            if "\x00" in value:
                raise UnicodeError
            encoding = "utf-8"
        except UnicodeError:
            value = b64encode(content).decode("ascii")
            encoding = "base64"
        sources.append({"name": path.name, "encoding": encoding, "content": value})
    result["source_reports"] = sources
    return result


def _portable(value, key=None):
    if isinstance(value, pl.DataFrame):
        return [_portable(row) for row in value.to_dicts()]
    if isinstance(value, dict):
        return {name: _portable(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    if isinstance(value, Path) or key in {"source_file", "source_path"} and isinstance(value, str):
        return Path(value).name
    return value


def _summary(analysis, portfolio):
    summary = dict(analysis.summary)
    period = summary.get("portfolio_period") or (portfolio.summary.get("portfolio_period") if portfolio is not None else None)
    if period is not None:
        summary["portfolio_period"] = period
        summary["headline_basis"] = "portfolio_period"
        summary["headline_profit"] = period.get("total_profit") if period.get("status") == "complete" else None
        summary["headline_return"] = period.get("period_return") if period.get("status") == "complete" else None
    if portfolio is None:
        return summary
    portfolio_risk = portfolio.summary.get("risk")
    if portfolio_risk:
        trading_risk = summary.get("risk") or {}
        risk = dict(portfolio_risk)
        metrics = {row["metric"]: dict(row) for row in risk.get("metrics", [])}
        for row in trading_risk.get("metrics", []):
            if row["metric"].startswith("proxy_") or row.get("basis") == "closed_pnl_proxy":
                metrics[row["metric"]] = dict(row)
        risk["metrics"] = list(metrics.values())
        risk["quality"] = list(dict.fromkeys([*risk.get("quality", []), *trading_risk.get("quality", [])]))
        summary["risk"] = risk
    return summary


def _explanations(summary):
    notes = ["Закрытия FIFO и вариационная маржа — торговый расчёт. Они не заменяют полный результат портфеля по начальной и конечной стоимости с внешними движениями."]
    if summary.get("trading_history_loaded") is False:
        notes.append("История торговых результатов не загружена; отсутствие строк не означает нулевую прибыль.")
    if summary.get("commission_basis") == "transactions_period":
        notes.append("Денежные комиссии периода учтены отдельными строками и не распределены по закрытиям или активам. Индивидуальный net P&L не определён; рейтинги используют gross до комиссий.")
    if summary.get("commission_complete") is False:
        notes.append("Полные комиссии не подтверждены: торговый результат уменьшен только на известные расходы.")
    if summary.get("result_complete") is False:
        notes.append("Торговый расчёт частичный: исключённые инструменты не входят в его суммы и рейтинги.")
    for key in ("commission_quality", "result_quality", "execution_quality"):
        notes.extend(str(value) for value in summary.get(key, []))
    return list(dict.fromkeys(notes))


def _add_explanations(frame, notes):
    rows = []
    for note in notes:
        row = {name: None for name in frame.columns}
        row["показатель"] = "Торговый расчёт"
        row["основание"] = note
        rows.append(row)
    return pl.concat([frame, pl.DataFrame(rows, schema=frame.schema)])


def _add_snapshot_values(frame, portfolio):
    if portfolio is None:
        return frame
    rows = []
    values = [("Капитал сейчас: сводная оценка брокера", portfolio.summary.get("equity"), portfolio.summary.get("base_currency"))]
    values.extend(("Деньги сейчас: " + str(row["currency"]), row["amount"], row["currency"])
                  for row in portfolio.cash.iter_rows(named=True))
    for label, value, currency in values:
        row = {name: None for name in frame.columns}
        row.update({
            "показатель": label, "значение": float(value) if value is not None else None, "единица": currency,
            "основание": "Текущий снимок портфеля. Денежные остатки уже входят в сводный капитал и повторно не прибавляются.",
        })
        if "дата / период" in row:
            row["дата / период"] = str(portfolio.summary.get("captured_at") or "")
        rows.append(row)
    return pl.concat([frame, pl.DataFrame(rows, schema=frame.schema)])


def _totals(analysis, portfolio, summary, notes):
    loaded = summary.get("trading_history_loaded") is not False
    currency = str((portfolio.summary.get("base_currency") if portfolio is not None else None) or summary.get("base_currency") or "RUB")
    rows = []
    if portfolio is not None:
        rows.extend([
            ("Капитал сейчас", portfolio.summary.get("equity"), currency, "Сводная оценка брокера; нереализованный результат повторно не прибавляется."),
            ("Полный результат портфеля за период", None, currency, "Нужны начальная оценка, конечная оценка и подтверждённые внешние движения периода."),
        ])
    fields = (
        ("gross_pnl", "Результат закрытий и ВМ до комиссий", currency),
        ("commission", "Учтённые комиссии торгового расчёта", currency),
        ("net_pnl", "Результат закрытий и ВМ после учтённых комиссий", currency),
        ("trades", "Строк торгового результата", "шт."),
        ("wins", "Положительных строк", "шт."),
        ("losses", "Отрицательных строк", "шт."),
        ("win_rate", "Доля положительных строк", "%"),
        ("max_daily_drawdown", "Просадка накопленного торгового результата", currency),
    )
    for key, label, unit in fields:
        value = summary.get(key) if loaded else None
        rows.append((label, float(value) if value is not None else None, unit, "Закрытия FIFO и денежные результаты ВМ; полная переоценка портфеля не моделируется."))
    frame = pl.DataFrame(rows, schema={"показатель": pl.String, "значение": pl.Float64, "единица": pl.String, "основание": pl.String}, orient="row")
    return _add_explanations(frame, notes)


def _results(analysis, summary):
    raw = analysis.trades
    if "event_type" not in raw.columns:
        raw = raw.with_columns(pl.lit("fifo_closed", dtype=pl.String).alias("event_type"))
    frame = _display_frame(raw, result_events=True, partial_fees=summary.get("commission_complete") is False)
    frame = frame.with_columns(raw["event_type"].alias("тип результата"))
    if summary.get("commission_basis") == "transactions_period":
        net_column = "финрез после известных комиссий" if summary.get("commission_complete") is False else "финрез net"
        frame = frame.with_columns(
            pl.when(pl.col("тип результата") == "broker_fee").then(pl.col(net_column)).otherwise(None).alias(net_column),
            pl.when(pl.col("тип результата") == "broker_fee")
            .then(pl.lit("Денежные расходы и возвраты периода."))
            .otherwise(pl.lit("Комиссия не распределена; расходы отдельными строками. Net P&L закрытия не определён."))
            .alias("основа комиссии"),
        )
    note = ""
    if summary.get("result_complete") is False:
        note = "Расчёт частичный; исключённые инструменты и причины — на первом листе."
    elif summary.get("commission_complete") is False:
        note = "Полные расходы неизвестны; учтены известные комиссии. Причины — на первом листе."
    elif summary.get("commission_basis") == "transactions_period":
        note = "Комиссии периода не распределены по строкам; основа — на первом листе."
    elif summary.get("execution_quality"):
        note = "Качество исходных исполнений: см. первый лист."
    if note:
        frame = frame.with_columns(pl.lit(note).alias("качество торгового расчёта"))
    return frame


def _numeric(frame):
    return frame.with_columns(pl.col(name).cast(pl.Float64) for name, dtype in frame.schema.items() if isinstance(dtype, pl.Decimal))


def _options(frame):
    percentages = {PORTFOLIO_HEADERS.get(name, name) for name in PERCENTAGES}
    formats = {
        name: {"num_format": "0.00%" if name in percentages else "#,##0.00"}
        for name, dtype in frame.schema.items() if dtype.is_numeric()
    }
    for name, dtype in frame.schema.items():
        if dtype == pl.Date or isinstance(dtype, pl.Datetime):
            formats[name] = {"num_format": "dd.mm.yyyy"}
        if name in {"основание", "основа", "примечание", "качество торгового расчёта", "основа комиссии"}:
            formats[name] = {"wrap_text": True}
    return {
        "column_widths": {"_all": 22},
        "column_formats": formats,
    }


def _ranking_charts(rankings, summary):
    positions = [index + 2 for index, value in enumerate(rankings["раздел"]) if value in {ASSET_LOSSES, ASSET_GAINS}]
    if not positions:
        return {}
    if positions != list(range(min(positions), max(positions) + 1)):
        return {}
    basis = "gross до комиссий" if summary.get("commission_basis") == "transactions_period" or summary.get("commission_complete") is False else "после учтённых комиссий"
    return {"L2": {
        "type": "bar", "title": f"Закрытия и ВМ по активам: {basis}",
        "categories_range": f"'Лидеры и потери'!$D${min(positions)}:$D${max(positions)}",
        "values_range": f"'Лидеры и потери'!$E${min(positions)}:$E${max(positions)}",
        "width": 760, "height": 360, "show_legend": False,
    }}


def _reserve(output_dir, prefix):
    for attempt in range(100):
        suffix = f"_{attempt}" if attempt else ""
        stem = output_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:10]}{suffix}"
        workbook, json_path = Path(f"{stem}.xlsx"), Path(f"{stem}.json")
        try:
            with workbook.open("xb"):
                pass
        except FileExistsError:
            continue
        try:
            with json_path.open("xb"):
                pass
        except Exception as error:
            workbook.unlink()
            if isinstance(error, FileExistsError):
                continue
            raise
        return workbook, json_path
    raise RuntimeError("Не удалось создать новое имя итогового отчёта")


def export_report(analysis_report, output_dir, *, portfolio_report=None, prefix="report", details=None):
    if analysis_report is None:
        analysis_report = analyze_trades([], timezone.utc)
        analysis_report.summary["trading_history_loaded"] = False
    if not prefix or Path(prefix).name != prefix or "/" in prefix or "\\" in prefix:
        raise ValueError("Префикс отчёта должен быть именем файла без пути")
    summary = _summary(analysis_report, portfolio_report)
    notes = _explanations(summary)
    period = summary.get("portfolio_period")
    risk = summary.get("risk")
    if period is not None:
        sheets = {"Портфель за период": _add_explanations(portfolio_period_sheet(period), notes)}
        options = {"Портфель за период": portfolio_period_sheet_options(period)}
    else:
        sheets = {"Итоги": _totals(analysis_report, portfolio_report, summary, notes)}
        options = {"Итоги": {"column_widths": {0: 58, 1: 22, 2: 12, 3: 100}, "column_formats": {"значение": {"num_format": "#,##0.00"}, "основание": {"wrap_text": True}}}}
        rate_row = sheets["Итоги"]["показатель"].to_list().index("Доля положительных строк") + 2
        if summary.get("win_rate") is not None and summary.get("trading_history_loaded") is not False:
            options["Итоги"]["cells"] = {f"B{rate_row}": {"value": float(summary["win_rate"]), "num_format": "0.00%"}}
    primary_name = next(iter(sheets))
    sheets[primary_name] = _add_snapshot_values(sheets[primary_name], portfolio_report)
    raw_rankings = build_rankings(analysis_report, portfolio_report)
    rankings = _numeric(raw_rankings)
    sheets["Лидеры и потери"] = rankings
    sheets["Результаты"] = _results(analysis_report, summary)
    period_flows = getattr(portfolio_report, "period_flows", None)
    if portfolio_report is not None:
        sheets["Позиции"] = display_portfolio(portfolio_report.positions)
    if period_flows is not None:
        sheets["Потоки периода"] = display_portfolio(period_flows)
    if risk is not None:
        sheets["Риск и доходность"] = risk_sheet(risk)
    sheets["Параметры расчёта"] = analysis_metadata(summary)
    for name, frame in sheets.items():
        options.setdefault(name, _options(frame))
    options["Лидеры и потери"].update({
        "column_widths": {0: 40, 1: 8, 2: 13, 3: 23, 4: 19, 5: 17, 6: 17, 7: 15, 8: 48, 9: 80},
        "charts": _ranking_charts(rankings, summary),
        "cells": {
            f"C{index + 2}": {"value": value, "num_format": "dd.mm.yyyy"}
            for index, value in enumerate(rankings["дата"]) if value is not None
        },
    })
    options["Лидеры и потери"]["column_formats"].update({
        "дата": {"num_format": "dd.mm.yyyy"},
        "место": {"num_format": "0"}, "строк результата": {"num_format": "0"},
    })
    options["Результаты"].update({
        "autofit": False,
        "column_widths": {
            index: 48 if name in {"основа комиссии", "качество торгового расчёта"} else 22
            for index, name in enumerate(sheets["Результаты"].columns)
        },
        "row_heights": {0: 30, **{index: 42 for index in range(1, sheets["Результаты"].height + 1)}},
    })
    options["Параметры расчёта"] = {"column_widths": {0: 40, 1: 80}}
    if risk is not None:
        options["Риск и доходность"] = risk_sheet_options(risk)
    analysis_payload = {
        "summary": summary, "trades": analysis_report.trades.to_dicts(),
        "results": analysis_report.trades.to_dicts(), "daily": analysis_report.daily.to_dicts(),
        "monthly": analysis_report.monthly.to_dicts(), "by_ticker": analysis_report.by_ticker.to_dicts(),
    }
    portfolio_payload = None
    if portfolio_report is not None:
        portfolio_payload = {
            "summary": portfolio_report.summary, "positions": portfolio_report.positions.to_dicts(),
            "allocation": portfolio_report.allocation.to_dicts(), "cash": portfolio_report.cash.to_dicts(),
            "flows": portfolio_report.flows.to_dicts(),
            "period_flows": period_flows.to_dicts() if period_flows is not None else None,
        }
    payload = {
        "kind": "trade_portfolio_report", "version": 1,
        "basis": "portfolio_period_with_separate_trading_results" if period is not None else "portfolio_snapshot_with_separate_trading_results" if portfolio_report is not None else "trading_results_only",
        **analysis_payload, "portfolio": portfolio_payload, "rankings": raw_rankings.to_dicts(),
        "details": _portable(_sources((getattr(analysis_report, "details", {}) if details is None else details) or {})),
    }
    if "event_type" in analysis_report.trades.columns and "futures_margin" in analysis_report.trades["event_type"].to_list():
        payload["futures_basis"] = "paid_margin_plus_change_in_pending_margin_including_open_positions"
    serialized = orjson.dumps(payload, default=_json_default, option=orjson.OPT_INDENT_2)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workbook, json_path = _reserve(output_dir, prefix)
    temporary_workbook = output_dir / f".{workbook.stem}_{uuid4().hex}.tmp.xlsx"
    temporary_json = output_dir / f".{json_path.stem}_{uuid4().hex}.tmp.json"
    try:
        write_workbook(temporary_workbook, sheets, sheet_options=options)
        temporary_json.write_bytes(serialized)
        os.replace(temporary_workbook, workbook)
        os.replace(temporary_json, json_path)
    except Exception:
        workbook.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        raise
    finally:
        temporary_workbook.unlink(missing_ok=True)
        temporary_json.unlink(missing_ok=True)
    return workbook, json_path
