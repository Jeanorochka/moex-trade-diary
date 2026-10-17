from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from trade_exporter.brokers.finam import FinamBroker
from trade_exporter.domain.models import Account
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services import finam_service
from trade_exporter.services.finam_service import execute_finam


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 3, tzinfo=UTC)
ACCOUNT = Account("123", "Finam", "finam", opened_at=datetime(2025, 1, 1, tzinfo=UTC))


def execution(trade_id, symbol, side, price, quantity, commission, timestamp, currency="RUB", fee_currency="RUB"):
    return {
        "trade_id": trade_id, "symbol": symbol, "side": side,
        "price": {"value": str(price)}, "size": {"value": str(quantity)},
        "commission": {"value": {"value": str(commission)}, "currency": fee_currency},
        "timestamp": timestamp, "currency": currency, "account_id": "123",
    }


class Broker:
    name = "finam"
    _decimal = staticmethod(FinamBroker._decimal)
    _timestamp = staticmethod(FinamBroker._timestamp)
    _fill = FinamBroker._fill

    def __init__(self, trades, assets=None):
        self.settings = {}
        self.trades = trades
        self.load_calls = []
        self.factor_calls = []
        self.assets = assets or {
            "SBER@MISX": {"type": "EQUITIES", "ticker": "SBER", "mic": "MISX"},
            "SiZ6@RTSX": {"type": "FUTURES", "ticker": "SiZ6", "mic": "RTSX", "name": "Si-12.26"},
        }

    def load_trades(self, account_id, date_from, date_to):
        self.load_calls.append((account_id, date_from, date_to))
        return deepcopy(self.trades)

    def _asset(self, account_id, symbol):
        return self.assets[symbol]

    def _price_factor(self, symbol, asset, executed_at):
        self.factor_calls.append(symbol)
        if asset["type"] == "FUTURES":
            raise AssertionError("Future absolute execution prices must not become monetary PnL")
        return Decimal("1")

    def _price_currency(self, symbol, asset, executed_at, currency):
        return currency


@pytest.fixture
def reports(monkeypatch, tmp_path):
    state = SimpleNamespace(calls=[], margins=[], files=[])

    class Reports:
        def __init__(self, broker, output_dir):
            self.report_dates = {}
            self.output_dir = Path(output_dir)

        def download(self, account_id, date_from, date_to_exclusive, *, chunk_days=92):
            state.calls.append((account_id, date_from, date_to_exclusive, chunk_days))
            day = date_from
            paths = []
            while day < date_to_exclusive:
                path = self.output_dir / f"source_{day:%Y%m%d}.xlsx"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"raw original broker report")
                self.report_dates[path] = day.date()
                paths.append(path)
                day += timedelta(days=1)
            state.files.extend(paths)
            return paths

    def parse(paths, *, report_dates):
        state.report_dates = report_dates
        return pl.DataFrame(state.margins, schema={
            "date": pl.Date, "ticker": pl.String, "gross_pnl": pl.String,
            "currency": pl.String, "source_id": pl.String, "source_file": pl.String,
        })

    monkeypatch.setattr(finam_service, "FinamReports", Reports)
    monkeypatch.setattr(finam_service, "parse_finam_margins", parse)
    return state


def margin(day, gross, ticker="SiZ6", currency="RUB", source_id=None):
    return {
        "date": day, "ticker": ticker, "gross_pnl": str(gross), "currency": currency,
        "source_id": source_id or f"{day}-{ticker}", "source_file": "report.xlsx",
    }


def test_stock_only_fifo_handles_partial_closes_without_requesting_reports(reports, tmp_path):
    broker = Broker([
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 10, 1, "2025-12-31T12:00:00Z"),
        execution("sell1", "SBER@MISX", "SIDE_SELL", 110, 4, "0.4", "2026-01-01T12:00:00Z"),
        execution("sell2", "SBER@MISX", "SIDE_SELL", 120, 6, "0.6", "2026-01-02T12:00:00Z"),
    ])
    path, analysis, sources = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert path.is_file()
    assert sources == reports.calls == []
    assert analysis.summary["gross_pnl"] == Decimal("160")
    assert analysis.summary["commission"] == Decimal("2")
    assert analysis.summary["net_pnl"] == Decimal("158")
    assert analysis.trades["event_type"].to_list() == ["stock_fifo", "stock_fifo"]
    assert analysis.trades["ticker"].to_list() == ["SBER@MISX", "SBER@MISX"]
    assert broker.load_calls == [("123", ACCOUNT.opened_at, END)]


