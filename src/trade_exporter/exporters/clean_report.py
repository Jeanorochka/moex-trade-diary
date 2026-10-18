from decimal import Decimal
from math import ceil, floor, log10
from pathlib import Path
from tempfile import TemporaryFile
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import polars as pl

from trade_exporter.services.report_view import report_metrics, report_rows


SHEET = "Отчёт"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
MONEY_FORMAT = "#,##0.00"


def clean_report(analysis, portfolio, summary, dates):
    closed, opened, best, worst, assets, gross, currency = report_rows(analysis, portfolio, summary)
    cells, merged, heights, breaks = {}, [], {}, []
    row = 1
    page_height = 0
    ink, muted, pale = "#203447", "#66788A", "#EDF2F6"

    def height(points=20):
        nonlocal row, page_height
        if page_height + points > 725:
            breaks.append(row - 1)
            page_height = 0
        heights[row - 1] = points
        page_height += points
        row += 1

    def cell(column, value, format_code=None, **style):
        if isinstance(value, Decimal):
            value = float(value)
        options = {"value": value, "format": {"font_name": "Calibri", "font_size": 10, "font_color": ink, "align_vertical": "center", "border": True, "border_color": "#CBD5E1", **style}}
        if format_code:
            options["num_format"] = format_code
        cells[f"{column}{row}"] = options

    def title(text, large=False):
        merged.append((f"A{row}:E{row}", text, {"font_name": "Calibri", "font_size": 17 if large else 11, "bold": True, "font_color": ink, "bg_color": "#FFFFFF" if large else pale, "align_vertical": "center"}))
        height(30 if large else 25)

    def space():
        height(10)

    def section(text, minimum=65):
        nonlocal page_height
        if page_height and page_height + minimum > 725:
            breaks.append(row - 1)
            page_height = 0
        space()
        title(text)

    def headers(values):
        for column, label in zip("ABCDE", values):
            cell(column, label, bold=True, font_color=muted, border_bottom=True, border_color="#DCE4EB")
        height(22)

    def results(text, rows, open_section=False):
        section(text)
        header = ("дата", "тикер", "вход", "сейчас" if open_section else "выход / сейчас" if text in {"лучшие 5", "худшие 5"} else "выход", f"финрез, {currency}")
        headers(header)
        if not rows:
            for column in "ABCDE":
                cell(column, None)
            cell("B", "—")
            height()
        for item in rows:
            if page_height + 20 > 725:
                height(0)
                headers(header)
            cell("A", item.day, "dd.mm.yyyy")
            cell("B", item.ticker, font_color="#247C72" if item.opened else ink)
            cell("C", item.entry, "0.00######")
            cell("D", item.exit, "0.00######")
            cell("E", item.result, MONEY_FORMAT, font_color="#B44C59" if item.result is not None and item.result < 0 else "#247C72", bold=True)
            height()

    def metric_rows(rows):
        for label, value, format_code in rows:
            merged.append((f"A{row}:D{row}", label, {"font_name": "Calibri", "font_size": 10, "font_color": ink, "align_vertical": "center", "border": True, "border_color": "#CBD5E1"}))
            cell("E", Decimal(str(value)) if value is not None else None, format_code, bold=True)
            height(21)

    title("дневник сделок", True)
    title(f"{dates[0]:%d.%m.%Y} — {dates[1]:%d.%m.%Y}")
    results("сделки, до комиссий" if gross else "сделки", closed)
    if opened:
        results("открытые позиции", opened, True)
    results("лучшие 5", best)
    results("худшие 5", worst)
    metrics, extrapolation = report_metrics(summary, portfolio)
    section(f"показатели портфеля, {currency}", minimum=25 + 10 + len(metrics) * 21)
    metric_rows(metrics)
    section("экстраполяция", minimum=79)
    metric_rows(extrapolation)
    charts = {}
    if assets:
        section(f"результат по активам, {currency}", minimum=min(725, 57 + len(assets) * 20))
        headers(("", "тикер", "закрыто", "открыто", "итого"))
        start = row
        for ticker, realized, unrealized, total in assets:
            cell("A", None)
            cell("B", ticker)
            cell("C", realized, MONEY_FORMAT)
            cell("D", unrealized, MONEY_FORMAT)
            cell("E", total, MONEY_FORMAT, bold=True)
            height()
        end = row - 1
        space()
        chart_height = max(330, min(870, len(assets) * 25 + 90))
        if page_height + 31 + chart_height * 0.75 > 725:
            breaks.append(row - 1)
            page_height = 0
        metric_rows([("комиссий всего:", summary.get("commission") if summary.get("trading_history_loaded") is not False else None, MONEY_FORMAT)])
        space()
        charts[f"A{row}"] = {
            "type": "bar", "title": "закрытый + открытый результат" + (", до комиссий" if gross else ""),
            "categories_range": f"'{SHEET}'!$B${start}:$B${end}",
            "values_range": f"'{SHEET}'!$E${start}:$E${end}",
            "width": 620, "height": chart_height, "show_legend": False,
        }
        for _ in range(ceil(chart_height / (20 * 4 / 3))):
            height()
    frame = pl.DataFrame({name: [None] * (row - 1) for name in "ABCDE"}, schema={name: pl.String for name in "ABCDE"})
    options = {
        "header": False, "autofit": False, "autofilter": False,
        "freeze_panes": (4, 0), "column_widths": {0: 13, 1: 24, 2: 15, 3: 15, 4: 18},
        "cells": cells, "merged_ranges": merged, "row_heights": heights, "charts": charts,
    }
    return {SHEET: frame}, {SHEET: options}, row - 1, sorted(set(breaks))


