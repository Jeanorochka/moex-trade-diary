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
from trade_exporter.domain.portfolio import CashFlow
from trade_exporter.brokers.portfolio import CashFlowHistory
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services import finam_service
from trade_exporter.services.analytics import analyze_file
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


def test_deferred_stock_report_preserves_partial_close_quantity_without_output(reports, tmp_path, monkeypatch):
    broker = Broker([
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 10, 1, "2025-12-31T12:00:00Z"),
        execution("sell", "SBER@MISX", "SIDE_SELL", 110, 4, "0.4", "2026-01-01T12:00:00Z"),
    ])
    exporter = ExcelExporter(tmp_path / "diary")

    def fail(*args, **kwargs):
        raise AssertionError("Deferred preparation must not create a diary workbook")

    monkeypatch.setattr(exporter, "export", fail)
    monkeypatch.setattr(exporter, "_new_file_path", fail)
    monkeypatch.setattr(finam_service, "write_workbook", fail)
    portfolio = SimpleNamespace(summary={
        "portfolio_period": {"status": "unavailable", "base_currency": "RUB", "quality": ["synthetic"]},
    })
    path, report, sources = execute_finam(
        broker, exporter, ACCOUNT, START, END, UTC, portfolio_report=portfolio, write_output=False,
    )
    assert path is None
    assert sources == reports.calls == []
    assert not exporter.output_dir.exists()
    assert report.summary["net_pnl"] == Decimal("39.2")
    assert report.summary["headline_basis"] == "portfolio_period"
    assert portfolio.summary["trading_result"]["net_pnl"] == Decimal("39.2")
    assert report.details["closed_trades"]["количество"].to_list() == [4]
    assert report.details["closed_records"][0]["quantity"] == 4
    assert report.details["closed_records"][0]["commission"] == Decimal("0.8")
    assert report.details["executions"]["идентификатор сделки"].to_list() == ["sell"]
    assert [row["trade_id"] for row in report.details["raw_executions"]] == ["sell"]
    assert report.details["futures_margin"].is_empty()
    assert report.details["broker_fees"].is_empty()


def test_deferred_future_report_preserves_sources_in_requested_input_directory(reports, tmp_path, monkeypatch):
    reports.margins = [margin(date(2026, 1, 1), "75")]
    broker = Broker([
        execution("buy", "SiZ6@RTSX", "SIDE_BUY", 100000, 1, 1, "2025-12-31T12:00:00Z"),
        execution("sell", "SiZ6@RTSX", "SIDE_SELL", 100100, 1, 2, "2026-01-01T12:00:00Z"),
    ])
    exporter = ExcelExporter(tmp_path / "diary")

    def fail(*args, **kwargs):
        raise AssertionError("Deferred preparation must not create a diary workbook")

    monkeypatch.setattr(exporter, "_new_file_path", fail)
    monkeypatch.setattr(finam_service, "write_workbook", fail)
    source_dir = tmp_path / "inputs"
    path, report, sources = execute_finam(
        broker, exporter, ACCOUNT, START, END, UTC, write_output=False, source_report_dir=source_dir,
    )
    assert path is None
    assert not exporter.output_dir.exists()
    assert report.summary["net_pnl"] == Decimal("73")
    assert report.details["source_reports"] == sources
    assert len(sources) == 3
    assert all(source.parent == source_dir and source.is_file() for source in sources)
    assert all(source.read_bytes() == b"raw original broker report" for source in sources)
    assert report.details["futures_margin"]["gross_pnl"].to_list() == ["75"]
    assert report.details["closed_trades"].is_empty()


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
    assert {"Сделки", "Исполнения ФИНАМ", "Фьючерсы", "Торговый результат", "Результаты", "Основа расчёта"} <= set(workbook)
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
    assert reports.calls[0] == ("123", datetime(2025, 12, 31, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), 1)


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


def without_fee(trade):
    result = deepcopy(trade)
    result.pop("commission")
    return result


def fee_history(monkeypatch, flows=(), *, complete=True, reasons=()):
    calls = []

    def load(broker, account, start, end, *, require_inception):
        calls.append((account.id, start, end, require_inception))
        return CashFlowHistory(tuple(flows), complete, tuple(reasons), start)

    monkeypatch.setattr(finam_service, "load_cash_flows", load)
    return calls


