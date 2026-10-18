from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import orjson
import polars as pl
import pytest

from trade_exporter.domain.models import Account
from trade_exporter.domain.portfolio import CashFlow, PortfolioSnapshot
from trade_exporter.exporters.analytics import export_analysis
from trade_exporter.services.analytics import analyze_file, analyze_results
from trade_exporter.services.performance import attach_portfolio_performance, validate_portfolio_period
from trade_exporter.services import portfolio_service as service
from trade_exporter.services.risk import analyze_result_risk


UTC = timezone.utc
START = datetime(2025, 9, 25, tzinfo=UTC)
END = START + timedelta(days=365)


def prepare(tmp_path, monkeypatch, capital="60", equity="80", flows=(), *, complete=True, requested_end=None):
    account = Account("synthetic-account", "test", "finam", opened_at=START - timedelta(days=30))
    broker = SimpleNamespace(name="finam", settings={})
    snapshot = PortfolioSnapshot(account.id, "finam", END, "RUB", Decimal(equity), source="broker NAV")
    monkeypatch.setattr(service, "load_portfolio", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(service, "load_cash_flows", lambda *args, **kwargs: service.CashFlowHistory(tuple(flows), complete, () if complete else ("Перевод бумаг не оценён.",), START))
    return service.prepare_portfolio_report(
        broker, account, tmp_path, starting_capital=Decimal(capital), capital_started_at=START,
        result_period_start=START, result_period_end=requested_end,
    )


def trading_report(pnl="-20"):
    report = analyze_results(pl.DataFrame({
        "date": [END.date() - timedelta(days=1)], "ticker": ["INHERITED"],
        "gross_pnl": [pnl], "commission": ["0"], "event_type": ["stock_fifo"],
    }), Decimal(60))
    report.summary["risk"] = analyze_result_risk(report.daily, Decimal(60), START, END)
    return report


def test_inherited_loss_is_not_misattributed_to_new_management_period(tmp_path, monkeypatch):
    portfolio = prepare(tmp_path, monkeypatch)
    portfolio.summary["unrealized_pnl"] = Decimal("999")
    report = attach_portfolio_performance(trading_report(), portfolio)
    assert report.summary["net_pnl"] == -20
    assert report.summary["headline_profit"] == 20
    assert report.summary["portfolio_period"]["ending_equity"] == 80
    assert report.summary["headline_return"] == pytest.approx(1 / 3)
    assert next(row for row in report.summary["risk"]["metrics"] if row["metric"] == "capital_period_profit")["value"] == 20
    assert report.summary["risk"]["metrics"][0]["metric"].startswith("proxy_") is False


def test_unsold_asset_revaluation_and_income_are_already_in_nav(tmp_path, monkeypatch):
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="125", flows=(
        CashFlow(START + timedelta(days=100), Decimal("3"), "RUB", "income", "dividend"),
        CashFlow(START + timedelta(days=101), Decimal("-1"), "RUB", "fee", "fee"),
    ))
    report = attach_portfolio_performance(trading_report("0"), portfolio)
    assert report.summary["headline_profit"] == 25
    assert report.summary["portfolio_period"]["net_external_flows"] == 0
    assert report.summary["headline_return"] == 0.25


def test_deposit_is_removed_and_period_ledger_is_exported(tmp_path, monkeypatch):
    deposit = CashFlow(START + timedelta(days=182, hours=12), Decimal(100), "RUB", "external", "deposit")
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="230", flows=(deposit,))
    period = portfolio.summary["portfolio_period"]
    assert period["total_profit"] == 30
    assert period["period_return"] == 0.2
    assert period["external_inflows"] == 100
    assert portfolio.period_flows["id"].to_list() == ["deposit"]
    paths = service.save_portfolio_report(portfolio, tmp_path)
    payload = orjson.loads(paths[1].read_bytes())
    assert payload["portfolio"]["period_flows"][0]["amount"] == "100.000000000000000000"
    assert list(pl.read_excel(paths[0], sheet_id=0)) == ["Отчёт"]


