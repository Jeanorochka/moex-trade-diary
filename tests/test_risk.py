from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from trade_exporter.domain.portfolio import CashFlow
from trade_exporter.services.risk import analyze_portfolio_risk, analyze_result_risk


UTC = timezone.utc
START = datetime(2025, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def summary(**overrides):
    values = {
        "account_id": "123", "broker": "finam", "captured_at": NOW.isoformat(),
        "base_currency": "RUB", "equity": Decimal("120"), "valuation_history": [],
        "gross_exposure": Decimal("120"), "allocation_complete": True,
        "positions_count": 1, "derivative_positions_count": 0,
    }
    values.update(overrides)
    return values


def metrics(report):
    return {item["metric"]: item for item in report["metrics"]}


def test_entered_capital_produces_immediate_return_without_second_snapshot():
    report = analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START)
    rows = metrics(report)
    assert rows["capital_period_profit"]["value"] == Decimal("20")
    assert rows["capital_period_return"]["value"] == pytest.approx(0.2)
    assert rows["capital_period_xirr"]["value"] == pytest.approx(0.2)
    assert rows["capital_period_annualized"]["value"] == pytest.approx(0.2)
    assert rows["capital_period_return"]["basis"] == "starting_capital_assumption"
    assert rows["max_drawdown_observed"]["value"] is None
    assert rows["historical_var95_1d_money"]["value"] is None
    assert "пользователем" in report["quality"][0]


def test_deposit_is_removed_from_profit_and_weighted_by_time():
    halfway = START + (NOW - START) / 2
    rows = metrics(analyze_portfolio_risk(summary(equity=Decimal("175")), [CashFlow(halfway, Decimal("50"), "RUB")], starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START))
    assert rows["capital_period_profit"]["value"] == Decimal("25")
    assert rows["capital_period_return"]["value"] == pytest.approx(0.2)
    assert rows["capital_value_change_unadjusted"]["value"] == Decimal("0.75")


def test_withdrawal_is_not_a_loss():
    rows = metrics(analyze_portfolio_risk(summary(equity=Decimal("80")), [CashFlow(NOW, Decimal("-40"), "RUB")], starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START))
    assert rows["capital_period_profit"]["value"] == Decimal("20")
    assert rows["capital_period_return"]["value"] == pytest.approx(0.2)
    assert rows["capital_period_xirr"]["value"] == pytest.approx(0.2)


def test_incomplete_flow_history_does_not_turn_value_change_into_net_return():
    rows = metrics(analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=False, ledger_start=START))
    assert rows["capital_value_change_unadjusted"]["value"] == Decimal("0.2")
    assert rows["capital_period_profit"]["value"] is None
    assert rows["capital_period_return"]["value"] is None
    assert "полная история" in rows["capital_period_return"]["quality_reason"]


def test_foreign_external_flow_blocks_adjusted_return_but_internal_dividend_does_not():
    external = CashFlow(START + timedelta(days=1), Decimal("10"), "USD")
    dividend = CashFlow(START + timedelta(days=1), Decimal("10"), "USD", "income")
    kwargs = dict(starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START)
    assert metrics(analyze_portfolio_risk(summary(), [external], **kwargs))["capital_period_return"]["value"] is None
    assert metrics(analyze_portfolio_risk(summary(), [dividend], **kwargs))["capital_period_return"]["value"] == pytest.approx(0.2)


def test_baseline_flow_coverage_must_start_no_later_than_entered_capital_date():
    rows = metrics(analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START + timedelta(days=1)))
    assert rows["capital_period_return"]["value"] is None


def test_actual_drawdown_uses_flow_adjusted_index_instead_of_raw_cash_drop():
    end = START + timedelta(days=1)
    history = [
        {"captured_at": START.isoformat(), "equity": "100", "currency": "RUB"},
        {"captured_at": end.isoformat(), "equity": "50", "currency": "RUB"},
    ]
    rows = metrics(analyze_portfolio_risk(summary(captured_at=end.isoformat(), equity=Decimal("50"), valuation_history=history), [CashFlow(end, Decimal("-50"), "RUB")], ledger_complete=True, ledger_start=START))
    assert rows["max_drawdown_observed"]["value"] == 0
    assert rows["daily_returns_count"]["value"] == 1


