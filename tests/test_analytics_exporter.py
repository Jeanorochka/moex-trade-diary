from datetime import datetime, timezone
from decimal import Decimal
from xml.etree import ElementTree
from zipfile import ZipFile

import orjson
import polars as pl

from trade_exporter.domain.models import ClosedTrade
from trade_exporter.exporters.analytics import export_analysis
from trade_exporter.services.analytics import analyze_file, analyze_trades


def make_report(values=None):
    trades = [
        ClosedTrade(
            exit_at=datetime(2026, 9, day, 12, tzinfo=timezone.utc),
            instrument_id=ticker,
            ticker=ticker,
            direction="LONG",
            quantity=1,
            entry_price=Decimal("100"),
            exit_price=Decimal("100") + Decimal(gross),
            gross_pnl=Decimal(gross),
            commission=Decimal("1"),
        )
        for day, ticker, gross in ([(11, "AAA", "-10"), (12, "BBB", "20")] if values is None else values)
    ]
    return analyze_trades(trades, timezone.utc)


def test_report_contains_readable_sheets_and_native_charts(tmp_path):
    report = make_report()
    workbook, json_path = export_analysis(report, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0, engine="calamine")
    assert list(sheets) == ["Аналитика", "Сделки", "Дни", "Месяцы", "Тикеры"] + (["Риск и доходность"] if report.summary.get("risk") is not None else [])
    assert sheets["Дни"]["финрез net"].to_list() == [-11, 19]
    assert sheets["Дни"]["просадка"].to_list() == [11, 0]
    assert sheets["Тикеры"]["финрез net"].sum() == 8
    assert sheets["Дни"]["дата"].to_list() == ["11.09.2026", "12.09.2026"]
    assert sheets["Месяцы"]["дата"].to_list() == ["09.2026"]
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["net_pnl"]) == 8
    assert Decimal(payload["summary"]["max_drawdown"]) == 11
    with ZipFile(workbook) as archive:
        charts = [name for name in archive.namelist() if name.startswith("xl/charts/chart") and name.endswith(".xml")]
        assert len(charts) == 4
        namespace = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
        formulas = [
            ref.text
            for name in charts
            for ref in ElementTree.fromstring(archive.read(name)).findall(".//c:f", namespace)
        ]
        assert any("Дни" in formula and "$F$2:$F$3" in formula for formula in formulas)
        assert any("Тикеры" in formula for formula in formulas)
        chart_values = [
            point.text
            for name in charts
            for point in ElementTree.fromstring(archive.read(name)).findall(".//c:strCache/c:pt/c:v", namespace)
        ]
        assert "11.09.2026" in chart_values
        assert "09.2026" in chart_values


def test_empty_report_has_no_invalid_chart_ranges(tmp_path):
    report = analyze_trades([], timezone.utc)
    workbook, json_path = export_analysis(report, tmp_path)
    assert orjson.loads(json_path.read_bytes())["summary"]["trades"] == 0
    with ZipFile(workbook) as archive:
        assert not any(name.startswith("xl/charts/chart") for name in archive.namelist())


def test_reports_do_not_overwrite_previous_results(tmp_path):
    first = export_analysis(make_report(), tmp_path)
    second = export_analysis(make_report(), tmp_path)
    assert set(first).isdisjoint(second)
    assert all(path.exists() for path in (*first, *second))


def test_trade_risk_sheet_marks_closed_pnl_as_proxy_and_keeps_numbers(tmp_path):
    report = make_report()
    report.summary["risk"] = {
        "basis": "closed_pnl_proxy", "as_of": "2026-09-12",
        "metrics": [
            {"metric": "proxy_max_drawdown", "label": "Просадка накопленного результата закрытых строк", "value": Decimal("11"), "unit": "RUB", "method": "Накопленный net P&L закрытых строк", "observations": 2, "basis": "closed_pnl_proxy", "quality_reason": "Без переоценки открытых позиций."},
            {"metric": "proxy_var95_1d_money", "label": "VaR 95% по дневным результатам строк", "value": None, "unit": "RUB", "method": "Исторический расчет по закрытым результатам", "observations": 2, "basis": "closed_pnl_proxy", "quality_reason": "Недостаточно дневных результатов; не риск всего портфеля."},
        ], "quality": [],
    }
    workbook, json_path = export_analysis(report, tmp_path)
    risk = pl.read_excel(workbook, sheet_name="Риск и доходность")
    assert risk["значение"].to_list() == [11, None]
    assert risk.schema["значение"].is_numeric()
    assert all("без полной переоценки портфеля" in basis for basis in risk["основание"])
    assert risk["качество данных"][0] == "Без переоценки открытых позиций."
    summary = pl.read_excel(workbook, sheet_name="Аналитика")
    assert "risk" not in summary["показатель"].to_list()
    assert "Просадка накопленного результата по дням" in summary["показатель"].to_list()
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["risk"]["metrics"][0]["value"]) == 11
    assert payload["summary"]["risk"]["metrics"][1]["value"] is None


