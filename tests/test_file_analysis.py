from decimal import Decimal

import pytest

from trade_exporter.exporters.report import export_report
from trade_exporter.services.file_analysis import analyze_local_file, analysis_worker
from test_report_exporter import analysis, portfolio


@pytest.fixture
def source_report(tmp_path):
    return export_report(analysis(), tmp_path, portfolio_report=portfolio())[0]


def test_inline_analysis_uses_complete_nav_without_adding_open_profit_again(source_report):
    result = analyze_local_file(source_report)
    assert result.portfolio_period is True
    assert Decimal(str(result.cards[1][1])) == 10
    assert result.summary["net_pnl"] == -10
    assert Decimal(str(result.cards[0][1])) == 120
    assert result.cards[3][1] == pytest.approx(portfolio().summary["portfolio_period"]["annualized_return"])
    assert next(row for row in result.assets if row[0] == "OPEN@MISX")[3] == 20
    assert result.best[0].opened and result.best[0].result == 20
    assert any(row[2] == "Открыта" for row in result.trades)


def test_reading_file_does_not_modify_it_or_create_new_exports(source_report, monkeypatch):
    from trade_exporter.brokers import registry

    monkeypatch.setattr(registry, "create_broker", lambda *args: pytest.fail("Offline analysis must not connect to a broker"))
    original = {path: path.read_bytes() for path in source_report.parent.iterdir()}
    analyze_local_file(source_report)
    assert set(source_report.parent.iterdir()) == set(original)
    assert all(path.read_bytes() == content for path, content in original.items())


def test_csv_analysis_uses_entered_capital_and_labels_trading_results(tmp_path):
    path = tmp_path / "сделки.csv"
    path.write_text("дата,тикер,цена входа,цена выхода,финрез,комиссия\n01.01.2026,AAA,10,12,20,2\n", encoding="utf-8")
    result = analyze_local_file(path, Decimal(1000))
    assert result.portfolio_period is False
    assert result.cards[0][:2] == ("Результат сделок", Decimal(18))
    assert result.cards[3][1] == Decimal("0.018")
    assert result.daily[0][1] == 18
    assert result.quality and "нет полной оценки портфеля" in result.quality[0]


def test_clean_report_requires_its_json_and_preserves_saved_capital(source_report):
    with pytest.raises(ValueError, match="Стартовый капитал отличается"):
        analyze_local_file(source_report, Decimal(101))
    source_report.with_suffix(".json").unlink()
    with pytest.raises(ValueError, match="JSON"):
        analyze_local_file(source_report)


def test_analysis_worker_returns_errors_and_always_finishes(tmp_path):
    events = []
    analysis_worker(tmp_path / "missing.xlsx", Decimal(0), lambda *event: events.append(event))
    assert any(kind == "error" for kind, _ in events)
    assert not any(kind == "analysis" for kind, _ in events)
    assert events[-1] == ("idle", None)


def test_portfolio_only_does_not_invent_zero_fees_or_trading_results(tmp_path):
    path, _ = export_report(None, tmp_path, portfolio_report=portfolio())
    result = analyze_local_file(path)
    assert result.portfolio_period
    assert result.summary["trading_history_loaded"] is False
    assert next(value for label, value, _ in result.metrics if label == "комиссии сделок") is None
    assert len(result.trades) == 1 and result.trades[0][2] == "Открыта"