def test_irregular_nav_is_not_called_daily_returns():
    end = START + timedelta(days=10)
    history = [
        {"captured_at": START.isoformat(), "equity": "100", "currency": "RUB"},
        {"captured_at": end.isoformat(), "equity": "90", "currency": "RUB"},
    ]
    rows = metrics(analyze_portfolio_risk(summary(captured_at=end.isoformat(), equity=Decimal("90"), valuation_history=history), ledger_complete=True, ledger_start=START))
    assert rows["max_drawdown_observed"]["value"] == pytest.approx(0.1)
    assert rows["daily_returns_count"]["value"] == 0
    assert rows["historical_var95_1d_money"]["value"] is None


def test_historical_var95_requires_twenty_genuinely_daily_returns_and_var99_requires_hundred():
    history = [{"captured_at": START.isoformat(), "equity": "100", "currency": "RUB"}]
    equity = Decimal("100")
    for day in range(1, 21):
        equity *= Decimal("0.99") if day % 2 else Decimal("1.02")
        history.append({"captured_at": (START + timedelta(days=day)).isoformat(), "equity": str(equity), "currency": "RUB"})
    rows = metrics(analyze_portfolio_risk(summary(captured_at=(START + timedelta(days=20)).isoformat(), equity=equity, valuation_history=history), ledger_complete=True, ledger_start=START))
    assert rows["daily_returns_count"]["value"] == 20
    assert rows["historical_var95_1d_fraction"]["value"] == pytest.approx(0.01)
    assert float(rows["historical_var95_1d_money"]["value"]) == pytest.approx(float(equity) * 0.01)
    assert rows["historical_var99_1d_money"]["value"] is None
    assert "100" in rows["historical_var99_1d_money"]["quality_reason"]


def test_selected_results_proxy_includes_starting_loss_and_exposes_normal_var_heuristic():
    results = pl.DataFrame({"date": [date(2025, 1, 1), date(2025, 1, 2)], "net_pnl": ["-10", "5"]})
    rows = metrics(analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, results=results))
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(0.1)
    assert rows["proxy_daily_returns_count"]["value"] == 2
    assert rows["proxy_normal_var95_1d_money"]["value"] > 0
    assert rows["proxy_normal_var99_1d_money"]["value"] > rows["proxy_normal_var95_1d_money"]["value"]
    assert rows["proxy_normal_var95_1d_money"]["basis"] == "closed_pnl_proxy"
    assert "Нормальная эвристика" in rows["proxy_normal_var95_1d_money"]["quality_reason"]
    assert rows["proxy_var95_1d_money"]["value"] is None
    assert "20" in rows["proxy_var95_1d_money"]["quality_reason"]


def test_proxy_aggregates_rows_per_date_and_fills_missing_result_days_with_zero():
    results = pl.DataFrame({"date": [date(2025, 1, 1), date(2025, 1, 1), date(2025, 1, 3)], "net_pnl": ["-2", "-3", "1"]})
    rows = metrics(analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, results=results))
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(0.05)
    assert rows["proxy_daily_returns_count"]["value"] == 3


def test_proxy_that_loses_all_capital_does_not_compute_relative_var_on_negative_equity():
    results = pl.DataFrame({"date": [date(2025, 1, 1), date(2025, 1, 2)], "net_pnl": ["-110", "1"]})
    rows = metrics(analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, results=results))
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(1.1)
    assert rows["proxy_normal_var95_1d_money"]["value"] is None


def test_position_shocks_are_partial_scenarios_and_do_not_include_futures_notional():
    report = analyze_portfolio_risk(summary(gross_exposure=Decimal("200"), derivative_positions_count=1))
    rows = metrics(report)
    assert rows["adverse_position_shock_5pct"]["value"] == Decimal("10")
    assert rows["adverse_position_shock_10pct"]["value"] == Decimal("20")
    assert rows["adverse_position_shock_5pct"]["basis"] == "scenario"
    assert "фьючерсов" in rows["adverse_position_shock_5pct"]["quality_reason"]
    assert any("частичный" in value for value in report["quality"])


