from datetime import datetime, timezone
from pathlib import Path

from trade_exporter.brokers.base import BrokerAdapter
from trade_exporter.domain.models import Account, Side
from trade_exporter.services.export_service import ExportTradeHistory
from trade_exporter.services.matcher import FifoMatcher
from conftest import make_fill

UTC = timezone.utc


class FakeBroker(BrokerAdapter):
    name = "fake"

    def __init__(self, fills):
        self.fills = fills
        self.requested_from = None

    def list_accounts(self):
        return []

    def get_fills(self, account_id, date_from, date_to):
        self.requested_from = date_from
        return self.fills


class FakeExporter:
    def export(self, **kwargs):
        self.trades = kwargs["trades"]
        return Path("fake.xlsx")


def test_full_reconstruction_starts_from_account_open_and_filters_by_exit_date():
    account_open = datetime(2026, 9, 1, tzinfo=UTC)
    account = Account("1", "Test", "fake", opened_at=account_open)
    broker = FakeBroker([
        make_fill(9, Side.BUY, "100"),
        make_fill(10, Side.SELL, "110"),
    ])
    exporter = FakeExporter()
    service = ExportTradeHistory(
        broker=broker,
        matcher=FifoMatcher(),
        exporter=exporter,
        reconstruction_mode="full",
        lookback_days=30,
        display_timezone=UTC,
    )
    period_from = datetime(2026, 9, 11, 0, tzinfo=UTC)
    period_to = datetime(2026, 9, 12, 0, tzinfo=UTC)
    path, trades = service.execute(account, period_from, period_to)

    assert broker.requested_from == account_open
    assert path == Path("fake.xlsx")
    assert len(trades) == 1
