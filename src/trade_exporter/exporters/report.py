from __future__ import annotations

from base64 import b64encode
from datetime import timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import orjson
import polars as pl

from trade_exporter.exporters.clean_report import clean_report, set_print_layout
from trade_exporter.exporters.excel import write_workbook
from trade_exporter.services.analytics import analyze_trades
from trade_exporter.services.rankings import build_rankings
from trade_exporter.services.report_view import report_dates


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


def _reserve(output_dir, dates):
    for attempt in range(100):
        suffix = f"_{attempt + 1}" if attempt else ""
        stem = output_dir / f"{dates[0]:%d.%m.%Y}_{dates[1]:%d.%m.%Y}{suffix}"
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


def export_report(analysis_report, output_dir, *, portfolio_report=None, prefix="report", details=None, period_from=None, period_to=None):
    if analysis_report is None:
        analysis_report = analyze_trades([], timezone.utc)
        analysis_report.summary["trading_history_loaded"] = False
    if not prefix or Path(prefix).name != prefix or "/" in prefix or "\\" in prefix:
        raise ValueError("Префикс отчёта должен быть именем файла без пути")
    summary = _summary(analysis_report, portfolio_report)
    period = summary.get("portfolio_period")
    dates = report_dates(analysis_report, summary, period_from, period_to)
    sheets, options, last_row, breaks = clean_report(analysis_report, portfolio_report, summary, dates)
    raw_rankings = build_rankings(analysis_report, portfolio_report)
    period_flows = getattr(portfolio_report, "period_flows", None)
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
    workbook, json_path = _reserve(output_dir, dates)
    temporary_workbook = output_dir / f".{workbook.stem}_{uuid4().hex}.tmp.xlsx"
    temporary_json = output_dir / f".{json_path.stem}_{uuid4().hex}.tmp.json"
    try:
        write_workbook(temporary_workbook, sheets, sheet_options=options)
        set_print_layout(temporary_workbook, last_row, breaks)
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
