from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from trade_exporter.services.analytics import MONEY, analyze_results


def event(**values):
    return {
        "date": "2026-01-31",
        "ticker": "SBER",
        "gross_pnl": "100",
        "commission": "1",
        **values,
    }


def test_mixed_stock_and_margin_results_preserve_prices_and_event_types():
    report = analyze_results(pl.DataFrame([
        event(date="2026-02-02", ticker="SiH6", gross_pnl="0", commission="2",
              entry_price=None, exit_price=None, event_type="futures_fee"),
        event(entry_price="200", exit_price="210", event_type="stock_closed"),
        event(ticker="SiH6", gross_pnl="-50", commission="0",
              entry_price=None, exit_price=None, event_type="futures_margin"),
        event(date="2026-02-01", ticker="SiH6", gross_pnl="20", commission="0.5",
              entry_price=None, exit_price=None, event_type="futures_margin"),
        event(ticker="SiH6", gross_pnl="0", commission="3",
              entry_price=None, exit_price=None, event_type="futures_fee"),
    ]), Decimal("1000"))

    assert report.summary["gross_pnl"] == Decimal("70")
    assert report.summary["commission"] == Decimal("6.5")
    assert report.summary["net_pnl"] == Decimal("63.5")
    assert (report.summary["trades"], report.summary["wins"], report.summary["losses"]) == (5, 2, 3)
    assert report.summary["max_drawdown"] == Decimal("53")
    assert report.trades["event_type"].to_list() == [
        "stock_closed", "futures_margin", "futures_fee", "futures_margin", "futures_fee",
    ]
    assert report.trades["entry_price"].to_list() == [Decimal("200"), None, None, None, None]
    assert report.trades["exit_price"].to_list() == [Decimal("210"), None, None, None, None]
    assert report.daily["date"].to_list() == [date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 2)]
    assert report.daily["net_pnl"].to_list() == [Decimal("46"), Decimal("19.5"), Decimal("-2")]
    assert report.monthly["net_pnl"].to_list() == [Decimal("46"), Decimal("17.5")]
    assert report.by_ticker["ticker"].to_list() == ["SBER", "SiH6"]
    assert report.by_ticker["net_pnl"].to_list() == [Decimal("99"), Decimal("-35.5")]


def test_optional_prices_and_kind_are_nullable_instead_of_fabricated_zero():
    report = analyze_results(pl.DataFrame([event()]))
    assert report.trades["entry_price"].to_list() == [None]
    assert report.trades["exit_price"].to_list() == [None]
    assert report.trades["event_type"].to_list() == [None]
    assert report.trades.schema["entry_price"] == MONEY
    assert report.trades.schema["event_type"] == pl.String


def test_result_decimal_values_and_initial_drawdown_are_exact():
    frame = pl.DataFrame([
        event(gross_pnl=Decimal("0.3"), commission=Decimal("0.300000000000000001")),
        event(date="2026-02-01", gross_pnl=Decimal("0.000000000000000002"), commission=Decimal("0")),
    ], schema_overrides={"gross_pnl": pl.Object, "commission": pl.Object})
    report = analyze_results(frame, Decimal("1"))
    assert report.summary["net_pnl"] == Decimal("0.000000000000000001")
    assert report.summary["max_drawdown"] == Decimal("0.000000000000000001")
    assert report.summary["max_daily_drawdown"] == Decimal("0.000000000000000001")
    assert report.daily["equity"].to_list() == [
        Decimal("0.999999999999999999"), Decimal("1.000000000000000001"),
    ]


def test_header_only_result_ledger_has_typed_empty_tables():
    frame = pl.DataFrame(schema={name: pl.String for name in event()})
    report = analyze_results(frame)
    assert report.summary["trades"] == 0
    assert report.summary["net_pnl"] == Decimal("0")
    assert report.summary["win_rate"] is None
    assert report.daily.is_empty()
    assert report.monthly.is_empty()
    assert report.by_ticker.is_empty()
    assert report.trades.schema["entry_price"] == MONEY
    assert report.trades.schema["exit_price"] == MONEY
    assert report.trades.schema["event_type"] == pl.String


@pytest.mark.parametrize(("column", "value"), [
    ("gross_pnl", "NaN"),
    ("gross_pnl", "Infinity"),
    ("gross_pnl", "0.0000000000000000001"),
    ("gross_pnl", True),
    ("commission", None),
    ("commission", "-Infinity"),
    ("entry_price", "NaN"),
    ("exit_price", "Infinity"),
    ("date", "2026-02-30"),
    ("ticker", " "),
    ("event_type", " "),
    ("event_type", 1),
])
def test_invalid_result_values_fail_before_aggregation(column, value):
    with pytest.raises(ValueError, match=f"Строка 2, {column}"):
        analyze_results(pl.DataFrame([event(**{column: value})]))


@pytest.mark.parametrize("column", ["date", "ticker", "gross_pnl", "commission"])
def test_result_ledger_requires_all_financial_and_identity_columns(column):
    with pytest.raises(ValueError, match=column):
        analyze_results(pl.DataFrame([event()]).drop(column))
