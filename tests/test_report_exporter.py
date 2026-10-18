from base64 import b64decode
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from math import expm1, log1p
from xml.etree import ElementTree
from zipfile import ZipFile

import orjson
import polars as pl
import pytest

from trade_exporter.domain.portfolio import CashFlow, PortfolioPosition, PortfolioSnapshot
from trade_exporter.exporters import report as report_exporter
from trade_exporter.exporters.report import export_report
from trade_exporter.services.analytics import analyze_file, analyze_results, analyze_trades
from trade_exporter.services.portfolio import analyze_portfolio


UTC = timezone.utc
START = datetime(2025, 10, 17, 12, tzinfo=UTC)
END = datetime(2026, 10, 17, 12, tzinfo=UTC)
DEPOSIT = datetime(2026, 4, 17, 12, tzinfo=UTC)


def analysis(unallocated=False):
    rows = [
        {"date": datetime(2026, 4, 18).date(), "ticker": "AAA", "entry_price": "100", "exit_price": "80", "gross_pnl": "-20", "commission": "0" if unallocated else "1", "event_type": "stock_fifo"},
        {"date": datetime(2026, 5, 18).date(), "ticker": "BBB", "entry_price": "100", "exit_price": "115", "gross_pnl": "15", "commission": "0" if unallocated else "1", "event_type": "stock_fifo"},
        {"date": datetime(2026, 6, 18).date(), "ticker": "FUT", "entry_price": None, "exit_price": None, "gross_pnl": "-2", "commission": "0" if unallocated else "1", "event_type": "futures_margin"},
    ]
    if unallocated:
        rows.append({"date": datetime(2026, 7, 18).date(), "ticker": "COMMISSION", "entry_price": None, "exit_price": None, "gross_pnl": "0", "commission": "3", "event_type": "broker_fee"})
    value = analyze_results(pl.DataFrame(rows), Decimal("100"))
    value.summary.update({
        "commission_complete": True, "commission_basis": "transactions_period" if unallocated else "execution_fifo_and_futures",
        "commission_quality": [], "result_complete": True, "result_quality": [],
    })
    return value


def portfolio():
    flows = [CashFlow(START, Decimal("100"), "RUB"), CashFlow(DEPOSIT, Decimal("10"), "RUB")]
    value = analyze_portfolio(
        PortfolioSnapshot("account", "finam", END, "RUB", Decimal("120"), positions=(
            PortfolioPosition("OPEN@MISX", "OPEN", "equities", Decimal("1"),
                              average_price=Decimal("100"), current_price=Decimal("120"), price_as_of=END),
        )), flows, ledger_complete=True, ledger_start=START,
    )
    weight = Decimal(str((END - DEPOSIT).total_seconds() / (END - START).total_seconds()))
    rate = float(Decimal("10") / (Decimal("100") + Decimal("10") * weight))
    note = "Полный ручной журнал пользователя; даты движений учитываются на конец дня."
    value.summary["portfolio_period"] = {
        "status": "complete", "base_currency": "RUB", "started_at": START.isoformat(), "ended_at": END.isoformat(),
        "requested_end": datetime(2026, 10, 18, tzinfo=UTC).isoformat(), "starting_equity": Decimal("100"),
        "ending_equity": Decimal("120"), "external_inflows": Decimal("10"), "external_outflows": Decimal("0"),
        "net_external_flows": Decimal("10"), "total_profit": Decimal("10"), "period_return": rate,
        "annualized_return": expm1(log1p(rate)), "xirr": None, "days": 365,
        "external_flows": [{"timestamp": DEPOSIT.isoformat(), "amount": Decimal("10"), "currency": "RUB"}],
        "valuation_basis": "current_broker_nav", "source": "Broker NAV", "cash_flow_mode": "manual",
        "cash_flow_basis": note, "quality": [note],
    }
    value.summary["risk"] = {
        "as_of": END.isoformat(), "metrics": [
            {"metric": "capital_period_profit", "label": "Полный результат периода", "value": Decimal("10"), "unit": "RUB", "basis": "starting_capital_assumption", "quality_reason": note},
            {"metric": "capital_period_return", "label": "Доходность портфеля за период", "value": rate, "unit": "fraction", "basis": "starting_capital_assumption", "quality_reason": note},
        ], "quality": [note],
    }
    return replace(value, period_flows=value.flows.filter(pl.col("timestamp") > START))


