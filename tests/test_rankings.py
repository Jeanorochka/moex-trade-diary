from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import polars as pl
import pytest

from trade_exporter.domain.portfolio import PortfolioPosition, PortfolioSnapshot
from trade_exporter.services.analytics import analyze_frame, analyze_results
from trade_exporter.services.portfolio import analyze_portfolio
from trade_exporter.services.rankings import (
    ASSET_GAINS, ASSET_LOSSES, OPEN_GAINS, OPEN_LOSSES, SCHEMA,
    TRADE_GAINS, TRADE_LOSSES, UNKNOWN_ASSETS, UNKNOWN_OPEN, build_rankings,
)


DAY = date(2026, 1, 1)
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def report(rows, **summary):
    records = [{
        "date": DAY, "ticker": ticker, "gross_pnl": gross, "commission": fee,
        "entry_price": "100" if event in {"stock_fifo", "stock_closed", "fifo_closed"} else None,
        "exit_price": "110" if event in {"stock_fifo", "stock_closed", "fifo_closed"} else None,
        "event_type": event,
    } for ticker, gross, fee, event in rows]
    frame = pl.DataFrame(records, schema={
        "date": pl.Date, "ticker": pl.String, "gross_pnl": pl.String, "commission": pl.String,
        "entry_price": pl.String, "exit_price": pl.String, "event_type": pl.String,
    })
    result = analyze_results(frame)
    result.summary.update(summary)
    return result


def section(frame, name):
    return frame.filter(pl.col("раздел") == name)


def test_asset_aggregation_and_fifo_rankings_use_different_populations():
    source = report([
        ("A", "-30", "1", "stock_fifo"), ("A", "10", "1", "stock_fifo"),
        ("B", "20", "1", "stock_fifo"), ("F", "100", "3", "futures_margin"),
        ("F", "0", "2", "futures_fee"),
    ])
    frame = build_rankings(source)
    assert section(frame, ASSET_LOSSES).select("тикер", "результат", "строк результата").rows() == [("A", Decimal("-22"), 2)]
    assert section(frame, ASSET_GAINS).select("тикер", "результат").rows() == [("F", Decimal("95")), ("B", Decimal("19"))]
    assert section(frame, TRADE_GAINS)["тикер"].to_list() == ["B", "A"]
    assert section(frame, TRADE_LOSSES)["результат"].to_list() == [Decimal("-31")]
    assert "F" not in frame.filter(pl.col("раздел").is_in([TRADE_GAINS, TRADE_LOSSES]))["тикер"].to_list()
    assert "не отдельная закрытая сделка" in section(frame, ASSET_GAINS)["примечание"][0]


def test_broker_fee_is_neither_an_asset_nor_a_trade_and_makes_costs_unallocated():
    source = report([
        ("A", "10", "20", "stock_fifo"), ("B", "5", "0", "stock_fifo"),
        ("КОМИССИИ ФИНАМ", "0", "1000", "broker_fee"),
    ], commission_complete=True)
    frame = build_rankings(source)
    assert "КОМИССИИ ФИНАМ" not in frame["тикер"].to_list()
    assert section(frame, TRADE_LOSSES).is_empty()
    assert section(frame, TRADE_GAINS).select("тикер", "результат").rows() == [("A", Decimal(10)), ("B", Decimal(5))]
    assert all("gross" in value for value in frame["основа"])
    assert all("не распределены" in value for value in frame["примечание"])


@pytest.mark.parametrize("metadata,phrase", [
    ({"commission_basis": "transactions_period", "commission_complete": True}, "не распределены"),
    ({"commission_complete": False}, "не подтверждены"),
])
def test_explicit_fee_metadata_controls_gross_basis_even_without_fee_rows(metadata, phrase):
    frame = build_rankings(report([("A", "10", "20", "stock_fifo")], **metadata))
    assert section(frame, TRADE_GAINS)["результат"].to_list() == [Decimal(10)]
    assert phrase in section(frame, TRADE_GAINS)["примечание"][0]


@pytest.mark.parametrize("event", ["stock_fifo", "stock_closed", "fifo_closed", "закрытие акций FIFO"])
def test_current_and_legacy_fifo_labels_are_recognized(event):
    source = report([("A", "-10", "0", "stock_fifo")])
    source = replace(source, trades=source.trades.with_columns(pl.lit(event).alias("event_type")))
    assert section(build_rankings(source), TRADE_LOSSES)["тикер"].to_list() == ["A"]


def test_legacy_russian_margin_and_fee_labels_keep_financial_meaning():
    source = report([
        ("F", "100", "0", "futures_margin"), ("F", "0", "2", "futures_fee"),
        ("EXPENSE", "0", "1000", "broker_fee"),
    ])
    source = replace(source, trades=source.trades.with_columns(pl.col("event_type").replace({
        "futures_margin": "вариационная маржа и комиссии", "futures_fee": "комиссии фьючерсов",
        "broker_fee": "денежные расходы и возвраты ФИНАМ",
    })))
    frame = build_rankings(source)
    assert section(frame, ASSET_GAINS).select("тикер", "результат").rows() == [("F", Decimal(100))]
    assert section(frame, TRADE_GAINS).is_empty()
    assert "EXPENSE" not in frame["тикер"].to_list()


def test_plain_closed_diary_without_event_type_is_fifo():
    source = analyze_frame(pl.DataFrame({
        "date": [DAY], "ticker": ["A"], "entry_price": ["100"], "exit_price": ["90"],
        "gross_pnl": ["-10"], "commission": ["1"],
    }))
    frame = build_rankings(source)
    assert section(frame, TRADE_LOSSES)["результат"].to_list() == [Decimal(-11)]
    assert "себестоимость FIFO" in section(frame, TRADE_LOSSES)["примечание"][0]


