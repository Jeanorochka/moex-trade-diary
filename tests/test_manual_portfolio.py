from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import orjson
import polars as pl
import pytest

from trade_exporter import cli
from trade_exporter.domain.models import Account
from trade_exporter.domain.portfolio import CashFlow, PortfolioSnapshot
from trade_exporter.services import portfolio_service as service
from trade_exporter.services.analytics import analyze_file, analyze_results
from trade_exporter.services.manual_flows import parse_cash_flows
from trade_exporter.services.performance import attach_portfolio_performance
from trade_exporter.services.risk import analyze_result_risk
from trade_exporter.exporters.analytics import export_analysis


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=10)


def report(tmp_path, monkeypatch, entries, equity="230", *, complete=True, mode="manual"):
    broker = SimpleNamespace(name="finam", settings={})
    account = Account("1", "test", "finam", opened_at=START - timedelta(days=100))
    snapshot = PortfolioSnapshot(account.id, broker.name, END, "RUB", Decimal(equity), source="broker NAV")
    broker_flows = (
        CashFlow(START + timedelta(days=5), Decimal(100), "RUB", "external", "deposit"),
        CashFlow(START + timedelta(days=3), Decimal("-2"), "RUB", "fee", "fee"),
    )
    calls = []

    def load(broker, account, start, end, *, require_inception=True):
        calls.append(require_inception)
        return service.CashFlowHistory(broker_flows, complete, () if complete else ("Переводы бумаг до начала периода не оценены.",), START)

    monkeypatch.setattr(service, "load_portfolio", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(service, "load_cash_flows", load)
    result = service.prepare_portfolio_report(
        broker, account, tmp_path, starting_capital=Decimal(100), capital_started_at=START,
        cash_flow_mode=mode, manual_cash_flows=parse_cash_flows(entries),
    )
    return result, calls


def test_manual_deposit_replaces_broker_deposit_without_double_counting(tmp_path, monkeypatch):
    result, calls = report(tmp_path, monkeypatch, ["05.01.2026:+100"])
    period = result.summary["portfolio_period"]
    assert period["cash_flow_mode"] == "manual"
    assert period["external_inflows"] == 100
    assert period["net_external_flows"] == 100
    assert period["total_profit"] == 30
    assert period["period_return"] == pytest.approx(0.2)
    assert result.summary["equity"] == 230
    assert result.period_flows.filter(pl.col("kind") == "external").height == 1
    assert result.period_flows.filter(pl.col("kind") == "fee").height == 1
    assert calls == [True]


def test_manual_withdrawal_restores_withdrawn_capital_to_profit(tmp_path, monkeypatch):
    result, _ = report(tmp_path, monkeypatch, ["05.01.2026:-20"], equity="110")
    period = result.summary["portfolio_period"]
    assert period["external_outflows"] == 20
    assert period["net_external_flows"] == -20
    assert period["total_profit"] == 30
    assert period["period_return"] == pytest.approx(1 / 3)


def test_explicit_empty_manual_journal_means_no_external_flows(tmp_path, monkeypatch):
    result, _ = report(tmp_path, monkeypatch, [], equity="120")
    period = result.summary["portfolio_period"]
    assert period["status"] == "complete"
    assert period["net_external_flows"] == 0
    assert period["total_profit"] == 20
    assert period["period_return"] == 0.2
    assert period["external_flows"] == []
    assert any("ручн" in reason.lower() for reason in period["quality"])


def test_manual_period_does_not_claim_lifetime_history_is_complete(tmp_path, monkeypatch):
    result, calls = report(tmp_path, monkeypatch, ["05.01.2026:+100"], complete=False)
    assert result.summary["ledger_complete"] is False
    assert result.summary["lifetime_profit"] is None
    assert result.summary["period_ledger_complete"] is True
    assert result.summary["portfolio_period"]["total_profit"] == 30
    assert calls == [True]


def test_auto_mode_keeps_broker_funding(tmp_path, monkeypatch):
    result, _ = report(tmp_path, monkeypatch, [], mode="auto")
    period = result.summary["portfolio_period"]
    assert period["cash_flow_mode"] == "auto"
    assert period["external_inflows"] == 100
    assert period["total_profit"] == 30


def test_manual_journal_survives_excel_json_and_offline_analysis(tmp_path, monkeypatch):
    portfolio, _ = report(tmp_path, monkeypatch, ["05.01.2026:+100"])
    paths = service.save_portfolio_report(portfolio, tmp_path)
    data = orjson.loads(paths[1].read_bytes())
    assert data["summary"]["portfolio_period"]["cash_flow_mode"] == "manual"
    assert data["period_flows"]
    first = pl.read_excel(paths[0], sheet_name="Портфель за период")
    assert any("ручн" in str(value).lower() for value in first["основание"])
    trading = analyze_results(pl.DataFrame({
        "date": [START.date()], "ticker": ["STOCK"], "gross_pnl": ["-1"],
        "commission": ["0"], "event_type": ["stock_fifo"],
    }), Decimal(100))
    trading.summary["risk"] = analyze_result_risk(trading.daily, Decimal(100), START, END)
    attach_portfolio_performance(trading, portfolio)
    workbook, _ = export_analysis(trading, tmp_path)
    offline = analyze_file(workbook)
    assert offline.summary["portfolio_period"]["cash_flow_mode"] == "manual"
    assert offline.summary["headline_profit"] == 30
    assert offline.summary["net_pnl"] == -1


def test_cli_parses_signed_cash_flows_with_explicit_manual_mode():
    args = cli.parse_args([
        "--starting-capital", "100", "--cash-flow-mode", "manual",
        "--cash-flow", "05.01.2026:+100", "--cash-flow", "06.01.2026:-20",
    ])
    assert [row.amount for row in args.manual_cash_flows] == [Decimal(100), Decimal(-20)]


@pytest.mark.parametrize("argv", [
    ["--cash-flow", "05.01.2026:+100"],
    ["--cash-flow-mode", "manual"],
    ["--starting-capital", "100", "--cash-flow-mode", "manual", "--cash-flow", "bad"],
    ["--analyze", "source.xlsx", "--starting-capital", "100", "--cash-flow-mode", "manual"],
])
def test_cli_rejects_ambiguous_or_invalid_manual_overrides(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)