def test_whole_portfolio_combines_stock_fifo_and_report_vm_with_actual_futures_fees(reports, tmp_path):
    reports.margins = [
        margin(date(2025, 12, 31), 999999),
        margin(date(2026, 1, 1), 150),
        margin(date(2026, 1, 2), 50, ticker="Si-12.26"),
    ]
    broker = Broker([
        execution("stockbuy", "SBER@MISX", "SIDE_BUY", 100, 10, 1, "2025-12-31T12:00:00Z"),
        execution("stock1", "SBER@MISX", "SIDE_SELL", 110, 4, "0.4", "2026-01-01T12:00:00Z"),
        execution("stock2", "SBER@MISX", "SIDE_SELL", 120, 6, "0.6", "2026-01-02T12:00:00Z"),
        execution("futurebuy", "SiZ6@RTSX", "SIDE_BUY", 100000, 1, 1, "2025-12-31T13:00:00Z", "USD"),
        execution("futuresell", "SiZ6@RTSX", "SIDE_SELL", 100100, 1, 2, "2026-01-01T13:00:00Z", "USD"),
        execution("futureopen", "SiZ6@RTSX", "SIDE_BUY", 100200, 1, 3, "2026-01-02T13:00:00Z", "USD"),
    ])
    path, analysis, sources = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert analysis.summary["gross_pnl"] == Decimal("360")
    assert analysis.summary["commission"] == Decimal("7")
    assert analysis.summary["net_pnl"] == Decimal("353")
    assert set(broker.factor_calls) == {"SBER@MISX"}
    assert reports.calls == [("123", START - timedelta(days=1), END, 1)]
    assert len(sources) == 3
    assert all(source.read_bytes() == b"raw original broker report" for source in sources)
    assert reports.report_dates[sources[0]] == date(2025, 12, 31)
    workbook = pl.read_excel(path, sheet_id=0, engine="calamine")
    assert {"Сделки", "Исполнения ФИНАМ", "Фьючерсы", "Итог портфеля", "Результаты", "Основа расчёта"} <= set(workbook)
    assert workbook["Сделки"].height == 2
    assert workbook["Исполнения ФИНАМ"].height == 4
    assert workbook["Фьючерсы"].height == 2
    assert workbook["Фьючерсы"]["ticker"].to_list() == ["SiZ6@RTSX", "SiZ6@RTSX"]


def test_paid_vm_is_used_instead_of_absolute_price_times_changing_factor(reports, tmp_path):
    reports.margins = [margin(date(2026, 1, 1), "1250")]
    broker = Broker([
        execution("buy", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 2, "2025-12-31T12:00:00Z", "USD"),
        execution("sell", "SiZ6@RTSX", "SIDE_SELL", 101, 1, 3, "2026-01-01T12:00:00Z", "USD"),
    ])
    _, analysis, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert analysis.summary["gross_pnl"] == Decimal("1250")
    assert analysis.summary["net_pnl"] == Decimal("1247")
    assert broker.factor_calls == []
    assert analysis.trades["entry_price"].null_count() == analysis.trades.height
    assert analysis.trades["exit_price"].null_count() == analysis.trades.height


def test_commission_only_day_is_retained_when_no_margin_row_exists(reports, tmp_path):
    broker = Broker([
        execution("open", "SiZ6@RTSX", "SIDE_BUY", 100, 1, "2.25", "2026-01-02T12:00:00Z", "USD"),
    ])
    _, analysis, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert analysis.summary["gross_pnl"] == 0
    assert analysis.summary["commission"] == Decimal("2.25")
    assert analysis.summary["net_pnl"] == Decimal("-2.25")
    assert analysis.trades["event_type"].to_list() == ["futures_fee"]
    assert analysis.daily["date"].to_list() == [date(2026, 1, 2)]