def test_duplicates_remain_separate_fifo_closures_and_aggregate_exactly():
    source = report([("A", "-10", "0", "stock_fifo"), ("A", "-10", "0", "stock_fifo")])
    frame = build_rankings(source)
    assert section(frame, ASSET_LOSSES)["результат"].to_list() == [Decimal(-20)]
    losses = section(frame, TRADE_LOSSES)
    assert losses["место"].to_list() == [1, 2]
    assert losses["примечание"][0].startswith("Строка результатов №1.")
    assert losses["примечание"][1].startswith("Строка результатов №2.")


def test_decimal_sign_and_order_do_not_disappear_at_eighteen_places():
    frame = build_rankings(report([
        ("A", "-0.000000000000000001", "0", "stock_fifo"),
        ("B", "0.000000000000000002", "0", "stock_fifo"),
    ]))
    assert section(frame, ASSET_LOSSES)["результат"][0] == Decimal("-1e-18")
    assert section(frame, ASSET_GAINS)["результат"][0] == Decimal("2e-18")
    assert frame.schema == SCHEMA


def test_limits_apply_per_section_and_do_not_turn_winners_into_losses():
    frame = build_rankings(report([
        ("B", "5", "0", "stock_fifo"), ("A", "5", "0", "stock_fifo"),
        ("C", "1", "0", "stock_fifo"), ("ZERO", "0", "0", "stock_fifo"),
    ]), limit=1)
    assert section(frame, ASSET_GAINS)["тикер"].to_list() == ["A"]
    assert section(frame, TRADE_GAINS)["тикер"].to_list() == ["A"]
    assert section(frame, ASSET_LOSSES).is_empty()
    assert section(frame, TRADE_LOSSES).is_empty()


def portfolio():
    positions = (
        PortfolioPosition("LOSS@MISX", "LOSS", "EQUITIES", Decimal(1), average_price=Decimal(100), current_price=Decimal(80)),
        PortfolioPosition("WIN@MISX", "WIN", "EQUITIES", Decimal(1), average_price=Decimal(100), current_price=Decimal(130)),
        PortfolioPosition("UNKNOWN@MISX", "UNKNOWN", "BONDS", Decimal(1)),
        PortfolioPosition("USD@XNYS", "USD", "EQUITIES", Decimal(1), unrealized_pnl=Decimal(5000), valuation_currency="USD", price_currency="USD"),
        PortfolioPosition("PENDING@MISX", "PENDING", "EQUITIES", Decimal(1), unrealized_pnl=Decimal(9999), quantity_includes_pending_orders=True),
        PortfolioPosition("STALE@MISX", "STALE", "EQUITIES", Decimal(1), unrealized_pnl=Decimal(6000), price_as_of=NOW - timedelta(days=30)),
        PortfolioPosition("ZERO@MISX", "ZERO", "EQUITIES", Decimal(0)),
    )
    return analyze_portfolio(PortfolioSnapshot("synthetic", "finam", NOW, "RUB", Decimal(1000), positions=positions), quote_max_age=timedelta(days=7))


def test_open_positions_are_separate_current_rankings_and_unknowns_are_not_zero():
    frame = build_rankings(None, portfolio())
    assert section(frame, OPEN_LOSSES).select("тикер", "результат").rows() == [("LOSS@MISX", Decimal(-20))]
    assert section(frame, OPEN_GAINS).select("тикер", "результат").rows() == [("WIN@MISX", Decimal(30))]
    missing = section(frame, UNKNOWN_OPEN)
    assert set(missing["тикер"]) == {"UNKNOWN@MISX", "USD@XNYS", "PENDING@MISX", "STALE@MISX"}
    assert missing["результат"].null_count() == 4
    assert missing["место"].null_count() == 4
    assert "ZERO@MISX" not in frame["тикер"].to_list()
    assert "не вклад инструмента" in section(frame, OPEN_GAINS)["примечание"][0]


def test_partial_and_excluded_instrument_results_are_not_healthy_zero_assets():
    frame = build_rankings(report([("A", "10", "0", "stock_fifo")], result_complete=False, excluded_instruments=["BOND@MISX"]))
    assert "Рейтинг частичный" in section(frame, ASSET_GAINS)["примечание"][0]
    missing = section(frame, UNKNOWN_ASSETS)
    assert missing["тикер"].to_list() == ["BOND@MISX"]
    assert missing["результат"].to_list() == [None]


def test_rankings_never_mutate_nav_or_add_unrealized_to_period_profit():
    source = report([("INHERITED", "-20", "0", "stock_fifo")])
    source.summary["headline_profit"] = Decimal(20)
    snapshot = portfolio()
    summary, portfolio_summary = deepcopy(source.summary), deepcopy(snapshot.summary)
    trades, positions = source.trades.clone(), snapshot.positions.clone()
    build_rankings(source, snapshot)
    assert source.summary == summary
    assert snapshot.summary == portfolio_summary
    assert source.trades.equals(trades)
    assert snapshot.positions.equals(positions)


def test_empty_reports_have_stable_numeric_schema():
    frame = build_rankings(report([]))
    assert frame.is_empty()
    assert frame.schema == SCHEMA
    assert build_rankings(None).schema == SCHEMA


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError, match="положительным целым"):
        build_rankings(report([]), limit=limit)