def test_missing_trade_commissions_use_period_cash_expenses_without_fabricating_trade_fees(reports, tmp_path, monkeypatch):
    trades = [
        without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 10, 1, "2025-12-31T12:00:00Z")),
        without_fee(execution("sell", "SBER@MISX", "SIDE_SELL", 110, 10, 1, "2026-01-01T12:00:00Z")),
    ]
    broker = Broker(trades)
    calls = fee_history(monkeypatch, [
        CashFlow(START + timedelta(hours=15), Decimal("-2.5"), "RUB", "fee", "fee1"),
        CashFlow(START + timedelta(hours=16), Decimal("1000"), "RUB", "external", "deposit"),
        CashFlow(START - timedelta(hours=1), Decimal("-99"), "RUB", "fee", "outside"),
        CashFlow(END, Decimal("-99"), "RUB", "fee", "end"),
    ])
    path, report, sources = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC, Decimal("1000"))
    assert report.summary["gross_pnl"] == 100
    assert report.summary["commission"] == Decimal("2.5")
    assert report.summary["net_pnl"] == Decimal("97.5")
    assert report.summary["commission_complete"] is True
    assert report.summary["commission_basis"] == "transactions_period"
    assert report.trades["event_type"].to_list() == ["stock_fifo", "broker_fee"]
    assert calls == [("123", START, END, False)]
    assert sources == reports.calls == []
    assert "commission" not in broker.trades[0]
    sheets = pl.read_excel(path, sheet_id=0, engine="calamine")
    assert sheets["Сделки"]["комиссия"].to_list() == [None]
    assert sheets["Сделки"]["финрез net"].to_list() == [None]
    assert sheets["Исполнения ФИНАМ"]["комиссия"].to_list() == [None]
    assert sheets["Исполнения ФИНАМ"]["статус комиссии"].to_list() == ["не передана API"]
    assert sheets["Денежные комиссии"]["сумма"].to_list() == [-2.5]
    assert "Риск и доходность" in sheets
    offline = analyze_file(path)
    assert offline.summary["net_pnl"] == Decimal("97.5")
    assert offline.summary["commission_complete"] is True
    assert offline.summary["commission_basis"] == "transactions_period"


def test_deferred_cash_fee_report_keeps_exact_cost_and_unknown_per_trade_fees(reports, tmp_path, monkeypatch):
    broker = Broker([
        without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z")),
        without_fee(execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 1, "2026-01-01T12:00:00Z")),
    ])
    fee = Decimal("-2.500000000000000001")
    fee_history(monkeypatch, [CashFlow(START, fee, "RUB", "fee", "fee")])
    exporter = ExcelExporter(tmp_path / "diary")
    path, report, sources = execute_finam(
        broker, exporter, ACCOUNT, START, END, UTC, write_output=False,
    )
    assert path is None
    assert sources == []
    assert not exporter.output_dir.exists()
    assert report.summary["net_pnl"] == Decimal("7.499999999999999999")
    assert report.details["broker_fees"]["сумма"].to_list() == [fee]
    assert report.details["closed_trades"]["комиссия"].to_list() == [None]
    assert report.details["closed_records"][0]["commission"] is None
    assert report.details["closed_records"][0]["net_pnl"] is None


def test_one_missing_historical_commission_switches_all_fees_to_cash_basis(reports, tmp_path, monkeypatch):
    broker = Broker([
        without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 9, "2025-12-31T12:00:00Z")),
        execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 9, "2026-01-01T12:00:00Z"),
    ])
    fee_history(monkeypatch, [CashFlow(START, Decimal("-2"), "RUB", "fee", "fee")])
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["commission"] == 2
    assert report.summary["net_pnl"] == 8
    assert pl.read_excel(path, sheet_name="Исполнения ФИНАМ")["комиссия"].to_list() == [9]


def test_missing_futures_fees_are_not_counted_twice_with_transactions(reports, tmp_path, monkeypatch):
    reports.margins = [margin(date(2026, 1, 1), 75)]
    broker = Broker([
        without_fee(execution("old", "SiZ6@RTSX", "SIDE_BUY", 100, 1, 9, "2025-12-31T12:00:00Z", "USD")),
        execution("sell", "SiZ6@RTSX", "SIDE_SELL", 110, 1, 9, "2026-01-01T12:00:00Z", "USD"),
    ])
    fee_history(monkeypatch, [CashFlow(START, Decimal("-3"), "RUB", "fee", "fee")])
    _, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["gross_pnl"] == 75
    assert report.summary["commission"] == 3
    assert report.summary["net_pnl"] == 72
    assert report.trades.filter(pl.col("event_type") == "futures_margin")["commission"].to_list() == [0]


