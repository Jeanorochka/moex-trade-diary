from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trade_exporter.domain.portfolio import CashBalance, CashFlow, PortfolioPosition, PortfolioSnapshot
from trade_exporter.services.portfolio import analyze_portfolio


UTC = timezone.utc
OPENED = datetime(2025, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def holding(**overrides):
    values = {
        "instrument_id": "SBER@MISX", "ticker": "SBER", "asset_type": "equities",
        "quantity": Decimal("1"), "average_price": Decimal("100"),
        "current_price": Decimal("120"), "price_currency": "RUB", "valuation_currency": "RUB",
    }
    values.update(overrides)
    return PortfolioPosition(**values)


def snapshot(**overrides):
    values = {
        "account_id": "123", "broker": "finam", "captured_at": NOW,
        "base_currency": "RUB", "equity": Decimal("120"),
        "positions": (holding(),), "source": "broker current account snapshot",
    }
    values.update(overrides)
    return PortfolioSnapshot(**values)


def test_year_old_unclosed_holding_has_current_valuation_and_unrealized_profit():
    report = analyze_portfolio(snapshot(), [CashFlow(OPENED, Decimal("100"), "RUB")], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["equity"] == Decimal("120")
    assert report.summary["unrealized_pnl"] == Decimal("20")
    assert report.summary["holdings_return"] == Decimal("0.2")
    assert report.summary["lifetime_profit"] == Decimal("20")
    assert report.summary["profit_to_net_funding_ratio"] == Decimal("0.2")
    assert report.summary["xirr"] == pytest.approx(0.2)
    assert report.positions["market_value"].to_list() == [Decimal("120")]
    assert report.positions["cost_basis"].to_list() == [Decimal("100")]
    assert report.positions["unrealized_return"].to_list() == [0.2]
    assert report.summary["valuation_residual"] == 0
    assert report.summary["valuation_reconciled"] is True
    assert "realized_pnl" not in report.summary


def test_deposits_are_not_portfolio_profit():
    report = analyze_portfolio(snapshot(equity=Decimal("150"), positions=(), cash=(CashBalance("RUB", Decimal("150")),)), [
        CashFlow(OPENED, Decimal("100"), "RUB"),
        CashFlow(OPENED + timedelta(days=180), Decimal("50"), "RUB"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["total_external_inflows"] == Decimal("150")
    assert report.summary["net_external_funding"] == Decimal("150")
    assert report.summary["lifetime_profit"] == Decimal("0")
    assert report.summary["xirr"] == pytest.approx(0, abs=1e-12)


def test_withdrawals_do_not_erase_profit():
    report = analyze_portfolio(snapshot(equity=Decimal("80"), positions=()), [
        CashFlow(OPENED, Decimal("100"), "RUB"), CashFlow(NOW, Decimal("-40"), "RUB"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["total_external_outflows"] == Decimal("40")
    assert report.summary["net_external_funding"] == Decimal("60")
    assert report.summary["lifetime_profit"] == Decimal("20")
    assert report.summary["xirr"] == pytest.approx(0.2)


def test_income_fees_and_taxes_are_not_deducted_twice_from_equity():
    report = analyze_portfolio(snapshot(equity=Decimal("108"), positions=()), [
        CashFlow(OPENED, Decimal("100"), "RUB"),
        CashFlow(NOW, Decimal("10"), "RUB", "income"),
        CashFlow(NOW, Decimal("-1"), "RUB", "fee"),
        CashFlow(NOW, Decimal("-1"), "RUB", "tax"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["income"] == 10
    assert report.summary["fees"] == report.summary["taxes"] == 1
    assert report.summary["lifetime_profit"] == 8
    assert report.summary["equity"] == 108


def test_short_position_uses_signed_cost_market_and_positive_absolute_short_exposure():
    report = analyze_portfolio(snapshot(
        equity=Decimal("140"), cash=(CashBalance("RUB", Decimal("300")),),
        positions=(holding(quantity=Decimal("-2"), current_price=Decimal("80")),),
    ))
    assert report.positions["market_value"].to_list() == [Decimal("-160")]
    assert report.positions["cost_basis"].to_list() == [Decimal("-200")]
    assert report.positions["unrealized_pnl"].to_list() == [Decimal("40")]
    assert report.positions["unrealized_return"].to_list() == [0.2]
    assert report.summary["long_market_value"] == 0
    assert report.summary["short_market_value"] == report.summary["gross_exposure"] == 160
    assert report.summary["valuation_residual"] == 0
    assert report.allocation["short_market_value"].to_list() == [Decimal("160")]
    assert report.allocation["short_weight"].to_list() == [1.0]
    assert report.positions["equity_weight"][0] < 0


def test_authoritative_equity_is_reconciled_without_inventing_missing_value():
    report = analyze_portfolio(snapshot(equity=Decimal("150"), cash=(CashBalance("RUB", Decimal("20")),)))
    assert report.summary["equity"] == 150
    assert report.summary["known_positions_market_value"] == 120
    assert report.summary["valuation_residual"] == 10
    assert report.summary["valuation_complete"] is False
    assert report.summary["valuation_components_complete"] is True
    assert report.summary["valuation_reconciliation_status"] == "unexplained_balance"
    assert report.summary["equity_authoritative"] is True
    assert report.summary["valuation_residual_basis"] == "equity_minus_known_base_currency_components"


def test_partial_valuation_does_not_present_known_subset_as_complete_total():
    report = analyze_portfolio(snapshot(equity=Decimal("200"), positions=(holding(), holding(
        instrument_id="UNKNOWN@MISX", ticker="UNKNOWN", current_price=None,
    ))))
    assert report.summary["positions_market_value"] is None
    assert report.summary["known_positions_market_value"] == 120
    assert report.summary["unrealized_pnl"] is None
    assert report.summary["known_unrealized_pnl"] == 20
    assert report.summary["holdings_return"] is None
    assert report.summary["valuation_complete"] is False
    assert report.summary["unvalued_positions_count"] == 1
    assert report.positions["weight"].to_list() == [None, None]


def test_foreign_currency_values_are_retained_but_not_added_to_ruble_totals():
    foreign = holding(quantity=Decimal("2"), average_price=Decimal("8"), current_price=Decimal("10"), price_currency="USD", valuation_currency="USD")
    report = analyze_portfolio(snapshot(equity=Decimal("1000"), positions=(foreign,), cash=(CashBalance("RUB", Decimal("900")), CashBalance("USD", Decimal("10")))), [
        CashFlow(OPENED, Decimal("10"), "USD"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.positions["market_value"].to_list() == [Decimal("20")]
    assert report.positions["unrealized_pnl"].to_list() == [Decimal("4")]
    assert report.summary["known_positions_market_value"] == report.summary["known_unrealized_pnl"] == 0
    assert report.summary["cash_base"] is None
    assert report.summary["known_cash_base"] == 900
    assert report.summary["positions_market_value"] is None
    assert report.summary["lifetime_profit"] is None
    assert report.summary["returns_complete"] is False
    assert report.cash.filter(report.cash["currency"] == "USD")["amount_base"].to_list() == [None]


def test_derivative_quote_is_not_asset_value_and_reported_vm_is_not_added_to_equity():
    position = holding(asset_type="FUTURES", average_price=Decimal("100000"), current_price=Decimal("101000"), market_value=None, unrealized_pnl=Decimal("10"), unrealized_source="broker pending variation margin")
    report = analyze_portfolio(snapshot(equity=Decimal("110"), cash=(CashBalance("RUB", Decimal("110")),), positions=(position,)))
    assert report.positions["market_value"].to_list() == [None]
    assert report.positions["cost_basis"].to_list() == [None]
    assert report.summary["equity"] == 110
    assert report.summary["unrealized_pnl"] == 10
    assert report.summary["known_positions_market_value"] == 0
    assert report.summary["valuation_residual"] == 0
    assert report.summary["gross_exposure"] == 0
    assert report.summary["holdings_return"] is None
    assert report.allocation.is_empty()


def test_derivative_broker_market_value_is_preserved_without_derivation():
    position = holding(asset_type="options", market_value=Decimal("37"), unrealized_pnl=Decimal("4"), valuation_source="broker option premium")
    report = analyze_portfolio(snapshot(equity=Decimal("37"), positions=(position,)))
    assert report.positions["market_value"].to_list() == [Decimal("37")]
    assert report.positions["valuation_source"].to_list() == ["broker option premium"]
    assert report.summary["equity"] == 37


def test_stale_quote_is_null_for_derived_current_valuation_when_age_limit_is_requested():
    report = analyze_portfolio(snapshot(positions=(holding(price_as_of=NOW - timedelta(days=10)),)), quote_max_age=timedelta(hours=24))
    assert report.positions["current_price"].to_list() == [None]
    assert report.positions["market_value"].to_list() == [None]
    assert report.positions["unrealized_pnl"].to_list() == [None]
    assert report.positions["valuation_status"].to_list() == ["stale_quote"]
    assert report.summary["stale_quotes_count"] == 1


def test_unknown_quote_time_remains_explicit_and_weekend_calendar_age_is_not_assumed_stale():
    report = analyze_portfolio(snapshot(positions=(holding(price_as_of=NOW - timedelta(days=3)), holding(instrument_id="SECOND", ticker="SECOND", price_as_of=None))))
    assert report.positions["market_value"].to_list() == [Decimal("120"), Decimal("120")]
    assert report.positions["price_status"].to_list() == ["as_of_quote", "timestamp_unknown"]
    assert report.summary["unknown_quote_times_count"] == 1


def test_authoritative_broker_values_are_preserved_even_when_unit_quote_is_stale():
    report = analyze_portfolio(snapshot(positions=(holding(price_as_of=NOW - timedelta(days=10), market_value=Decimal("119"), unrealized_pnl=Decimal("19")),)), quote_max_age=timedelta(days=1))
    assert report.positions["current_price"].to_list() == [None]
    assert report.positions["market_value"].to_list() == [Decimal("119")]
    assert report.positions["unrealized_pnl"].to_list() == [Decimal("19")]


def test_theoretical_pending_quantity_is_flagged_and_not_used_to_invent_asset_values():
    report = analyze_portfolio(snapshot(positions_include_pending_orders=True))
    assert report.summary["positions_include_pending_orders"] is True
    assert report.positions["market_value"].to_list() == [None]
    assert report.positions["cost_basis"].to_list() == [None]
    assert report.positions["valuation_status"].to_list() == ["pending_quantity"]


def test_average_price_in_different_currency_does_not_create_fake_unrealized_profit():
    report = analyze_portfolio(snapshot(positions=(holding(average_price_currency="USD"),)))
    assert report.positions["market_value"].to_list() == [Decimal("120")]
    assert report.positions["cost_basis"].to_list() == [None]
    assert report.positions["unrealized_pnl"].to_list() == [None]


def test_incomplete_ledger_never_implies_lifetime_profit_or_return():
    report = analyze_portfolio(snapshot(), [CashFlow(OPENED, Decimal("10"), "RUB")])
    assert report.summary["lifetime_profit"] is None
    assert report.summary["profit_to_net_funding_ratio"] is None
    assert report.summary["xirr"] is None
    assert report.summary["returns_complete"] is False


def test_same_day_xirr_is_undefined():
    report = analyze_portfolio(snapshot(captured_at=OPENED + timedelta(hours=1)), [CashFlow(OPENED, Decimal("100"), "RUB")], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["xirr"] is None
    assert report.summary["xirr_status"] == "same_day"


def test_multiple_irr_roots_are_reported_instead_of_arbitrary_root():
    later = OPENED + timedelta(days=730)
    report = analyze_portfolio(snapshot(captured_at=later, equity=Decimal("-132"), positions=()), [
        CashFlow(OPENED, Decimal("100"), "RUB"),
        CashFlow(OPENED + timedelta(days=365), Decimal("-230"), "RUB"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["xirr"] is None
    assert report.summary["xirr_status"] == "multiple_roots"
    assert report.summary["profit_to_net_funding_ratio"] is None


def test_no_inception_funding_does_not_get_an_irr():
    report = analyze_portfolio(snapshot(), [], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["xirr"] is None
    assert report.summary["xirr_status"] == "no_inception_funding"
    assert report.summary["lifetime_profit"] is None
    assert report.summary["returns_complete"] is False
    assert report.summary["funding_status"] == "missing_inception_funding"


def test_flow_id_deduplication_does_not_drop_legitimate_anonymous_transfers():
    flow = CashFlow(OPENED, Decimal("100"), "RUB", id="deposit")
    report = analyze_portfolio(snapshot(), [flow, flow, CashFlow(OPENED, Decimal("1"), "RUB"), CashFlow(OPENED, Decimal("1"), "RUB")])
    assert report.flows.height == 3
    assert report.summary["total_external_inflows"] == 102
    with pytest.raises(ValueError, match="одинаковым id"):
        analyze_portfolio(snapshot(), [flow, replace(flow, amount=Decimal("99"))])


def test_cash_is_aggregated_exactly_by_currency():
    report = analyze_portfolio(snapshot(cash=(CashBalance("rub", Decimal("0.1")), CashBalance("RUB", Decimal("0.2")))))
    assert report.cash["amount"].to_list() == [Decimal("0.3")]
    assert report.summary["cash_base"] == Decimal("0.3")


def test_foreign_internal_dividend_does_not_block_equity_based_lifetime_profit():
    report = analyze_portfolio(snapshot(), [
        CashFlow(OPENED, Decimal("100"), "RUB"),
        CashFlow(NOW, Decimal("1"), "USD", "income"),
    ], ledger_complete=True, ledger_start=OPENED)
    assert report.summary["lifetime_profit"] == Decimal("20")
    assert report.summary["xirr"] == pytest.approx(0.2)
    assert report.summary["returns_complete"] is True
    assert report.summary["flows_currency_complete"] is False
    assert report.summary["external_flows_currency_complete"] is True
    assert report.summary["income"] is None


def test_pending_position_unrealized_is_diagnostic_until_quantity_basis_is_known():
    report = analyze_portfolio(snapshot(positions=(holding(unrealized_pnl=Decimal("20")),), positions_include_pending_orders=True))
    assert report.summary["unrealized_pnl"] is None
    assert report.summary["known_unrealized_pnl"] == Decimal("20")
    assert report.summary["unrealized_complete"] is False


def test_unrelated_pending_order_does_not_block_verified_held_stock():
    report = analyze_portfolio(snapshot(positions=(
        holding(quantity_includes_pending_orders=False),
        holding(instrument_id="SiZ6@RTSX", ticker="SiZ6", asset_type="futures", quantity_includes_pending_orders=True),
    ), positions_include_pending_orders=True))
    assert report.positions["market_value"].to_list() == [Decimal("120"), None]
    assert report.positions["unrealized_pnl"].to_list() == [Decimal("20"), None]
    assert report.positions["quantity_includes_pending_orders"].to_list() == [False, True]
    assert report.summary["known_unrealized_pnl"] == Decimal("20")


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), True, None])
def test_invalid_financial_numbers_are_rejected(bad):
    with pytest.raises(ValueError):
        CashBalance("RUB", bad)
    with pytest.raises(ValueError):
        snapshot(equity=bad)


def test_immutable_models_and_invalid_signed_short_cost():
    with pytest.raises(FrozenInstanceError):
        holding().quantity = Decimal("2")
    with pytest.raises(ValueError, match="знак"):
        holding(quantity=Decimal("-1"), cost_basis=Decimal("100"))


def test_invalid_timestamps_and_duplicate_positions_are_rejected():
    with pytest.raises(ValueError, match="часовым поясом"):
        snapshot(captured_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="объединены"):
        snapshot(positions=(holding(), holding()))
    with pytest.raises(ValueError, match="котировки"):
        analyze_portfolio(snapshot(positions=(holding(price_as_of=NOW + timedelta(seconds=1)),)))
    with pytest.raises(ValueError, match="позже снимка"):
        analyze_portfolio(snapshot(), [CashFlow(NOW + timedelta(days=1), Decimal("1"), "RUB")])


def test_ledger_coverage_must_be_explicit_and_consistent():
    with pytest.raises(ValueError, match="дата начала"):
        analyze_portfolio(snapshot(), ledger_complete=True)
    with pytest.raises(ValueError, match="раньше заявленного"):
        analyze_portfolio(snapshot(), [CashFlow(OPENED - timedelta(days=1), Decimal("1"), "RUB")], ledger_start=OPENED)


def test_empty_portfolio_has_typed_tables_and_no_invented_returns():
    report = analyze_portfolio(snapshot(equity=Decimal("0"), positions=()))
    assert report.positions.is_empty()
    assert report.allocation.is_empty()
    assert report.cash.is_empty()
    assert report.flows.is_empty()
    assert report.summary["unrealized_pnl"] == 0
    assert report.summary["holdings_return"] is None
    assert report.summary["lifetime_profit"] is None


def test_etfs_get_ordinary_share_valuation():
    report = analyze_portfolio(snapshot(positions=(holding(asset_type="etfs"),)))
    assert report.positions["market_value"].to_list() == [Decimal("120")]
    assert report.summary["unrealized_pnl"] == Decimal("20")


def test_reconciliation_rounding_tolerance_is_explicit():
    report = analyze_portfolio(snapshot(equity=Decimal("120.01")))
    assert report.summary["valuation_reconciled"] is True
    assert report.summary["valuation_residual"] == Decimal("0.01")
    assert report.summary["valuation_residual_threshold"] == Decimal("0.01")


def test_complete_empty_zero_account_can_have_zero_lifetime_profit():
    report = analyze_portfolio(snapshot(equity=Decimal("0"), positions=()), ledger_complete=True, ledger_start=OPENED)
    assert report.summary["lifetime_profit"] == 0
    assert report.summary["funding_status"] == "empty_zero_equity"
