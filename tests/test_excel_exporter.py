from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import fastexcel
import polars as pl
import pytest

from trade_exporter.domain.models import ClosedTrade
from trade_exporter.exporters.excel import ExcelExporter, write_workbook

UTC = timezone.utc
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sample_trade():
    return ClosedTrade(
        exit_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        instrument_id="uid",
        ticker="IMOEXF",
        direction="LONG",
        quantity=1,
        entry_price=Decimal("2300"),
        exit_price=Decimal("2306"),
        gross_pnl=Decimal("60"),
        commission=Decimal("18.30"),
    )


def export(exporter, trades=None, display_timezone=UTC):
    return exporter.export(
        [sample_trade()] if trades is None else trades,
        "tbank",
        "123456789",
        datetime(2026, 9, 11, tzinfo=UTC),
        datetime(2026, 9, 11, 23, 59, tzinfo=UTC),
        display_timezone,
    )


def xml_part(path, part):
    with ZipFile(path) as workbook:
        return ET.fromstring(workbook.read(part))


def cell_format(path, sheet_number, address):
    sheet = xml_part(path, f"xl/worksheets/sheet{sheet_number}.xml")
    cell = sheet.find(f".//s:c[@r='{address}']", NS)
    styles = xml_part(path, "xl/styles.xml")
    style = styles.find("s:cellXfs", NS)[int(cell.get("s", "0"))]
    number_format_id = style.get("numFmtId")
    for number_format in styles.findall("s:numFmts/s:numFmt", NS):
        if number_format.get("numFmtId") == number_format_id:
            return number_format.get("formatCode")
    return number_format_id


def test_excel_has_exact_base_columns_and_ddmmyy_date(tmp_path):
    exporter = ExcelExporter(tmp_path, extra_columns=False, include_summary_sheet=False)
    path = export(exporter)
    reader = fastexcel.read_excel(path)
    assert reader.sheet_names == ["Сделки"]
    frame = pl.read_excel(path, sheet_name="Сделки")
    assert frame.columns == ExcelExporter.BASE_HEADERS
    assert frame.row(0) == ("11.09.26", "IMOEXF", 2300, 2306, 60, 18.3)
    assert frame.schema["дата"] == pl.String


def test_every_export_gets_a_new_filename(tmp_path):
    exporter = ExcelExporter(tmp_path, include_summary_sheet=False)
    first = export(exporter)
    second = export(exporter)
    assert first != second
    assert first.exists()
    assert second.exists()
    assert first.name.startswith("trades_tbank_456789_20260911_20260911_")


def test_extra_columns_and_display_timezone_are_preserved(tmp_path):
    trade = replace(
        sample_trade(),
        exit_at=datetime(2026, 9, 11, 23, tzinfo=UTC),
        direction="SHORT",
        quantity=3,
    )
    path = export(
        ExcelExporter(tmp_path, extra_columns=True, include_summary_sheet=False),
        [trade],
        timezone(timedelta(hours=3)),
    )
    frame = pl.read_excel(path, sheet_name="Сделки")
    assert frame.columns == ExcelExporter.BASE_HEADERS + ExcelExporter.EXTRA_HEADERS
    assert frame.row(0) == (
        "12.09.26", "IMOEXF", 2300, 2306, 60, 18.3, "SHORT", 3, 41.7
    )


def test_summary_retains_layout_and_uses_net_results(tmp_path):
    trades = [
        sample_trade(),
        replace(sample_trade(), gross_pnl=Decimal("5"), commission=Decimal("8")),
        replace(sample_trade(), gross_pnl=Decimal("2"), commission=Decimal("2")),
    ]
    path = export(ExcelExporter(tmp_path), trades)
    assert fastexcel.read_excel(path).sheet_names == ["Сделки", "Итоги"]
    summary = pl.read_excel(path, sheet_name="Итоги", has_header=False)
    assert summary.get_column("column_0").to_list() == [
        "Закрытых сделок", "Прибыльных", "Убыточных", "Win rate",
        "Gross P&L", "Комиссия", "Net P&L",
    ]
    assert summary.get_column("column_1").to_list() == pytest.approx(
        [3, 1, 1, 1 / 3, 67, 28.3, 38.7]
    )
    assert cell_format(path, 2, "B4") == "0.00%"
    assert all(cell_format(path, 2, cell) == "0.00" for cell in ("B5", "B6", "B7"))


@pytest.mark.parametrize("extra_columns", [False, True])
def test_empty_export_still_has_headers_and_zero_summary(tmp_path, extra_columns):
    path = export(ExcelExporter(tmp_path, extra_columns=extra_columns), [])
    frame = pl.read_excel(path, sheet_name="Сделки")
    expected = ExcelExporter.BASE_HEADERS + (ExcelExporter.EXTRA_HEADERS if extra_columns else [])
    assert frame.columns == expected
    assert frame.height == 0
    summary = pl.read_excel(path, sheet_name="Итоги", has_header=False)
    assert summary.get_column("column_1").to_list() == [0] * 7
    assert xml_part(path, "xl/worksheets/sheet1.xml").find("s:autoFilter", NS).get("ref") == (
        "A1:I1" if extra_columns else "A1:F1"
    )


def test_workbook_retains_header_freeze_filter_and_number_formats(tmp_path):
    path = export(ExcelExporter(tmp_path, extra_columns=True))
    sheet = xml_part(path, "xl/worksheets/sheet1.xml")
    assert sheet.find("s:sheetViews/s:sheetView/s:pane", NS).get("topLeftCell") == "A2"
    assert sheet.find("s:autoFilter", NS).get("ref") == "A1:I2"
    styles = xml_part(path, "xl/styles.xml")
    for cell in sheet.findall("s:sheetData/s:row[@r='1']/s:c", NS):
        style = styles.find("s:cellXfs", NS)[int(cell.get("s", "0"))]
        font = styles.find("s:fonts", NS)[int(style.get("fontId", "0"))]
        assert font.find("s:b", NS) is not None
    assert all(cell_format(path, 1, cell) == "0.######" for cell in ("C2", "D2"))
    assert all(cell_format(path, 1, cell) == "0.00" for cell in ("E2", "F2", "I2"))
    assert sheet.find("s:sheetData/s:row[@r='2']/s:c[@r='A2']", NS).get("t") == "s"


def test_generic_writer_preserves_sheet_order_and_per_sheet_options(tmp_path):
    path = write_workbook(
        tmp_path / "nested" / "analytics.xlsx",
        {"daily": pl.DataFrame({"net": [41.7]}), "metrics": pl.DataFrame({"value": [1]})},
        sheet_options={"metrics": {"header": False, "autofilter": False}},
    )
    assert fastexcel.read_excel(path).sheet_names == ["daily", "metrics"]
    assert pl.read_excel(path, sheet_name="daily")["net"].to_list() == [41.7]
    assert pl.read_excel(path, sheet_name="metrics", has_header=False).row(0) == (1,)
