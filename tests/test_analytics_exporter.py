from datetime import datetime, timezone
from decimal import Decimal
from xml.etree import ElementTree
from zipfile import ZipFile

import orjson
import polars as pl

from trade_exporter.domain.models import ClosedTrade
from trade_exporter.exporters.analytics import export_analysis
from trade_exporter.services.analytics import analyze_trades


def make_report():
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
        for day, ticker, gross in [(11, "AAA", "-10"), (12, "BBB", "20")]
    ]
    return analyze_trades(trades, timezone.utc)


def test_report_contains_readable_sheets_and_native_charts(tmp_path):
    report = make_report()
    workbook, json_path = export_analysis(report, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0, engine="calamine")
    assert list(sheets) == ["Аналитика", "Сделки", "Дни", "Месяцы", "Тикеры"]
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