def test_cash_fee_refund_preserves_signed_economic_effect(reports, tmp_path, monkeypatch):
    broker = Broker([without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"))])
    fee_history(monkeypatch, [
        CashFlow(START, Decimal("-5"), "RUB", "fee", "charge"),
        CashFlow(START, Decimal("2"), "RUB", "fee", "refund"),
    ])
    _, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["gross_pnl"] == 2
    assert report.summary["commission"] == 5
    assert report.summary["net_pnl"] == -3


def test_unavailable_transactions_export_gross_with_explicit_incomplete_costs(reports, tmp_path, monkeypatch):
    broker = Broker([
        without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z")),
        without_fee(execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 1, "2026-01-01T12:00:00Z")),
    ])

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic unavailable")

    monkeypatch.setattr(finam_service, "load_cash_flows", unavailable)
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert path.is_file()
    assert report.summary["net_pnl"] == 10
    assert report.summary["commission_complete"] is False
    assert report.summary["net_pnl_basis"] == "gross_less_known_fees"
    assert report.summary["commission_quality"]
    assert all("Комиссии неполны" in metric["quality_reason"] for metric in report.summary["risk"]["metrics"])
    explanation = pl.read_excel(path, sheet_name="Основа расчёта")
    assert any("частичным" in text for text in explanation["основа расчёта"])
    offline = analyze_file(path)
    assert offline.summary["net_pnl"] == 10
    assert offline.summary["commission_complete"] is False
    assert offline.summary["commission_quality"] == report.summary["commission_quality"]


def test_incomplete_or_foreign_fee_history_retains_only_known_ruble_costs(reports, tmp_path, monkeypatch):
    broker = Broker([without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"))])
    fee_history(monkeypatch, [
        CashFlow(START, Decimal("-2"), "RUB", "fee", "ruble"),
        CashFlow(START, Decimal("-3"), "USD", "fee", "foreign"),
    ], complete=False, reasons=["Неизвестный тип денежной операции внутри периода"])
    _, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["commission"] == 2
    assert report.summary["net_pnl"] == -2
    assert report.summary["commission_complete"] is False
    assert any("USD" in reason for reason in report.summary["commission_quality"])
    assert any("Неизвестный тип" in reason for reason in report.summary["commission_quality"])


def test_complete_execution_fees_do_not_request_cash_history(reports, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Complete execution fees must keep the existing FIFO basis")

    monkeypatch.setattr(finam_service, "load_cash_flows", unexpected)
    broker = Broker([
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z"),
        execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 1, "2026-01-01T12:00:00Z"),
    ])
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["commission_complete"] is True
    assert report.summary["commission_basis"] == "execution_fifo_and_futures"
    assert report.summary["net_pnl"] == 8
    assert "Риск и доходность" in pl.read_excel(path, sheet_id=0)


def test_empty_complete_cash_history_confirms_zero_period_expense(reports, tmp_path, monkeypatch):
    broker = Broker([without_fee(execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z"))])
    fee_history(monkeypatch)
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["commission_complete"] is True
    assert report.summary["commission"] == 0
    assert report.summary["commission_quality"] == []
    assert report.trades.is_empty()
    assert pl.read_excel(path, sheet_name="Исполнения ФИНАМ")["комиссия"].to_list() == [None]


def test_local_execution_ids_are_explained_and_preserved_offline(reports, tmp_path):
    broker = Broker([execution("local:1", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z")])
    broker.history_quality = {"123": {"synthetic_trade_ids": 3}}
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["synthetic_trade_ids"] == 3
    assert "Брокер не передал ID для 3 исполнений" in report.summary["execution_quality"][0]
    basis = pl.read_excel(path, sheet_name="Основа расчёта")
    assert any("одинаковые исполнения сохранены" in text for text in basis["основа расчёта"])
    offline = analyze_file(path)
    assert offline.summary["execution_quality"] == report.summary["execution_quality"]
    assert offline.summary["synthetic_trade_ids"] == 3


class PriceBroker(Broker):
    def __init__(self, trades, multipliers=None):
        super().__init__(trades)
        self.assets["BOND@MISX"] = {"type": "BONDS", "ticker": "BOND", "mic": "MISX"}
        self._price_multipliers = multipliers or {}

    def _price_factor(self, symbol, asset, executed_at):
        self.factor_calls.append(symbol)
        return FinamBroker._price_factor(self, symbol, asset, executed_at)


def test_irrelevant_old_bond_does_not_block_selected_stock_reconstruction(reports, tmp_path):
    broker = PriceBroker([
        execution("oldbond", "BOND@MISX", "SIDE_BUY", 100, 1, 1, "2025-06-01T12:00:00Z"),
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z"),
        execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 1, "2026-01-01T12:00:00Z"),
    ])
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["net_pnl"] == 8
    assert report.summary["result_complete"] is True
    assert report.summary["result_quality"] == []
    assert "BOND@MISX" not in broker.factor_calls
    assert path.exists()


def test_selected_unsupported_bond_preserves_executions_and_marks_partial_result(reports, tmp_path):
    broker = PriceBroker([
        execution("bondbuy", "BOND@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-30T12:00:00Z"),
        execution("buy", "SBER@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z"),
        execution("bondsell", "BOND@MISX", "SIDE_SELL", 101, 1, 1, "2026-01-01T12:00:00Z"),
        execution("sell", "SBER@MISX", "SIDE_SELL", 110, 1, 1, "2026-01-01T13:00:00Z"),
    ])
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["net_pnl"] == 8
    assert report.summary["result_complete"] is False
    assert report.summary["excluded_instruments"] == ["BOND@MISX"]
    assert "price_multipliers" in report.summary["result_quality"][0]
    assert report.trades["ticker"].to_list() == ["SBER@MISX"]
    assert all("BOND@MISX" in item["quality_reason"] for item in report.summary["risk"]["metrics"])
    sheets = pl.read_excel(path, sheet_id=0)
    assert set(sheets["Исполнения ФИНАМ"]["инструмент"]) == {"BOND@MISX", "SBER@MISX"}
    assert any("весь инструмент исключён" in reason for reason in sheets["Основа расчёта"]["основа расчёта"])
    offline = analyze_file(path)
    assert offline.summary["result_complete"] is False
    assert offline.summary["result_quality"] == report.summary["result_quality"]
    assert offline.summary["excluded_instruments"] == ["BOND@MISX"]


def test_selected_configured_bond_keeps_confirmed_monetary_multiplier(reports, tmp_path):
    broker = PriceBroker([
        execution("buy", "BOND@MISX", "SIDE_BUY", 100, 1, 1, "2025-12-31T12:00:00Z"),
        execution("sell", "BOND@MISX", "SIDE_SELL", 101, 1, 1, "2026-01-01T12:00:00Z"),
    ], {"BOND@MISX": Decimal("10")})
    _, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.summary["gross_pnl"] == 10
    assert report.summary["net_pnl"] == 8
    assert report.summary["result_complete"] is True


def test_nkd_in_later_fill_excludes_all_previous_fills_of_that_symbol(reports, tmp_path):
    accrued = execution("last", "BOND@MISX", "SIDE_SELL", 102, 6, 1, "2026-01-02T12:00:00Z")
    accrued["accrued_interest"] = {"value": "2"}
    broker = PriceBroker([
        execution("buy", "BOND@MISX", "SIDE_BUY", 100, 10, 1, "2025-12-31T12:00:00Z"),
        execution("first", "BOND@MISX", "SIDE_SELL", 101, 4, 1, "2026-01-01T12:00:00Z"),
        accrued,
    ], {"BOND@MISX": Decimal("10")})
    path, report, _ = execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
    assert report.trades.is_empty()
    assert report.summary["result_complete"] is False
    assert "НКД" in report.summary["result_quality"][0]
    assert pl.read_excel(path, sheet_name="Исполнения ФИНАМ").height == 2


def test_generic_api_error_is_not_misclassified_as_unsupported_instrument(reports, tmp_path, monkeypatch):
    broker = PriceBroker([execution("buy", "BOND@MISX", "SIDE_BUY", 100, 1, 1, "2026-01-01T12:00:00Z")])

    def unavailable(*args):
        raise RuntimeError("Finam API 401: unavailable")

    monkeypatch.setattr(broker, "_price_factor", unavailable)
    with pytest.raises(RuntimeError, match="Finam API 401"):
        execute_finam(broker, ExcelExporter(tmp_path), ACCOUNT, START, END, UTC)
