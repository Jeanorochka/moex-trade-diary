from base64 import b64decode
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from math import expm1, log1p
from types import SimpleNamespace
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


def test_unified_report_is_one_pair_with_positive_nav_and_separate_negative_trades(tmp_path):
    source = analysis()
    current = portfolio()
    workbook, json_path = export_report(source, tmp_path, portfolio_report=current)
    assert set(tmp_path.iterdir()) == {workbook, json_path}
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert list(sheets) == ["Портфель за период", "Лидеры и потери", "Результаты", "Позиции", "Потоки периода", "Риск и доходность", "Параметры расчёта"]
    primary = sheets["Портфель за период"]
    assert primary.filter(pl.col("показатель") == "Полный результат портфеля")["значение"].to_list() == [10]
    assert primary.filter(pl.col("показатель") == "Капитал сейчас: сводная оценка брокера")["значение"].to_list() == [120]
    assert any("не заменяют полный результат" in value for value in primary["основание"] if value)
    assert "Ручной журнал" in primary.filter(pl.col("показатель") == "Источник денежных движений")["основание"][0]
    assert sheets["Результаты"]["финрез net"].sum() == -10
    assert sheets["Результаты"]["тип результата"].to_list() == ["stock_fifo", "stock_fifo", "futures_margin"]
    assert sheets["Потоки периода"]["сумма"].to_list() == [10]
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["headline_profit"]) == 10
    assert Decimal(payload["summary"]["net_pnl"]) == -10
    assert Decimal(payload["portfolio"]["summary"]["equity"]) == 120
    assert len(payload["trades"]) == len(payload["results"]) == 3
    assert len(payload["daily"]) == 3
    assert len(payload["monthly"]) == 3
    assert len(payload["by_ticker"]) == 3
    assert payload["summary"]["portfolio_period"]["cash_flow_mode"] == "manual"
    assert payload["futures_basis"] == "paid_margin_plus_change_in_pending_margin_including_open_positions"
    with ZipFile(workbook) as archive:
        charts = [name for name in archive.namelist() if name.startswith("xl/charts/chart") and name.endswith(".xml")]
        assert len(charts) == 1
        chart = ElementTree.fromstring(archive.read(charts[0]))
        namespace = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
        assert chart.find(".//c:barChart", namespace) is not None
        formulas = [ref.text for ref in chart.findall(".//c:f", namespace)]
        assert any("Лидеры и потери" in formula for formula in formulas)
    offline = analyze_file(workbook)
    assert offline.summary["net_pnl"] == -10
    assert offline.summary["headline_profit"] == 10
    assert offline.summary["portfolio_period"]["cash_flow_mode"] == "manual"


def test_cash_balances_keep_their_currency_and_are_not_added_to_nav(tmp_path):
    current = portfolio()
    cash = pl.DataFrame({
        "currency": ["RUB", "USD"], "amount": [Decimal("5"), Decimal("2")],
        "amount_base": [Decimal("5"), None],
    }, schema=current.cash.schema)
    current = replace(current, cash=cash)
    workbook, json_path = export_report(analysis(), tmp_path, portfolio_report=current)
    frame = pl.read_excel(workbook, sheet_name="Портфель за период")
    assert frame.filter(pl.col("показатель") == "Деньги сейчас: USD")["значение"].to_list() == [2]
    assert frame.filter(pl.col("показатель") == "Деньги сейчас: USD")["единица"].to_list() == ["USD"]
    assert frame.filter(pl.col("показатель") == "Капитал в конце")["значение"].to_list() == [120]
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["portfolio"]["summary"]["equity"]) == 120
    assert payload["portfolio"]["cash"][1]["amount_base"] is None