def test_same_day_period_return_is_available_but_annualization_and_xirr_are_undefined():
    now = START + timedelta(hours=1)
    rows = metrics(analyze_portfolio_risk(summary(captured_at=now.isoformat()), starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START))
    assert rows["capital_period_return"]["value"] == pytest.approx(0.2)
    assert rows["capital_period_xirr"]["value"] is None
    assert rows["capital_period_annualized"]["value"] is None


def test_annualized_dietz_is_explicit_extrapolation_and_guarded_against_overflow():
    rows = metrics(analyze_portfolio_risk(summary(observed_period={"modified_dietz_return": "0.1", "days": "365"})))
    assert rows["annualized_dietz_return"]["value"] == pytest.approx(0.1)
    assert "не прогноз" in rows["annualized_dietz_return"]["quality_reason"]
    huge = metrics(analyze_portfolio_risk(summary(observed_period={"modified_dietz_return": "100", "days": "1"})))
    assert huge["annualized_dietz_return"]["value"] is None


def test_duplicate_flow_ids_are_not_counted_twice_and_conflicts_fail():
    deposit = CashFlow(START + timedelta(days=1), Decimal("50"), "RUB", id="deposit")
    kwargs = dict(starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START)
    rows = metrics(analyze_portfolio_risk(summary(equity=Decimal("170")), [deposit, deposit], **kwargs))
    assert rows["capital_period_profit"]["value"] == 20
    with pytest.raises(ValueError, match="одинаковым id"):
        analyze_portfolio_risk(summary(), [deposit, CashFlow(deposit.timestamp, Decimal("51"), "RUB", id="deposit")], **kwargs)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", None, True])
def test_nonfinite_or_invalid_current_equity_is_rejected(bad):
    with pytest.raises(ValueError):
        analyze_portfolio_risk(summary(equity=bad))


def test_conflicting_nav_or_future_dated_nav_is_rejected():
    history = [
        {"captured_at": NOW.isoformat(), "equity": "100", "currency": "RUB"},
        {"captured_at": NOW.isoformat(), "equity": "101", "currency": "RUB"},
    ]
    with pytest.raises(ValueError, match="противоречивые"):
        analyze_portfolio_risk(summary(valuation_history=history))
    with pytest.raises(ValueError, match="будущего"):
        analyze_portfolio_risk(summary(valuation_history=[{"captured_at": (NOW + timedelta(days=1)).isoformat(), "equity": "100", "currency": "RUB"}]))


def test_invalid_baseline_and_proxy_values_are_rejected():
    with pytest.raises(ValueError, match="раньше"):
        analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=NOW)
    with pytest.raises(ValueError, match="отрицательным"):
        analyze_portfolio_risk(summary(), starting_capital=Decimal("-1"))
    with pytest.raises(ValueError, match="net_pnl"):
        analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, results=pl.DataFrame({"date": [date(2025, 1, 1)], "net_pnl": ["NaN"]}))


def test_all_numeric_metric_values_are_finite_or_explicitly_missing():
    report = analyze_portfolio_risk(summary(), starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START)
    for item in report["metrics"]:
        value = item["value"]
        if isinstance(value, float):
            assert math.isfinite(value)
        if isinstance(value, Decimal):
            assert value.is_finite()


def test_result_risk_has_selected_period_annualization_without_fake_current_equity():
    zone = ZoneInfo("Europe/Moscow")
    start = datetime(2025, 1, 1, tzinfo=zone)
    end = datetime(2026, 1, 1, tzinfo=zone)
    results = pl.DataFrame({
        "date": [date(2024, 12, 31), date(2025, 1, 1), date(2025, 12, 31), date(2026, 1, 1)],
        "net_pnl": ["999", "10", "10", "999"],
    })
    report = analyze_result_risk(results, Decimal("100"), start, end)
    rows = metrics(report)
    assert report["basis"] == "closed_pnl_proxy"
    assert rows["proxy_net_pnl"]["value"] == 20
    assert rows["proxy_terminal_capital"]["value"] == 120
    assert rows["proxy_period_return"]["value"] == pytest.approx(0.2)
    assert rows["proxy_annualized_return"]["value"] == pytest.approx(0.2)
    assert rows["proxy_daily_returns_count"]["value"] == 365
    assert "current_equity" not in rows
    assert "не фактическая" in rows["proxy_terminal_capital"]["method"]