def test_partial_commission_metadata_is_readable_and_does_not_become_a_numeric_metric(tmp_path):
    report = make_report()
    report.summary.update({
        "commission_complete": False,
        "commission_basis": "transactions_period",
        "commission_quality": ["История денежных расходов за период не подтверждена."],
        "net_pnl_basis": "gross_less_known_fees",
    })
    workbook, json_path = export_analysis(report, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0)
    labels = sheets["Аналитика"]["показатель"].to_list()
    assert "Результат после известных комиссий; расходы неполны" in labels
    assert "commission_complete" not in labels
    assert "commission_basis" not in labels
    assert "финрез после известных комиссий" in sheets["Дни"].columns
    basis = sheets["Основа расчёта"]["основа расчёта"].to_list()
    assert any("по отдельным сделкам не распределены" in value for value in basis)
    assert "История денежных расходов за период не подтверждена." in basis
    payload = orjson.loads(json_path.read_bytes())
    assert payload["summary"]["commission_complete"] is False
    assert payload["summary"]["commission_basis"] == "transactions_period"
    offline = analyze_file(workbook)
    assert offline.summary["commission_complete"] is False
    assert offline.summary["commission_basis"] == "transactions_period"
    assert offline.summary["net_pnl"] == report.summary["net_pnl"]


def test_positive_nav_period_precedes_negative_realized_analytics(tmp_path):
    report = make_report([(11, "AAA", "-100"), (12, "BBB", "10")])
    period = {
        "status": "complete", "base_currency": "RUB", "started_at": "2025-09-25T00:00:00+03:00",
        "ended_at": "2026-10-17T12:00:00+03:00", "requested_end": "2026-10-18T00:00:00+03:00",
        "starting_equity": Decimal("130135.23"), "ending_equity": Decimal("134369.45"),
        "external_inflows": Decimal("1000"), "external_outflows": Decimal("0"),
        "net_external_flows": Decimal("1000"), "total_profit": Decimal("3234.22"),
        "period_return": 0.02479256288698292, "annualized_return": 0.023335293575455992,
        "xirr": 0.023335765979261878, "days": 387.5, "valuation_basis": "current_broker_nav",
        "quality": [], "source": "Историческая начальная оценка, текущий NAV и движения выбранного периода.",
    }
    report.summary["portfolio_period"] = period
    workbook, json_path = export_analysis(report, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert list(sheets)[0] == "Портфель за период"
    headline = sheets["Портфель за период"]
    assert headline.filter(pl.col("показатель") == "Полный результат портфеля")["значение"].to_list() == [3234.22]
    assert headline.filter(pl.col("показатель") == "Доходность портфеля за период")["значение"].to_list() == [period["period_return"]]
    trading = sheets["Аналитика"]
    assert trading.filter(pl.col("показатель") == "Результат закрытий и ВМ после комиссий")["значение"].to_list() == [-92]
    metadata = dict(sheets["Параметры расчёта"].iter_rows())
    assert orjson.loads(metadata["portfolio_period"])["total_profit"] == "3234.22"
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["portfolio_period"]["total_profit"]) == Decimal("3234.22")
    assert Decimal(payload["summary"]["net_pnl"]) == -92


def test_large_risk_metadata_is_saved_in_lossless_excel_sized_parts(tmp_path):
    from trade_exporter.exporters.analytics import analysis_metadata
    from trade_exporter.exporters.excel import write_workbook

    risk = {"metrics": [], "quality": ["длинное пояснение " * 4000], "capital": Decimal("130135.23")}
    frame = analysis_metadata({"risk": risk})
    keys = frame["ключ"].to_list()
    assert keys == [f"risk__part_{index:04d}" for index in range(1, frame.height + 1)]
    assert frame.height > 1
    assert max(len(value) for value in frame["значение"]) <= 12000
    workbook = write_workbook(tmp_path / "long_metadata.xlsx", {"Параметры расчёта": frame})
    saved = pl.read_excel(workbook, sheet_name="Параметры расчёта")
    restored = orjson.loads("".join(saved["значение"]))
    assert restored["quality"] == risk["quality"]
    assert restored["capital"] == "130135.23"