def cells(path):
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        values = ["".join(part.text or "" for part in item.findall(".//s:t", namespace)) for item in strings]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        result = {}
        for cell in sheet.findall(".//s:c", namespace):
            value = cell.find("s:v", namespace)
            if value is not None:
                result[cell.get("r")] = values[int(value.text)] if cell.get("t") == "s" else float(value.text)
        return result


def metric(path, label):
    values = cells(path)
    address = next(key for key, value in values.items() if key.startswith("A") and value == label)
    return values.get("E" + address[1:])


def test_one_sheet_preserves_nav_and_full_json_without_adding_open_pnl_twice(tmp_path):
    workbook, json_path = export_report(analysis(), tmp_path, portfolio_report=portfolio())
    assert set(tmp_path.iterdir()) == {workbook, json_path}
    assert list(pl.read_excel(workbook, sheet_id=0)) == ["Отчёт"]
    assert workbook.name == "17.10.2025_17.10.2026.xlsx"
    assert metric(workbook, "капитал в конце") == 120
    assert metric(workbook, "финрез портфеля") == 10
    assert metric(workbook, "текущая годовая доходность") == pytest.approx(portfolio().summary["portfolio_period"]["annualized_return"])
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["headline_profit"]) == 10
    assert Decimal(payload["summary"]["net_pnl"]) == -10
    assert len(payload["results"]) == len(payload["trades"]) == 3
    assert payload["portfolio"]["period_flows"][0]["amount"] == "10.000000000000000000"
    assert payload["futures_basis"] == "paid_margin_plus_change_in_pending_margin_including_open_positions"
    offline = analyze_file(workbook)
    assert offline.summary["net_pnl"] == -10
    assert offline.summary["headline_profit"] == 10
    assert offline.summary["portfolio_period"]["cash_flow_mode"] == "manual"


def test_same_asset_combines_closed_and_unrealized_for_chart(tmp_path):
    from trade_exporter.services.report_view import report_rows
    current = portfolio()
    current = replace(current, positions=current.positions.with_columns(pl.lit("BBB").alias("ticker"), pl.lit("BBB@MISX").alias("instrument_id")))
    source = analysis()
    closed, opened, best, worst, assets, *_ = report_rows(source, current, source.summary)
    assert len(closed) == 3 and len(opened) == 1
    assert best[0].opened and best[0].result == 20
    assert next(row for row in assets if row[0] == "BBB@MISX")[1:] == (14, 20, 34)
    workbook, _ = export_report(source, tmp_path, portfolio_report=current)
    values = cells(workbook)
    best_row = next(int(key[1:]) for key, value in values.items() if value == "лучшие 5")
    assert values[f"B{best_row + 2}"] == "BBB@MISX"
    assert values[f"E{best_row + 2}"] == 20


def test_futures_vm_includes_open_contract_and_is_not_added_again():
    from trade_exporter.services.report_view import report_rows
    current = portfolio()
    current = replace(current, positions=current.positions.with_columns(pl.lit("FUT").alias("ticker"), pl.lit("FUT").alias("instrument_id"), pl.lit("FUTURES").alias("asset_type")))
    rows = report_rows(analysis(), current, analysis().summary)
    assert next(row for row in rows[4] if row[0] == "FUT")[3] == -3