def set_print_layout(path, last_row, breaks):
    path = Path(path)
    ET.register_namespace("", NS)
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")

    def tag(name):
        return f"{{{NS}}}{name}"

    with TemporaryFile() as buffer:
        with ZipFile(path) as source, ZipFile(buffer, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    sheet = ET.fromstring(data)
                    properties = sheet.find(tag("sheetPr"))
                    if properties is None:
                        properties = ET.Element(tag("sheetPr"))
                        sheet.insert(0, properties)
                    ET.SubElement(properties, tag("pageSetUpPr"), {"fitToPage": "1"})
                    for view in sheet.findall(f"{tag('sheetViews')}/{tag('sheetView')}"):
                        view.set("showGridLines", "0")
                    margins = sheet.find(tag("pageMargins"))
                    if margins is None:
                        margins = ET.SubElement(sheet, tag("pageMargins"))
                    margins.attrib.update(left="0.3", right="0.3", top="0.4", bottom="0.4", header="0.15", footer="0.15")
                    setup = ET.Element(tag("pageSetup"), {"paperSize": "9", "orientation": "portrait", "fitToWidth": "1", "fitToHeight": "0"})
                    sheet.insert(list(sheet).index(margins) + 1, setup)
                    if breaks:
                        row_breaks = ET.Element(tag("rowBreaks"), {"count": str(len(breaks)), "manualBreakCount": str(len(breaks))})
                        for number in breaks:
                            ET.SubElement(row_breaks, tag("brk"), {"id": str(number), "min": "0", "max": "16383", "man": "1"})
                        sheet.insert(list(sheet).index(setup) + 1, row_breaks)
                    data = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
                elif info.filename == "xl/workbook.xml":
                    workbook = ET.fromstring(data)
                    names = workbook.find(tag("definedNames"))
                    if names is None:
                        names = ET.Element(tag("definedNames"))
                        sheets = workbook.find(tag("sheets"))
                        workbook.insert(list(workbook).index(sheets) + 1, names)
                    for name, value in (("_xlnm.Print_Area", f"'{SHEET}'!$A$1:$E${last_row}"), ("_xlnm.Print_Titles", f"'{SHEET}'!$1:$2")):
                        ET.SubElement(names, tag("definedName"), {"name": name, "localSheetId": "0"}).text = value
                    data = ET.tostring(workbook, encoding="utf-8", xml_declaration=True)
                elif info.filename.startswith("xl/charts/chart") and info.filename.endswith(".xml"):
                    chart = ET.fromstring(data)
                    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
                    for labels in chart.findall(f".//{{{chart_ns}}}catAx/{{{chart_ns}}}tickLblPos"):
                        labels.set("val", "low")
                    _format_chart_numbers(chart, chart_ns)
                    data = ET.tostring(chart, encoding="utf-8", xml_declaration=True)
                target.writestr(info, data)
        buffer.seek(0)
        path.write_bytes(buffer.read())


def _format_chart_numbers(chart, namespace):
    def tag(name):
        return f"{{{namespace}}}{name}"

    values = [float(value.text) for value in chart.findall(f".//{tag('val')}//{tag('pt')}/{tag('v')}") if value.text]
    low, high = min([0, *values]), max([0, *values])
    target = (high - low) / 4 or 1
    scale = 10 ** floor(log10(target))
    step = next(factor * scale for factor in (1, 2, 5, 10) if factor * scale >= target)
    format_code = "#,##0" if step >= 1 else "#,##0." + "0" * ceil(-log10(step))
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    for axis in chart.findall(f".//{tag('valAx')}"):
        axis.find(tag("numFmt")).attrib.update(formatCode=format_code, sourceLinked="0")
        scaling = axis.find(tag("scaling"))
        ET.SubElement(scaling, tag("max"), {"val": str(ceil(high / step) * step if high else 0)})
        ET.SubElement(scaling, tag("min"), {"val": str(floor(low / step) * step if low else 0)})
        if low == high:
            scaling.find(tag("max")).set("val", str(step))
        ET.SubElement(axis, tag("majorUnit"), {"val": str(step)})
        text = ET.Element(tag("txPr"))
        ET.SubElement(text, f"{{{drawing_ns}}}bodyPr", {"rot": "0"})
        ET.SubElement(text, f"{{{drawing_ns}}}lstStyle")
        paragraph = ET.SubElement(text, f"{{{drawing_ns}}}p")
        properties = ET.SubElement(paragraph, f"{{{drawing_ns}}}pPr")
        font = ET.SubElement(properties, f"{{{drawing_ns}}}defRPr", {"sz": "1100"})
        ET.SubElement(font, f"{{{drawing_ns}}}latin", {"typeface": "Calibri"})
        axis.insert(list(axis).index(axis.find(tag("crossAx"))), text)