def test_selected_result_proxy_zero_days_stop_at_selected_end_not_today():
    zone = ZoneInfo("Europe/Moscow")
    start = datetime(2025, 1, 1, tzinfo=zone)
    end = datetime(2025, 1, 11, tzinfo=zone)
    results = pl.DataFrame({"date": [date(2025, 1, 1), date(2025, 1, 2)], "net_pnl": ["-10", "5"]})
    actual = summary(result_period_start=start.isoformat(), result_period_end=end.isoformat())
    rows = metrics(analyze_portfolio_risk(actual, starting_capital=Decimal("100"), capital_started_at=start, results=results))
    assert rows["proxy_daily_returns_count"]["value"] == 10
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(0.1)


def test_result_risk_without_starting_capital_keeps_net_pnl_but_not_return_or_var():
    results = pl.DataFrame({"date": [date(2025, 1, 1)], "net_pnl": ["20"]})
    rows = metrics(analyze_result_risk(results, Decimal("0"), START, START + timedelta(days=2)))
    assert rows["proxy_net_pnl"]["value"] == 20
    assert rows["proxy_period_return"]["value"] is None
    assert rows["proxy_annualized_return"]["value"] is None
    assert rows["proxy_normal_var95_1d_money"]["value"] is None


def test_result_proxy_uses_configured_calendar_dates_instead_of_moscow():
    zone = ZoneInfo("Asia/Vladivostok")
    start = datetime(2025, 1, 2, tzinfo=zone)
    end = datetime(2025, 1, 4, tzinfo=zone)
    results = pl.DataFrame({
        "date": [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
        "net_pnl": ["999", "-10", "5", "999"],
    })
    rows = metrics(analyze_result_risk(results, Decimal("100"), start, end))
    assert rows["proxy_net_pnl"]["value"] == -5
    assert rows["proxy_daily_returns_count"]["value"] == 2
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(0.1)


def test_portfolio_result_proxy_preserves_original_calendar_offset():
    zone = ZoneInfo("Asia/Vladivostok")
    start = datetime(2025, 1, 2, tzinfo=zone)
    end = datetime(2025, 1, 4, tzinfo=zone)
    results = pl.DataFrame({"date": [date(2025, 1, 2), date(2025, 1, 3)], "net_pnl": ["-10", "5"]})
    actual = summary(result_period_start=start.isoformat(), result_period_end=end.isoformat())
    rows = metrics(analyze_portfolio_risk(actual, starting_capital=Decimal("100"), capital_started_at=start, results=results))
    assert rows["proxy_daily_returns_count"]["value"] == 2
    assert rows["proxy_max_drawdown"]["value"] == pytest.approx(0.1)
    assert datetime.fromisoformat(rows["proxy_max_drawdown"]["as_of"]) < end
    assert rows["proxy_max_drawdown"]["as_of"] != actual["captured_at"]
    assert "02.01.2025–03.01.2025" in rows["proxy_max_drawdown"]["method"]


def test_capital_baseline_is_after_same_timestamp_flows_and_end_is_inclusive():
    flows = [CashFlow(START, Decimal("100"), "RUB"), CashFlow(NOW, Decimal("50"), "RUB")]
    rows = metrics(analyze_portfolio_risk(summary(equity=Decimal("170")), flows, starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=START))
    assert rows["capital_period_profit"]["value"] == 20
    assert rows["capital_period_return"]["value"] == pytest.approx(0.2)


def test_foreign_funding_before_first_nav_does_not_block_observed_base_currency_risk():
    first = START + timedelta(days=1)
    last = first + timedelta(days=1)
    history = [
        {"captured_at": first.isoformat(), "equity": "100", "currency": "RUB"},
        {"captured_at": last.isoformat(), "equity": "90", "currency": "RUB"},
    ]
    rows = metrics(analyze_portfolio_risk(summary(captured_at=last.isoformat(), equity=Decimal("90"), valuation_history=history), [CashFlow(START, Decimal("1"), "USD")], ledger_complete=True, ledger_start=START))
    assert rows["max_drawdown_observed"]["value"] == pytest.approx(0.1)
    assert rows["daily_returns_count"]["value"] == 1
