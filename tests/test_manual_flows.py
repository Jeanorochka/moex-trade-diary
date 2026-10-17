from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trade_exporter.services.manual_flows import ManualCashFlow, manual_cash_flow_history, parse_cash_flows
from trade_exporter.services.risk import analyze_portfolio_risk


ZONE = ZoneInfo("Europe/Moscow")
START = datetime(2025, 1, 1, tzinfo=ZONE)
END = datetime(2026, 1, 1, tzinfo=ZONE)


def test_cli_dates_and_signed_amounts_parse_without_rounding():
    entries = parse_cash_flows(["01.01.2025:+1 000,50", "02.01.25:-20.25", "2025-01-03:+0.000000000000000001"])
    assert entries == (
        ManualCashFlow(date(2025, 1, 1), Decimal("1000.50")),
        ManualCashFlow(date(2025, 1, 2), Decimal("-20.25")),
        ManualCashFlow(date(2025, 1, 3), Decimal("1e-18")),
    )


@pytest.mark.parametrize("raw", [
    "01.01.2025:0", "01.01.2025:-0.00", "01.01.2025:NaN", "01.01.2025:Infinity",
    "01.01.2025:1e20", "01.01.2025:-1e20", "01.01.2025:0.0000000000000000001",
    "01.01.2025:1.0000000000000000000", "31.02.2025:+1", "01.01.2025", "01.01.2025:+1:+2", True,
])
def test_invalid_cli_movements_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_cash_flows([raw])


@pytest.mark.parametrize("raw", [True, Decimal("0"), Decimal("NaN"), Decimal("Infinity"), Decimal("1e-19")])
def test_typed_entry_does_not_bypass_financial_validation(raw):
    with pytest.raises(ValueError):
        ManualCashFlow(date(2025, 1, 1), raw)


def test_same_day_baseline_entries_are_after_opening_value_and_counted():
    history = manual_cash_flow_history(parse_cash_flows(["01.01.2025:+100"]), START, END, "RUB")
    flow = history.flows[0]
    assert flow.timestamp == datetime.combine(START.date(), time.max, ZONE).astimezone(timezone.utc)
    assert flow.timestamp > START
    report = analyze_portfolio_risk({"captured_at": END.isoformat(), "equity": Decimal("230"), "base_currency": "RUB"}, history.flows, starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=history.complete, ledger_start=history.started_at)
    metrics = {row["metric"]: row["value"] for row in report["metrics"]}
    assert metrics["capital_period_profit"] == 30
    expected_weight = Decimal(str((END - flow.timestamp).total_seconds())) / Decimal(str((END - START).total_seconds()))
    assert metrics["capital_period_return"] == pytest.approx(float(Decimal(30) / (100 + 100 * expected_weight)))


def test_identical_entries_and_same_day_withdrawals_remain_distinct():
    history = manual_cash_flow_history(parse_cash_flows(["01.06.2025:-10", "01.06.2025:-10", "01.06.2025:+25"]), START, END, "RUB")
    assert [flow.amount for flow in history.flows] == [Decimal(-10), Decimal(-10), Decimal(25)]
    assert len({flow.id for flow in history.flows}) == 3
    assert len({flow.timestamp for flow in history.flows}) == 1
    assert sum((flow.amount for flow in history.flows), Decimal(0)) == 5


def test_current_calendar_day_is_clamped_to_valuation_time_and_its_withdrawal_is_included():
    captured = datetime(2025, 1, 2, 12, 30, tzinfo=ZONE)
    history = manual_cash_flow_history(parse_cash_flows(["02.01.2025:-40"]), START, captured.astimezone(timezone.utc), "RUB")
    assert history.flows[0].timestamp == captured.astimezone(timezone.utc)
    assert any("момент оценки" in reason for reason in history.reasons)
    report = analyze_portfolio_risk({"captured_at": captured.isoformat(), "equity": Decimal("80"), "base_currency": "RUB"}, history.flows, starting_capital=Decimal("100"), capital_started_at=START, ledger_complete=True, ledger_start=history.started_at)
    metrics = {row["metric"]: row["value"] for row in report["metrics"]}
    assert metrics["capital_period_profit"] == 20
    assert metrics["capital_period_return"] == pytest.approx(0.2)


def test_end_calendar_is_evaluated_in_starting_timezone_instead_of_utc():
    start = datetime(2025, 1, 1, tzinfo=ZoneInfo("Asia/Vladivostok"))
    captured = datetime(2025, 1, 1, 15, tzinfo=timezone.utc)
    history = manual_cash_flow_history(parse_cash_flows(["02.01.2025:+10"]), start, captured, "RUB")
    assert history.flows[0].timestamp == captured


@pytest.mark.parametrize("day", [date(2024, 12, 31), date(2026, 1, 2)])
def test_before_baseline_and_future_days_are_rejected(day):
    with pytest.raises(ValueError, match="вне выбранного периода"):
        manual_cash_flow_history((ManualCashFlow(day, Decimal(1)),), START, END, "RUB")


def test_empty_manual_journal_is_a_complete_user_assertion_not_a_broker_history():
    assert parse_cash_flows(None) == ()
    history = manual_cash_flow_history((), START, END, "RUB")
    assert history.complete is True
    assert history.flows == ()
    assert history.started_at == START.astimezone(timezone.utc)
    assert history.reasons == ("Ручной журнал: пользователь указал все внешние движения; даты учитываются на конец дня",)


def test_movements_are_stably_sorted_by_day_without_losing_original_identity():
    history = manual_cash_flow_history(parse_cash_flows(["03.01.2025:+3", "01.01.2025:-1", "01.01.2025:-2"]), START, END, "rub")
    assert [flow.amount for flow in history.flows] == [Decimal(-1), Decimal(-2), Decimal(3)]
    assert [flow.id for flow in history.flows] == ["manual:00000002", "manual:00000003", "manual:00000001"]
    assert all(flow.currency == "RUB" and flow.kind == "external" for flow in history.flows)


@pytest.mark.parametrize("start,end,currency", [
    (START.replace(tzinfo=None), END, "RUB"),
    (START, END.replace(tzinfo=None), "RUB"),
    (START, START, "RUB"),
    (END, START, "RUB"),
    (START, END, "рубли"),
])
def test_invalid_coverage_or_currency_is_rejected(start, end, currency):
    with pytest.raises(ValueError):
        manual_cash_flow_history((), start, end, currency)


def test_intraday_baseline_cannot_include_an_event_at_the_same_timestamp():
    baseline = datetime.combine(date(2025, 1, 1), time.max, ZONE)
    with pytest.raises(ValueError, match="позже начальной оценки"):
        manual_cash_flow_history(parse_cash_flows(["01.01.2025:+1"]), baseline, baseline + timedelta(days=1), "RUB")