@pytest.mark.parametrize("change", [{"valuation_currency": "USD"}, {"price_status": "stale"}, {"quantity_includes_pending_orders": True}])
def test_unknown_open_pnl_is_not_ranked_as_known_profit(change):
    from trade_exporter.services.report_view import report_rows
    current = portfolio()
    current = replace(current, positions=current.positions.with_columns(pl.lit(value).alias(key) for key, value in change.items()))
    rows = report_rows(analysis(), current, analysis().summary)
    assert rows[1][0].result is None
    assert not any(item.opened for item in rows[2] + rows[3])


def test_historical_report_never_adds_todays_open_pnl():
    from trade_exporter.services.report_view import report_rows
    current = portfolio()
    source = analysis()
    source.summary["portfolio_period"] = {**current.summary["portfolio_period"], "valuation_basis": "historical_broker_report"}
    rows = report_rows(source, current, source.summary)
    assert rows[1] == []
    assert "OPEN@MISX" not in [row[0] for row in rows[4]]


def test_cash_currencies_and_unknown_values_stay_in_json_without_inflating_nav(tmp_path):
    current = portfolio()
    current = replace(current, cash=pl.DataFrame({"currency": ["RUB", "USD"], "amount": [Decimal("5"), Decimal("2")], "amount_base": [Decimal("5"), None]}, schema=current.cash.schema))
    workbook, json_path = export_report(analysis(), tmp_path, portfolio_report=current)
    assert metric(workbook, "капитал в конце") == 120
    payload = orjson.loads(json_path.read_bytes())
    assert payload["portfolio"]["cash"][1]["amount_base"] is None
    assert payload["portfolio"]["cash"][1]["currency"] == "USD"


def test_unallocated_fees_and_quality_remain_in_json_and_offline_analysis(tmp_path):
    source = analysis(unallocated=True)
    reasons = ["BOND исключён из P&L. " * 200]
    source.summary.update(commission_complete=False, result_complete=False, result_quality=reasons, excluded_instruments=["BOND"])
    workbook, json_path = export_report(source, tmp_path)
    values = cells(workbook)
    assert "сделки, до комиссий" in values.values()
    assert "COMMISSION" not in values.values()
    assert metric(workbook, "комиссии сделок") == 3
    assert not any("BOND" in str(value) for value in values.values())
    payload = orjson.loads(json_path.read_bytes())
    assert payload["summary"]["result_quality"] == reasons
    assert len(payload["results"]) == 4
    assert analyze_file(workbook).summary["result_quality"] == reasons
    assert analyze_file(workbook).summary["net_pnl"] == -10


def test_sources_embedded_without_temporary_paths_or_extra_files(tmp_path):
    sources = tmp_path / "inputs"
    sources.mkdir()
    html, binary = sources / "broker.xls", sources / "broker.xlsx"
    html.write_bytes("<html>брокерский отчёт</html>".encode())
    binary.write_bytes(b"PK\x00\xff\x10")
    output = tmp_path / "out"
    workbook, json_path = export_report(analysis(), output, details={"source_reports": [html, binary], "source_file": str(html)})
    payload = orjson.loads(json_path.read_bytes())
    assert payload["details"]["source_file"] == "broker.xls"
    assert payload["details"]["source_reports"][0]["content"] == "<html>брокерский отчёт</html>"
    assert b64decode(payload["details"]["source_reports"][1]["content"]) == binary.read_bytes()
    assert str(sources) not in json_path.read_text(encoding="utf-8")
    assert set(output.iterdir()) == {workbook, json_path}


def test_portfolio_only_and_unavailable_nav_do_not_invent_trading_profit(tmp_path):
    current = portfolio()
    current.summary["portfolio_period"].update(status="unavailable", total_profit=None, period_return=None, annualized_return=None)
    workbook, json_path = export_report(None, tmp_path, portfolio_report=current)
    assert metric(workbook, "финрез портфеля") is None
    assert metric(workbook, "текущая годовая доходность") is None
    assert metric(workbook, "комиссии сделок") is None
    assert "OPEN@MISX" in cells(workbook).values()
    assert orjson.loads(json_path.read_bytes())["summary"]["trading_history_loaded"] is False


