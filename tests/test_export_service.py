from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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


def test_deferred_export_keeps_fifo_details_and_portfolio_attachment_without_writing():
    account_open = datetime(2026, 9, 1, tzinfo=UTC)
    account = Account("1", "Test", "fake", opened_at=account_open)
    broker = FakeBroker([
        replace(make_fill(9, Side.BUY, "100", qty=5, commission="0.5"), executed_at=datetime(2026, 9, 10, 9, tzinfo=UTC)),
        make_fill(10, Side.SELL, "110", qty=2, commission="0.2"),
    ])

    class NoOutput:
        def export(self, **kwargs):
            raise AssertionError("Deferred preparation must not create a diary workbook")

    portfolio = SimpleNamespace(summary={
        "portfolio_period": {"status": "unavailable", "base_currency": "RUB", "quality": ["synthetic"]},
    })
    service = ExportTradeHistory(
        broker, FifoMatcher(), NoOutput(), "full", 30, UTC,
        starting_capital=Decimal("1000"), portfolio_report=portfolio,
    )
    path, trades = service.execute(
        account, datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC), write_output=False,
    )
    assert path is None
    assert broker.requested_from == account_open
    assert len(trades) == 1
    assert trades[0].quantity == 2
    report = service.analysis_report
    assert report.summary["net_pnl"] == Decimal("19.6")
    assert report.summary["headline_basis"] == "portfolio_period"
    assert portfolio.summary["trading_result"]["net_pnl"] == Decimal("19.6")
    assert report.details["closed_trades"]["количество"].to_list() == [2]
    assert report.details["closed_records"][0]["commission"] == Decimal("0.4")
    assert report.details["closed_records"][0]["net_pnl"] == Decimal("19.6")
    assert report.details["executions"].height == 1
    assert [row["quantity"] for row in report.details["raw_executions"]] == [2]
    assert report.details["source_reports"] == []


def test_deferred_export_can_prepare_empty_period_without_creating_output():
    account = Account("1", "Test", "fake", opened_at=datetime(2026, 9, 1, tzinfo=UTC))
    exporter = FakeExporter()
    service = ExportTradeHistory(FakeBroker([]), FifoMatcher(), exporter, "full", 30, UTC)
    path, trades = service.execute(
        account, datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC), write_output=False,
    )
    assert path is None
    assert trades == []
    assert not hasattr(exporter, "trades")
    assert service.analysis_report.details["executions"].is_empty()
    assert service.analysis_report.details["closed_trades"].is_empty()


def test_ruble_portfolio_excludes_dollar_closures_without_dropping_raw_executions():
    fills = [
        make_fill(9, Side.BUY, "100", instrument_id="rub-stock", ticker="RUB"),
        make_fill(10, Side.SELL, "110", instrument_id="rub-stock", ticker="RUB"),
        replace(make_fill(9, Side.BUY, "100", instrument_id="usd-stock", ticker="USD"), currency="usd"),
        replace(make_fill(10, Side.SELL, "120", instrument_id="usd-stock", ticker="USD"), currency="usd"),
    ]
    portfolio = SimpleNamespace(summary={"base_currency": "RUB"})
    exporter = FakeExporter()
    service = ExportTradeHistory(FakeBroker(fills), FifoMatcher(), exporter, "full", 30, UTC, portfolio_report=portfolio)
    path, closed = service.execute(
        Account("1", "Test", "fake", opened_at=datetime(2026, 9, 1, tzinfo=UTC)),
        datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC),
    )
    report = service.analysis_report
    assert path == Path("fake.xlsx")
    assert [trade.instrument_id for trade in closed] == ["rub-stock"]
    assert exporter.trades == closed
    assert report.summary["base_currency"] == "RUB"
    assert report.summary["net_pnl"] == Decimal("10")
    assert report.summary["result_complete"] is False
    assert report.summary["excluded_instruments"] == ["usd-stock"]
    assert all("валют" in reason for reason in report.summary["result_quality"])
    assert report.details["executions"].height == 4
    assert {row["currency"] for row in report.details["raw_executions"]} == {"rub", "usd"}
    assert report.details["closed_trades"]["тикер"].to_list() == ["RUB"]
    assert report.details["excluded_closed_records"][0]["instrument_id"] == "usd-stock"
    assert report.details["excluded_closed_records"][0]["gross_pnl"] == Decimal("20")


def test_single_dollar_currency_without_portfolio_is_reported_as_dollars():
    fills = [replace(fill, currency="USD") for fill in (
        make_fill(9, Side.BUY, "100"), make_fill(10, Side.SELL, "120"),
    )]
    service = ExportTradeHistory(FakeBroker(fills), FifoMatcher(), FakeExporter(), "full", 30, UTC, starting_capital=Decimal("1000"))
    path, closed = service.execute(
        Account("1", "Test", "fake", opened_at=datetime(2026, 9, 1, tzinfo=UTC)),
        datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC), write_output=False,
    )
    report = service.analysis_report
    assert path is None
    assert len(closed) == 1
    assert report.summary["base_currency"] == "USD"
    assert report.summary["net_pnl"] == Decimal("20")
    assert report.summary["result_complete"] is True
    assert report.summary["excluded_instruments"] == []
    assert report.summary["result_quality"] == []
    assert next(row for row in report.summary["risk"]["metrics"] if row["metric"] == "proxy_net_pnl")["unit"] == "USD"