def test_standard_fifo_without_event_type_has_offline_readable_results(tmp_path):
    source = analysis()
    standard = analyze_results(source.trades.filter(pl.col("event_type") == "stock_fifo").drop("event_type"), Decimal("100"))
    standard = replace(standard, trades=standard.trades.drop("event_type"))
    workbook, json_path = export_report(standard, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert list(sheets)[0] == "Итоги"
    assert sheets["Результаты"]["тип результата"].to_list() == ["fifo_closed", "fifo_closed"]
    assert analyze_file(workbook).summary["net_pnl"] == standard.summary["net_pnl"]
    assert orjson.loads(json_path.read_bytes())["portfolio"] is None


def test_unallocated_cash_fees_remain_visible_and_offline_totals_are_preserved(tmp_path):
    source = analysis(unallocated=True)
    source.summary.update(commission_complete=False, commission_quality=["Известны не все расходы."], result_complete=False, result_quality=["BOND исключён из P&L."], excluded_instruments=["BOND"])
    workbook, json_path = export_report(source, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0)
    rows = sheets["Результаты"]
    assert rows["финрез после известных комиссий"].to_list() == [None, None, None, -3]
    assert all("не распределена" in value for value in rows["основа комиссии"][:3])
    assert any("не распределены" in value for value in sheets["Итоги"]["основание"])
    assert any("BOND исключён" in value for value in sheets["Итоги"]["основание"])
    rankings = sheets["Лидеры и потери"]
    known = rankings.filter(pl.col("результат").is_not_null())
    assert all("gross" in value for value in known["основа"])
    assert "COMMISSION" not in rankings["тикер"].to_list()
    assert analyze_file(workbook).summary["net_pnl"] == -10
    assert analyze_file(workbook).summary["commission_complete"] is False
    payload = orjson.loads(json_path.read_bytes())
    assert payload["summary"]["excluded_instruments"] == ["BOND"]


def test_details_and_broker_sources_are_embedded_without_temporary_paths(tmp_path):
    sources = tmp_path / "inputs"
    sources.mkdir()
    html = sources / "broker.xls"
    binary = sources / "broker.xlsx"
    html.write_bytes("<html>брокерский отчёт</html>".encode())
    binary.write_bytes(b"PK\x00\xff\x10")
    source = analysis()
    source = replace(source, details={
        "executions": pl.DataFrame({"id": ["execution"], "amount": [Decimal("1.234567890123456789")]}),
        "futures_margin": pl.DataFrame({"source_file": [str(html)], "gross_pnl": [Decimal("2.5")]}),
        "source_reports": [html, binary],
    })
    output = tmp_path / "out"
    workbook, json_path = export_report(source, output)
    payload = orjson.loads(json_path.read_bytes())
    details = payload["details"]
    assert details["executions"][0]["amount"] == "1.234567890123456789"
    assert details["futures_margin"][0]["source_file"] == "broker.xls"
    assert details["source_reports"][0] == {"name": "broker.xls", "encoding": "utf-8", "content": "<html>брокерский отчёт</html>"}
    assert b64decode(details["source_reports"][1]["content"]) == binary.read_bytes()
    assert str(sources) not in json_path.read_text(encoding="utf-8")
    assert set(output.iterdir()) == {workbook, json_path}


def test_portfolio_only_report_has_open_rankings_without_false_zero_trading_profit(tmp_path):
    workbook, json_path = export_report(None, tmp_path, portfolio_report=portfolio())
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert sheets["Результаты"].is_empty()
    assert "Открытые позиции: прибыль" in sheets["Лидеры и потери"]["раздел"].to_list()
    assert any("не загружена" in value for value in sheets["Портфель за период"]["основание"] if value)
    assert orjson.loads(json_path.read_bytes())["summary"]["trading_history_loaded"] is False


def test_unavailable_nav_cannot_turn_into_portfolio_profit(tmp_path):
    current = portfolio()
    current.summary["portfolio_period"].update(status="unavailable", total_profit=None, period_return=None, annualized_return=None)
    workbook, json_path = export_report(analysis(), tmp_path, portfolio_report=current)
    primary = pl.read_excel(workbook, sheet_name="Портфель за период")
    assert primary.filter(pl.col("показатель") == "Полный результат портфеля")["значение"].to_list() == [None]
    assert orjson.loads(json_path.read_bytes())["summary"]["headline_profit"] is None


def test_report_filename_collision_does_not_overwrite_existing_pair(tmp_path, monkeypatch):
    frozen = SimpleNamespace(now=lambda: datetime(2026, 10, 17, 12))
    monkeypatch.setattr(report_exporter, "datetime", frozen)
    monkeypatch.setattr(report_exporter, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    first = export_report(analysis(), tmp_path, prefix="report.copy")
    first_bytes = [path.read_bytes() for path in first]
    second = export_report(analysis(), tmp_path, prefix="report.copy")
    assert set(first).isdisjoint(second)
    assert [path.read_bytes() for path in first] == first_bytes
    assert len(list(tmp_path.iterdir())) == 4


def test_failed_workbook_does_not_leave_half_report_or_remove_other_files(tmp_path, monkeypatch):
    existing = tmp_path / "keep.txt"
    existing.write_text("keep")

    def fail(path, *args, **kwargs):
        path.write_bytes(b"partial")
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(report_exporter, "write_workbook", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        export_report(analysis(), tmp_path)
    assert set(tmp_path.iterdir()) == {existing}
    assert existing.read_text() == "keep"


def test_missing_broker_source_fails_before_creating_output_pair(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        export_report(analysis(), output, details={"source_reports": [tmp_path / "missing.xlsx"]})
    assert not output.exists()


def test_empty_report_keeps_headers_without_invalid_charts(tmp_path):
    workbook, json_path = export_report(analyze_trades([], UTC), tmp_path)
    assert pl.read_excel(workbook, sheet_name="Результаты").is_empty()
    assert orjson.loads(json_path.read_bytes())["summary"]["trades"] == 0
    with ZipFile(workbook) as archive:
        assert not any(name.startswith("xl/charts/chart") for name in archive.namelist())


def test_ranking_dates_and_counts_have_explicit_excel_formats(tmp_path):
    workbook, _ = export_report(analysis(), tmp_path)
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(workbook) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        formats = {row.get("numFmtId"): row.get("formatCode") for row in styles.findall("s:numFmts/s:numFmt", namespace)}

        def number_format(address):
            cell = sheet.find(f".//s:c[@r='{address}']", namespace)
            style = styles.find("s:cellXfs", namespace)[int(cell.get("s", "0"))]
            return formats.get(style.get("numFmtId"), {"0": "General", "1": "0"}.get(style.get("numFmtId"), style.get("numFmtId")))

        assert number_format("C5") == "dd.mm.yyyy"
        assert number_format("B2") == "0"
        assert number_format("H2") == "0"
    saved = pl.read_excel(workbook, sheet_name="Лидеры и потери")
    assert saved["дата"].drop_nulls()[0] == datetime(2026, 4, 18).date()


def test_long_exclusion_reasons_are_not_repeated_in_result_rows(tmp_path):
    source = analysis(unallocated=True)
    reasons = [f"BOND{index}: инструмент исключён. " + "Подробная причина исключения. " * 12 for index in range(7)]
    source.summary.update(result_complete=False, result_quality=reasons, excluded_instruments=[f"BOND{index}" for index in range(7)])
    workbook, json_path = export_report(source, tmp_path)
    rows = pl.read_excel(workbook, sheet_name="Результаты")
    assert rows["качество торгового расчёта"].unique().to_list() == ["Расчёт частичный; исключённые инструменты и причины — на первом листе."]
    assert max(len(value) for value in rows["основа комиссии"]) < 100
    assert all("BOND" not in value for value in rows["качество торгового расчёта"])
    first = pl.read_excel(workbook, sheet_name="Итоги")
    assert all(reason in first["основание"].to_list() for reason in reasons)
    assert orjson.loads(json_path.read_bytes())["summary"]["result_quality"] == reasons
    assert analyze_file(workbook).summary["result_quality"] == reasons
    with ZipFile(workbook) as archive:
        namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet3.xml"))
        heights = [float(row.get("ht")) for row in sheet.findall("s:sheetData/s:row", namespace) if row.get("ht")]
        assert heights and max(heights) <= 42
        assert sum(heights) <= 42 * (rows.height + 1)
