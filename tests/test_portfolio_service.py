from datetime import datetime, timedelta, timezone
from decimal import Decimal

import orjson
import polars as pl
import pytest

from trade_exporter.domain.models import Account
from trade_exporter.domain.portfolio import CashBalance, CashFlow, PortfolioSnapshot
from trade_exporter.services import portfolio_service as service


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def snapshot(at=START, equity="100", account_id="1", currency="RUB"):
    return PortfolioSnapshot(account_id, "tbank", at, currency, Decimal(equity), (CashBalance(currency, Decimal(equity)),), source="broker")


def records(opening="100", closing="120", days=10):
    return [
        {"captured_at": START.isoformat(), "equity": Decimal(opening), "currency": "RUB"},
        {"captured_at": (START + timedelta(days=days)).isoformat(), "equity": Decimal(closing), "currency": "RUB"},
    ]


def test_deposit_is_not_a_profit_and_weight_uses_exact_flow_time():
    flows = (CashFlow(START + timedelta(days=5), Decimal(100), "RUB"),)
    period, reasons = service.observed_performance(records("100", "230"), flows, ledger_complete=True)
    assert period["net_external_flows"] == 100
    assert period["profit"] == 30
    assert period["weighted_capital"] == 150
    assert period["modified_dietz_return"] == Decimal("0.2")
    assert reasons == []


def test_period_excludes_opening_flow_and_includes_closing_flow():
    flows = (
        CashFlow(START, Decimal(100), "RUB"),
        CashFlow(START + timedelta(days=10), Decimal(-20), "RUB"),
        CashFlow(START + timedelta(days=5), Decimal(5), "RUB", kind="income"),
    )
    period, _ = service.observed_performance(records("100", "110"), flows, ledger_complete=True)
    assert period["net_external_flows"] == -20
    assert period["profit"] == 30
    assert period["modified_dietz_return"] == Decimal("0.3")


def test_observed_period_uses_one_copy_of_each_broker_flow():
    deposit = CashFlow(START + timedelta(days=5), Decimal(100), "RUB", id="one")
    period, _ = service.observed_performance(records("100", "230"), (deposit, deposit), ledger_complete=True)
    assert period["net_external_flows"] == 100
    assert period["profit"] == 30


def test_observed_period_requires_coverage_from_opening_observation():
    period, reasons = service.observed_performance(records(), (), ledger_complete=True, ledger_start=START + timedelta(days=1))
    assert period is None and reasons


def test_observed_period_rejects_future_and_conflicting_flows():
    future = CashFlow(START + timedelta(days=11), Decimal(100), "RUB")
    with pytest.raises(ValueError):
        service.observed_performance(records(), (future,), ledger_complete=True)
    first = CashFlow(START + timedelta(days=5), Decimal(100), "RUB", id="one")
    conflict = CashFlow(first.timestamp, Decimal(200), "RUB", id="one")
    with pytest.raises(ValueError):
        service.observed_performance(records(), (first, conflict), ledger_complete=True)


def test_withdrawal_to_zero_leaves_return_undefined_when_capital_is_nonpositive():
    flows = (CashFlow(START + timedelta(days=1), Decimal(-200), "RUB"),)
    period, reasons = service.observed_performance(records("100", "0"), flows, ledger_complete=True)
    assert period["profit"] == 100
    assert period["modified_dietz_return"] is None
    assert reasons


def test_unknown_ledger_or_currency_suppresses_period_return():
    assert service.observed_performance(records(), (), ledger_complete=False)[0] is None
    flows = (CashFlow(START + timedelta(days=1), Decimal(100), "USD"),)
    assert service.observed_performance(records(), flows, ledger_complete=True)[0] is None
    assert service.observed_performance(records()[:1], (), ledger_complete=True)[0] is None


def test_snapshots_round_trip_preserves_decimal_and_partitions_account_currency(tmp_path):
    first = service.record_snapshot(snapshot(equity="100.123456789123456789"), tmp_path)
    second = service.record_snapshot(snapshot(START + timedelta(days=1), "120.123456789123456789"), tmp_path)
    assert second[0] == first[0]
    assert second[1]["equity"] == Decimal("120.123456789123456789")
    assert service.record_snapshot(snapshot(START + timedelta(days=1), "120.123456789123456789"), tmp_path) == second
    service.record_snapshot(snapshot(account_id="2"), tmp_path)
    service.record_snapshot(snapshot(currency="USD"), tmp_path)
    assert len(list((tmp_path / "portfolio_history").glob("*.json"))) == 3


