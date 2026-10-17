from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

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
    ):
        history_from = self._history_start(account, period_from)
        fills = self.broker.get_fills(
            account_id=account.id,
            date_from=history_from,
            date_to=period_to_exclusive,
        )
        all_closed = self.matcher.match(fills)
        selected = [
            trade
            for trade in all_closed
            if period_from <= trade.exit_at.astimezone(period_from.tzinfo) < period_to_exclusive
        ]

        options = {}
        report = analyze_trades(selected, self.display_timezone, self.starting_capital)
        self.analysis_report = report
        if self.starting_capital > 0 or self.portfolio_report is not None:
            report.summary["risk"] = analyze_result_risk(report.daily, self.starting_capital, period_from, period_to_exclusive)
            attach_portfolio_performance(report, self.portfolio_report)
            options["risk"] = report.summary["risk"]
            if "portfolio_period" in report.summary:
                options["portfolio_period"] = report.summary["portfolio_period"]
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
