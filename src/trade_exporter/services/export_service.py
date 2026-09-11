from __future__ import annotations

from datetime import datetime, timedelta

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services.matcher import FifoMatcher


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
    ):
        self.broker = broker
        self.matcher = matcher
        self.exporter = exporter
        self.reconstruction_mode = reconstruction_mode
        self.lookback_days = lookback_days
        self.display_timezone = display_timezone

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

        path = self.exporter.export(
            trades=selected,
            broker_name=self.broker.name,
            account_id=account.id,
            period_from=period_from,
            period_to_inclusive=period_to_exclusive - timedelta(microseconds=1),
            display_timezone=self.display_timezone,
        )
        return path, selected

    def _history_start(self, account: Account, period_from: datetime) -> datetime:
        if self.reconstruction_mode == "full" and account.opened_at is not None:
            return account.opened_at.astimezone(period_from.tzinfo)
        return period_from - timedelta(days=self.lookback_days)