def test_report_filename_collision_does_not_overwrite_existing_pair(tmp_path):
    first = export_report(analysis(), tmp_path)
    content = [path.read_bytes() for path in first]
    second = export_report(analysis(), tmp_path)
    assert second[0].stem == first[0].stem + "_2"
    assert set(first).isdisjoint(second)
    assert [path.read_bytes() for path in first] == content
    assert len(list(tmp_path.iterdir())) == 4


def test_failed_workbook_cleans_half_pair_and_preserves_existing_files(tmp_path, monkeypatch):
    existing = tmp_path / "keep.txt"
    existing.write_text("keep")
    def fail(path, *args, **kwargs):
        path.write_bytes(b"partial")
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(report_exporter, "write_workbook", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        export_report(analysis(), tmp_path)
    assert set(tmp_path.iterdir()) == {existing}


def test_missing_source_fails_before_creating_output(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        export_report(analysis(), output, details={"source_reports": [tmp_path / "missing.xlsx"]})
    assert not output.exists()


def test_empty_report_has_no_invalid_chart_and_still_reopens(tmp_path):
    workbook, json_path = export_report(analyze_trades([], UTC), tmp_path)
    assert analyze_file(workbook).summary["trades"] == 0
    with ZipFile(workbook) as archive:
        assert not any(name.startswith("xl/charts/chart") for name in archive.namelist())


def test_a4_portrait_all_rows_and_native_numeric_dates(tmp_path):
    source = analysis()
    source = replace(source, trades=pl.concat([source.trades] * 110))
    workbook, _ = export_report(source, tmp_path, period_from=START, period_to=END)
    values = cells(workbook)
    assert sum(value == "AAA" for key, value in values.items() if key.startswith("B")) >= 110
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(workbook) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        setup = sheet.find("s:pageSetup", namespace)
        assert setup.attrib == {"paperSize": "9", "orientation": "portrait", "fitToWidth": "1", "fitToHeight": "0"}
        assert len(sheet.findall("s:rowBreaks/s:brk", namespace)) >= 8
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        date_cell = sheet.find(".//s:c[@r='A6']", namespace)
        assert date_cell.get("t") is None
        style = styles.find("s:cellXfs", namespace)[int(date_cell.get("s"))]
        assert styles.find(f"s:numFmts/s:numFmt[@numFmtId='{style.get('numFmtId')}']", namespace).get("formatCode") == "dd.mm.yyyy"
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        assert book.find("s:definedNames/s:definedName[@name='_xlnm.Print_Area']", namespace).text.startswith("'Отчёт'!$A$1:$E$")
        chart = ElementTree.fromstring(archive.read("xl/charts/chart1.xml"))
        cns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
        assert chart.find(".//c:barChart", cns) is not None
        assert all("Отчёт" in ref.text for ref in chart.findall(".//c:f", cns))


def test_reopening_clean_report_requires_its_json(tmp_path):
    workbook, json_path = export_report(analysis(), tmp_path)
    json_path.unlink()
    with pytest.raises(ValueError, match="JSON рядом"):
        analyze_file(workbook)


def test_standard_fifo_roundtrip_and_json_rankings_are_unchanged(tmp_path):
    from trade_exporter.services.rankings import build_rankings
    source = analysis()
    standard = analyze_results(source.trades.filter(pl.col("event_type") == "stock_fifo").drop("event_type"), Decimal("100"))
    standard = replace(standard, trades=standard.trades.drop("event_type"))
    workbook, json_path = export_report(standard, tmp_path)
    assert analyze_file(workbook).summary["net_pnl"] == standard.summary["net_pnl"]
    payload = orjson.loads(json_path.read_bytes())
    expected = orjson.loads(orjson.dumps(build_rankings(standard).to_dicts(), default=report_exporter._json_default))
    assert payload["rankings"] == expected
    assert payload["portfolio"] is None
