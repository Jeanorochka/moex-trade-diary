from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

import polars as pl

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services.matcher import FifoMatcher
from trade_exporter.services.analytics import analyze_trades
from trade_exporter.services.risk import analyze_result_risk
from trade_exporter.services.performance import attach_portfolio_performance


class ExportTradeHistory:
    """Use-case: получить -> сопоставить -> отфильтровать -> экспортировать."""

    def __init__(
        self,
        broker: BrokerAdapter,
        matcher: FifoMatcher,
        exporter: ExcelExporter,
        reconstruction_mode: str,
        lookback_days: int,
        display_timezone,
        starting_capital: Decimal = Decimal(0),
        portfolio_report=None,
    ):
        self.broker = broker
        self.matcher = matcher
        self.exporter = exporter
        self.reconstruction_mode = reconstruction_mode
        self.lookback_days = lookback_days
        self.display_timezone = display_timezone
        self.starting_capital = starting_capital
        self.portfolio_report = portfolio_report
        self.analysis_report = None

    def execute(
        self,
        account: Account,
        period_from: datetime,
        period_to_exclusive: datetime,
        *,
        write_output: bool = True,
    ):
        history_from = self._history_start(account, period_from)
        fills = list(self.broker.get_fills(
            account_id=account.id,
            date_from=history_from,
            date_to=period_to_exclusive,
        ))
        all_closed = self.matcher.match(fills)
        selected = [
            trade
            for trade in all_closed
            if period_from <= trade.exit_at.astimezone(period_from.tzinfo) < period_to_exclusive
        ]
        selected_fills = [
            fill for fill in fills
            if period_from <= fill.executed_at.astimezone(period_from.tzinfo) < period_to_exclusive
        ]
        selected_currencies = {str(fill.currency or "").strip().upper() for fill in selected_fills}
        if self.portfolio_report is not None:
            base_currency = str(self.portfolio_report.summary.get("base_currency") or "RUB").strip().upper()
        else:
            base_currency = next(iter(selected_currencies)) if len(selected_currencies) == 1 else "RUB"
            base_currency = base_currency or "RUB"
        relevant = {trade.instrument_id for trade in selected} | {fill.instrument_id for fill in selected_fills}
        currencies = defaultdict(set)
        for fill in fills:
            if fill.instrument_id in relevant:
                currencies[fill.instrument_id].add(str(fill.currency or "").strip().upper())
        excluded = sorted(instrument for instrument, values in currencies.items() if values != {base_currency})
        quality = [
            f"{instrument}: валюты исполнений {', '.join(value or 'не указана' for value in sorted(currencies[instrument]))} "
            f"не подтверждают единый результат в {base_currency}; инструмент исключён из торгового P&L без исторического валютного пересчёта."
            for instrument in excluded
        ]
        excluded_closed = [trade for trade in selected if trade.instrument_id in excluded]
        selected = [trade for trade in selected if trade.instrument_id not in excluded]

        options = {}
        report = analyze_trades(selected, self.display_timezone, self.starting_capital)
        report.summary.update({
            "base_currency": base_currency, "result_complete": not excluded,
            "excluded_instruments": excluded, "result_quality": quality,
        })
        self.analysis_report = report
        if self.starting_capital > 0 or self.portfolio_report is not None:
            report.summary["risk"] = analyze_result_risk(
                report.daily, self.starting_capital, period_from, period_to_exclusive, base_currency=base_currency,
            )
            for reason in quality:
                report.summary["risk"]["quality"].append(reason)
                for metric in report.summary["risk"]["metrics"]:
                    metric["quality_reason"] = " ".join(filter(None, (metric.get("quality_reason"), reason)))
            attach_portfolio_performance(report, self.portfolio_report)
            options["risk"] = report.summary["risk"]
            if "portfolio_period" in report.summary:
                options["portfolio_period"] = report.summary["portfolio_period"]
        report.details.update({
            "executions": pl.DataFrame([{
                "дата": fill.executed_at.astimezone(self.display_timezone).strftime("%d.%m.%Y"),
                "время": fill.executed_at.isoformat(), "инструмент": fill.instrument_id,
                "тикер": fill.ticker, "сторона": fill.side.value, "количество": fill.quantity,
                "цена исполнения": float(fill.display_price), "денежная цена": float(fill.pnl_price),
                "комиссия": float(fill.commission), "валюта": fill.currency,
            } for fill in selected_fills], schema={
                "дата": pl.String, "время": pl.String, "инструмент": pl.String,
                "тикер": pl.String, "сторона": pl.String, "количество": pl.Int64,
                "цена исполнения": pl.Float64, "денежная цена": pl.Float64,
                "комиссия": pl.Float64, "валюта": pl.String,
            }),
            "raw_executions": [asdict(fill) for fill in selected_fills],
            "closed_trades": pl.DataFrame([{
                "дата": trade.exit_at.astimezone(self.display_timezone).strftime("%d.%m.%y"),
                "тикер": trade.ticker, "цена входа": float(trade.entry_price),
                "цена выхода": float(trade.exit_price), "финрез": float(trade.gross_pnl),
                "комиссия": float(trade.commission), "направление": trade.direction,
                "количество": trade.quantity, "финрез net": float(trade.net_pnl),
            } for trade in selected], schema={
                "дата": pl.String, "тикер": pl.String, "цена входа": pl.Float64,
                "цена выхода": pl.Float64, "финрез": pl.Float64, "комиссия": pl.Float64,
                "направление": pl.String, "количество": pl.Int64, "финрез net": pl.Float64,
            }),
            "closed_records": [{**asdict(trade), "net_pnl": trade.net_pnl} for trade in selected],
            "excluded_closed_records": [{**asdict(trade), "net_pnl": trade.net_pnl} for trade in excluded_closed],
            "source_reports": [],
        })
        if not write_output:
            return None, selected
        path = self.exporter.export(
            trades=selected,
            broker_name=self.broker.name,
            account_id=account.id,
            period_from=period_from,
            period_to_inclusive=period_to_exclusive - timedelta(microseconds=1),
            display_timezone=self.display_timezone,
            **options,
        )
        return path, selected

    def _history_start(self, account: Account, period_from: datetime) -> datetime:
        if self.reconstruction_mode == "full" and account.opened_at is not None:
            return account.opened_at.astimezone(period_from.tzinfo)
        return period_from - timedelta(days=self.lookback_days)