def test_margin_on_open_future_is_included_even_without_selected_execution(reports, tmp_path):
    reports.margins = [margin(date(2026, 1, 1), 75)]
    broker = Broker([
        execution("open", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z", "USD"),
    ])
    _, analysis, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert analysis.summary["net_pnl"] == Decimal("75")
    assert analysis.summary["commission"] == 0


def test_open_future_position_without_any_trade_history_still_requests_margin_reports(reports, tmp_path):
    reports.margins = [margin(date(2026, 1, 1), 88)]
    broker = Broker([])
    broker._account_positions = {"123": [{"symbol": "SiZ6@RTSX", "quantity": {"value": "2"}}]}
    path, analysis, sources = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert path.is_file()
    assert analysis.summary["net_pnl"] == Decimal("88")
    assert len(sources) == 3
    assert reports.calls


def test_full_reconstruction_requires_known_opening_or_first_trade_date(reports, tmp_path):
    account = Account("123", "Finam", "finam")
    broker = Broker([])
    with pytest.raises(ValueError, match="для полной истории нужна дата"):
        execute_finam(broker, ExcelExporter(tmp_path), account, START, END, UTC)
    assert broker.load_calls == []


@pytest.mark.parametrize("ticker", ["NOT_FOUND", "same label"])
def test_unknown_and_ambiguous_report_contracts_fail_instead_of_guessing(reports, tmp_path, ticker):
    reports.margins = [margin(date(2026, 1, 1), 75, ticker=ticker)]
    assets = {
        "SiZ6@RTSX": {"type": "FUTURES", "name": "same label"},
        "EuZ6@RTSX": {"type": "FUTURES", "name": "same label"},
    }
    broker = Broker([
        execution("a", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"),
        execution("b", "EuZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T13:00:00Z"),
    ], assets)
    with pytest.raises(RuntimeError, match="однозначно сопоставить"):
        execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)


def test_exact_native_symbol_disambiguates_shared_contract_labels(reports, tmp_path):
    reports.margins = [margin(date(2026, 1, 1), 75, ticker="SiZ6@RTSX")]
    assets = {
        "SiZ6@RTSX": {"type": "FUTURES", "name": "same label"},
        "EuZ6@RTSX": {"type": "FUTURES", "name": "same label"},
    }
    broker = Broker([
        execution("a", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"),
        execution("b", "EuZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T13:00:00Z"),
    ], assets)
    assert execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)[1].summary["net_pnl"] == Decimal("73")


@pytest.mark.parametrize("source", ["margin", "fee"])
def test_nonruble_future_financial_results_are_rejected(reports, tmp_path, source):
    reports.margins = [margin(date(2026, 1, 1), 75, currency="USD" if source == "margin" else "RUB")]
    broker = Broker([
        execution("a", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z", "USD", "USD" if source == "fee" else "RUB"),
    ])
    with pytest.raises(RuntimeError, match="RUB"):
        execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)


def test_nonruble_stock_closed_result_is_rejected(reports, tmp_path):
    broker = Broker([
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z", "USD", "USD"),
        execution("sell", "SBER@MISX", "SIDE_SELL", 101, 1, 1, "2026-01-01T12:00:00Z", "USD", "USD"),
    ])
    with pytest.raises(RuntimeError, match="акциями должны быть в RUB"):
        execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)


def test_report_baseline_uses_local_calendar_day(reports, tmp_path):
    zone = ZoneInfo("Europe/Moscow")
    start = datetime(2026, 1, 1, tzinfo=zone)
    end = datetime(2026, 1, 2, tzinfo=zone)
    broker = Broker([
        execution("a", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"),
    ])
    execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, start, end, zone)
    assert reports.calls[0] == ("123", datetime(2025, 12, 31, tzinfo=zone), end, 1)


def test_lookback_reconstruction_uses_configured_history_window(reports, tmp_path):
    broker = Broker([])
    execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC, reconstruction_mode="lookback", lookback_days=7)
    assert broker.load_calls[0][1] == START - timedelta(days=7)


def test_future_intraday_period_is_rejected_before_report_creation(reports, tmp_path):
    broker = Broker([
        execution("a", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"),
    ])
    with pytest.raises(ValueError, match="полные календарные дни"):
        execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START + timedelta(hours=1), END, UTC)
    assert reports.calls == []