@pytest.mark.parametrize("mutation", ["invalid_json", "future", "different_equity", "invalid_currency", "unordered", "wrong_account"])
def test_bad_history_does_not_get_overwritten(tmp_path, mutation):
    service.record_snapshot(snapshot(), tmp_path)
    path = next((tmp_path / "portfolio_history").glob("*.json"))
    payload = orjson.loads(path.read_bytes())
    at, equity = START + timedelta(days=1), "120"
    if mutation == "invalid_json":
        path.write_bytes(b"{broken")
    elif mutation == "future":
        payload["snapshots"][0]["captured_at"] = (START + timedelta(days=10)).isoformat()
    elif mutation == "different_equity":
        at = START
    elif mutation == "invalid_currency":
        payload["snapshots"][0]["currency"] = "USD"
    elif mutation == "unordered":
        payload["snapshots"].append(payload["snapshots"][0])
    elif mutation == "wrong_account":
        payload["account_id"] = "999"
    if mutation != "invalid_json":
        path.write_bytes(orjson.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        service.record_snapshot(snapshot(at, equity), tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_snapshot_pipeline_keeps_current_report_when_flow_history_is_unavailable(tmp_path, monkeypatch):
    account = Account("1", "test", "tbank", opened_at=START)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot(START + timedelta(days=1), "120"))

    def unavailable(*args, **kwargs):
        raise RuntimeError("T-Bank API: unavailable")

    monkeypatch.setattr(service, "load_cash_flows", unavailable)
    report, paths = service.execute_portfolio(object(), account, tmp_path)
    assert all(path.exists() for path in paths)
    assert report.summary["equity"] == 120
    assert report.summary["lifetime_profit"] is None
    assert report.summary["cash_flow_error"] == "T-Bank API: unavailable"
    assert report.summary["observed_period"] is None


def test_unknown_opening_does_not_prevent_current_valuation(tmp_path, monkeypatch):
    account = Account("1", "test", "tbank")
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot())
    monkeypatch.setattr(service, "load_cash_flows", lambda *a, **k: pytest.fail("unknown inception requested full ledger"))
    report, paths = service.execute_portfolio(object(), account, tmp_path)
    assert report.summary["equity"] == 100
    assert report.summary["lifetime_profit"] is None
    assert report.summary["cash_flow_quality"]
    assert all(path.exists() for path in paths)


def test_snapshot_pipeline_excludes_deposit_between_observed_values(tmp_path, monkeypatch):
    account = Account("1", "test", "tbank", opened_at=START - timedelta(days=1))
    flows = (CashFlow(START - timedelta(hours=1), Decimal(100), "RUB"), CashFlow(START + timedelta(days=5), Decimal(100), "RUB"))
    monkeypatch.setattr(service, "load_cash_flows", lambda *a, **k: service.CashFlowHistory(tuple(flow for flow in flows if flow.timestamp < a[3]), True, (), account.opened_at))
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot())
    service.execute_portfolio(object(), account, tmp_path)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot(START + timedelta(days=10), "230"))
    report, paths = service.execute_portfolio(object(), account, tmp_path)
    payload = orjson.loads(paths[1].read_bytes())
    assert report.summary["observed_period"]["profit"] == 30
    assert Decimal(payload["summary"]["observed_period"]["modified_dietz_return"]) == Decimal("0.2")


def test_observed_returns_work_even_when_broker_hides_original_funding(tmp_path, monkeypatch):
    account = Account("1", "test", "finam", opened_at=START - timedelta(days=365))
    requests = []

    def cash_flows(broker, account, start, end, *, require_inception=True):
        requests.append(require_inception)
        if require_inception:
            return service.CashFlowHistory((), False, ("Первоначальное пополнение не выдано API.",), start)
        return service.CashFlowHistory((CashFlow(START + timedelta(days=5), Decimal(100), "RUB"),), True, (), start)

    monkeypatch.setattr(service, "load_cash_flows", cash_flows)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot())
    service.execute_portfolio(object(), account, tmp_path)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot(START + timedelta(days=10), "230"))
    report, _ = service.execute_portfolio(object(), account, tmp_path)
    assert report.summary["lifetime_profit"] is None
    assert report.summary["observed_period"]["profit"] == 30
    assert report.summary["observed_period"]["modified_dietz_return"] == Decimal("0.2")
    assert requests == [True, True, False]


def test_process_lock_is_released_after_failed_history_validation(tmp_path):
    service.record_snapshot(snapshot(), tmp_path)
    with pytest.raises(ValueError):
        service.record_snapshot(snapshot(START, "200"), tmp_path)
    records = service.record_snapshot(snapshot(START + timedelta(days=1), "120"), tmp_path)
    assert len(records) == 2


def test_entered_capital_gives_annualized_return_in_the_first_export(tmp_path, monkeypatch):
    account = Account("1", "test", "tbank", opened_at=START)
    end = START + timedelta(days=365)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot(end, "1200"))
    monkeypatch.setattr(service, "load_cash_flows", lambda *a, **k: service.CashFlowHistory((), True, (), START))
    report, paths = service.execute_portfolio(
        object(), account, tmp_path, starting_capital=Decimal("1000"), capital_started_at=START,
    )
    metrics = {row["metric"]: row for row in report.summary["risk"]["metrics"]}
    assert report.summary["observed_period"] is None
    assert metrics["capital_period_profit"]["value"] == 200
    assert metrics["capital_period_return"]["value"] == pytest.approx(0.2)
    assert metrics["capital_period_annualized"]["value"] == pytest.approx(0.2)
    assert "Риск и доходность" in pl.read_excel(paths[0], sheet_id=0)


def test_entered_capital_uses_period_flows_without_initial_funding_history(tmp_path, monkeypatch):
    account = Account("1", "test", "finam", opened_at=START - timedelta(days=365))
    end = START + timedelta(days=10)
    monkeypatch.setattr(service, "load_portfolio", lambda *a, **k: snapshot(end, "230"))

    def cash_flows(broker, account, start, stop, *, require_inception=True):
        if require_inception:
            return service.CashFlowHistory((), False, ("Начальное пополнение недоступно.",), account.opened_at)
        return service.CashFlowHistory((CashFlow(START + timedelta(days=5), Decimal(100), "RUB"),), True, (), START)

    monkeypatch.setattr(service, "load_cash_flows", cash_flows)
    report, _ = service.execute_portfolio(object(), account, tmp_path, starting_capital=Decimal(100), capital_started_at=START)
    metrics = {row["metric"]: row for row in report.summary["risk"]["metrics"]}
    assert report.summary["lifetime_profit"] is None
    assert metrics["capital_period_profit"]["value"] == 30
    assert metrics["capital_period_return"]["value"] == pytest.approx(0.2)