def test_missing_external_transfer_value_does_not_become_zero_funding(tmp_path, monkeypatch):
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="230", complete=False)
    report = attach_portfolio_performance(trading_report(), portfolio)
    assert report.summary["portfolio_period"]["status"] == "unavailable"
    assert report.summary["headline_profit"] is None
    assert report.summary["portfolio_period"]["external_inflows"] is None


def test_past_period_uses_historical_close_instead_of_current_nav(tmp_path, monkeypatch):
    from trade_exporter.services import historical_valuation

    requested_end = START + timedelta(days=10, hours=-3)
    monkeypatch.setattr(historical_valuation, "load_historical_valuation", lambda *args: SimpleNamespace(
        equity=Decimal(110), currency="RUB", source_file=tmp_path / "report.html", quality=(),
    ))
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="200", requested_end=requested_end)
    assert portfolio.summary["equity"] == 200
    assert portfolio.summary["portfolio_period"]["ending_equity"] == 110
    assert portfolio.summary["portfolio_period"]["total_profit"] == 10
    assert portfolio.summary["portfolio_period"]["valuation_basis"] == "historical_broker_report"


def test_missing_past_nav_is_unavailable_without_fabricated_loss(tmp_path, monkeypatch):
    from trade_exporter.services import historical_valuation

    def missing(*args):
        raise RuntimeError("Отчёт недоступен")

    monkeypatch.setattr(historical_valuation, "load_historical_valuation", missing)
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="200", requested_end=START + timedelta(days=10, hours=-3))
    assert portfolio.summary["portfolio_period"]["status"] == "unavailable"
    assert portfolio.summary["portfolio_period"]["total_profit"] is None
    assert portfolio.summary["portfolio_period"]["ending_equity"] is None


def test_offline_analysis_preserves_nav_headline_separately_from_fifo(tmp_path, monkeypatch):
    portfolio = prepare(tmp_path, monkeypatch)
    report = attach_portfolio_performance(trading_report(), portfolio)
    source, _ = export_analysis(report, tmp_path)
    offline = analyze_file(source)
    assert offline.summary["net_pnl"] == -20
    assert offline.summary["headline_profit"] == 20
    assert offline.summary["headline_return"] == pytest.approx(1 / 3)
    with pytest.raises(ValueError, match="Стартовый капитал отличается"):
        analyze_file(source, Decimal(500))


def test_long_risk_metadata_roundtrips_without_excel_cell_truncation(tmp_path, monkeypatch):
    portfolio = prepare(tmp_path, monkeypatch)
    portfolio.summary["risk"]["quality"] = ["Пояснение " * 6000]
    report = attach_portfolio_performance(trading_report(), portfolio)
    source, _ = export_analysis(report, tmp_path)
    parameters = pl.read_excel(source, sheet_name="Параметры расчёта")
    assert any(str(key).startswith("risk__part_") for key in parameters["ключ"])
    offline = analyze_file(source)
    assert offline.summary["headline_profit"] == 20
    assert offline.summary["risk"]["quality"] == report.summary["risk"]["quality"]


def test_mismatched_total_profit_metadata_is_rejected(tmp_path, monkeypatch):
    period = prepare(tmp_path, monkeypatch).summary["portfolio_period"]
    with pytest.raises(ValueError, match="Прибыль портфеля не сходится"):
        validate_portfolio_period({**period, "total_profit": Decimal(999)})


def test_historical_eod_cannot_be_used_for_an_earlier_intraday_cutoff(tmp_path, monkeypatch):
    from trade_exporter.services import historical_valuation

    monkeypatch.setattr(historical_valuation, "load_historical_valuation", lambda *args: pytest.fail("incompatible cutoff requested a later historical NAV"))
    portfolio = prepare(tmp_path, monkeypatch, capital="100", equity="200", requested_end=START + timedelta(days=10))
    assert portfolio.summary["portfolio_period"]["status"] == "unavailable"
    assert "по Москве" in " ".join(portfolio.summary["portfolio_period"]["quality"])
