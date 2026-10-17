from datetime import date
from decimal import Decimal
from html import escape
from zipfile import ZipFile

import polars as pl
import pytest
from xlsxturbo import df_to_xlsx

from trade_exporter.services.finam_report import MARGIN_SCHEMA, parse_finam_margins


def workbook(path, rows):
    width = max(map(len, rows))
    frame = pl.DataFrame(
        [list(row) + [""] * (width - len(row)) for row in rows],
        schema={f"c{index}": pl.String for index in range(width)},
        orient="row",
    )
    df_to_xlsx(frame, path, header=False)
    return path


def html_table(rows):
    return "<table>" + "".join(
        "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    ) + "</table>"


def ledger(path, rows):
    return workbook(path, [
        ["Дата", "Инструмент", "Вариационная маржа", "Валюта", "№ операции"],
        *rows,
    ])


def positions(path, day, rows, extra=""):
    text = (
        f"<html><body><p>Справка о состоянии обязательств за период с {day} по {day}</p>"
        "<table><tr><td colspan='4'>Оценка позиции по Производным финансовым инструментам, руб.</td></tr>"
        "<tr><td rowspan='2'>Инструмент</td><td colspan='2'>Вариационная маржа</td><td rowspan='2'>Позиция на конец</td></tr>"
        "<tr><td>факт</td><td>план</td></tr>"
        + "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
        + "</table>" + extra + "</body></html>"
    )
    path.write_bytes(text.encode("cp1251"))
    return path


def test_xlsx_preserves_explicit_money_zero_rows_and_ignores_totals(tmp_path):
    path = ledger(tmp_path / "report.xlsx", [
        ["11.09.2026", "SiU6", "1\xa0234,56", "Рубль", "vm-1"],
        ["11.09.2026", "RIU6", "(12,34)", "RUB", "vm-2"],
        ["12.09.2026", "SiU6", "0", "RUB", "vm-3"],
        ["Итого", "", "1222,22", "RUB", ""],
    ])
    result = parse_finam_margins([path])
    assert result.schema == MARGIN_SCHEMA
    assert result["gross_pnl"].to_list() == [Decimal("1234.56"), Decimal("-12.34"), Decimal("0")]
    assert result["date"].to_list() == [date(2026, 9, 11), date(2026, 9, 11), date(2026, 9, 12)]
    assert result["ticker"].to_list() == ["SiU6", "RIU6", "SiU6"]
    assert result["source_id"].to_list() == ["vm-1", "vm-2", "vm-3"]
    assert result["source_file"].to_list() == [str(path)] * 3


def test_html_xls_explicit_margin_section_received_minus_paid(tmp_path):
    path = tmp_path / "report.xls"
    path.write_bytes(("<html>" + html_table([
        ["Вариационная маржа, руб."],
        ["Дата", "Контракт", "Зачислено", "Списано"],
        ["11.09.26", "RIU6", "", "15,20"],
        ["11.09.26", "SiU6", "150,00", "0"],
    ]) + "</html>").encode("cp1251"))
    result = parse_finam_margins([path])
    assert result["gross_pnl"].to_list() == [Decimal("-15.20"), Decimal("150")]
    assert result["currency"].to_list() == ["RUB", "RUB"]
    assert all(str(path) in source for source in result["source_id"])


def test_generic_cash_ledger_selects_only_explicit_margin_operations(tmp_path):
    path = workbook(tmp_path / "cash.xlsx", [
        ["Дата операции", "Инструмент", "Вид операции", "Сумма", "Валюта"],
        ["11.09.2026", "SBER", "Покупка", "-100000", "RUB"],
        ["11.09.2026", "SiU6", "Вариационная маржа", "15", "RUB"],
        ["11.09.2026", "", "Ввод ДС", "1000000", "RUB"],
    ])
    result = parse_finam_margins([path])
    assert result.height == 1
    assert result["ticker"].to_list() == ["SiU6"]
    assert result["gross_pnl"].to_list() == [Decimal("15")]


def test_empty_explicit_margin_table_is_valid_zero_activity(tmp_path):
    path = ledger(tmp_path / "empty.xlsx", [])
    result = parse_finam_margins([path])
    assert result.is_empty()
    assert result.schema == MARGIN_SCHEMA


def test_generic_cash_table_without_vm_does_not_prove_zero_vm(tmp_path):
    path = workbook(tmp_path / "cash.xlsx", [
        ["Дата", "Инструмент", "Операция", "Сумма", "Валюта"],
        ["11.09.2026", "SBER", "Покупка", "-1000", "RUB"],
    ])
    with pytest.raises(ValueError, match="не найдена явная таблица"):
        parse_finam_margins([path])


@pytest.mark.parametrize("column,value", [
    (0, "oops"), (1, ""), (2, "NaN"), (2, "inf"),
    (2, "100000000000000000000"), (3, ""), (3, "неизвестно"),
])
def test_bad_margin_rows_fail_instead_of_silently_disappearing(tmp_path, column, value):
    row = ["11.09.2026", "SiU6", "1", "RUB", "vm-1"]
    row[column] = value
    path = ledger(tmp_path / "bad.xlsx", [row])
    with pytest.raises(ValueError, match="bad.xlsx"):
        parse_finam_margins([path])


def test_missing_currency_and_ambiguous_margin_headers_are_rejected(tmp_path):
    missing = workbook(tmp_path / "missing.xlsx", [
        ["Дата", "Инструмент", "Вариационная маржа"],
        ["11.09.2026", "SiU6", "10"],
    ])
    with pytest.raises(ValueError, match="валюта"):
        parse_finam_margins([missing])
    ambiguous = workbook(tmp_path / "ambiguous.xlsx", [
        ["Дата", "Инструмент", "Вариационная маржа, руб.", "Сумма"],
        ["11.09.2026", "SiU6", "10", "10"],
    ])
    with pytest.raises(ValueError, match="Неоднозначные"):
        parse_finam_margins([ambiguous])


def test_no_identifier_dedup_preserves_maximum_intrareport_multiplicity(tmp_path):
    row = ["11.09.2026", "SiU6", "10", "RUB", ""]
    first = ledger(tmp_path / "one.xlsx", [row, row])
    second = ledger(tmp_path / "two.xlsx", [row, row, row])
    result = parse_finam_margins([first, second])
    assert result.height == 3
    assert result["gross_pnl"].sum() == Decimal("30")


def test_explicit_identifiers_dedup_overlap_but_keep_distinct_equal_cashflows(tmp_path):
    row = ["11.09.2026", "SiU6", "10", "RUB", "id-1"]
    other = [*row[:-1], "id-2"]
    first = ledger(tmp_path / "one.xlsx", [row])
    second = ledger(tmp_path / "two.xlsx", [row, other])
    assert parse_finam_margins([first, second])["source_id"].to_list() == ["id-1", "id-2"]
    conflict = ledger(tmp_path / "conflict.xlsx", [[*row[:2], "11", *row[3:]]])
    with pytest.raises(ValueError, match="Противоречивые данные"):
        parse_finam_margins([first, conflict])


def test_zip_reads_reports_without_extracting_archive_paths(tmp_path):
    source = ledger(tmp_path / "one.xlsx", [["11.09.2026", "SiU6", "10", "RUB", "vm-1"]])
    archive = tmp_path / "report.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("../../report.xlsx", source.read_bytes())
        output.writestr("signature.sig", b"signature")
    assert parse_finam_margins([archive])["gross_pnl"].to_list() == [Decimal("10")]
    assert not (tmp_path.parent / "report.xlsx").exists()


def test_unknown_report_keeps_source_and_reports_path(tmp_path):
    source = tmp_path / "report.xml"
    content = b"<broker_report><unified_account><spot_main_deals/></unified_account></broker_report>"
    source.write_bytes(content)
    with pytest.raises(ValueError, match="report.xml"):
        parse_finam_margins([source])
    assert source.read_bytes() == content


def test_daily_positions_reconcile_paid_and_change_in_pending_from_baseline(tmp_path):
    first = positions(tmp_path / "baseline.xls", "10.09.2026", [["RIU6", "0", "100", "1"]])
    second = positions(tmp_path / "second.xls", "11.09.2026", [["RIU6", "100", "30", "1"]])
    third = positions(tmp_path / "third.xls", "12.09.2026", [["RIU6", "30", "5", "0"]])
    result = parse_finam_margins([third, first, second])
    assert result["gross_pnl"].to_list() == [Decimal("100"), Decimal("30"), Decimal("5")]
    assert result["date"].to_list() == [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)]
    assert result.filter(pl.col("date") > date(2026, 9, 10))["gross_pnl"].sum() == Decimal("35")


def test_positions_take_precedence_over_overlapping_trade_margin_table(tmp_path):
    other = html_table([
        ["Дата", "Инструмент", "Вариационная маржа, руб."],
        ["11.09.2026", "RIU6", "999"],
    ])
    path = positions(tmp_path / "report.xls", "11.09.2026", [["RIU6", "10", "5", "1"]], other)
    assert parse_finam_margins([path])["gross_pnl"].to_list() == [Decimal("15")]


def test_position_report_day_must_match_request_and_be_single_day(tmp_path):
    path = positions(tmp_path / "report.xls", "11.09.2026", [["RIU6", "10", "5", "1"]])
    with pytest.raises(ValueError, match="противоречит"):
        parse_finam_margins([path], report_dates={path: date(2026, 9, 12)})
    text = path.read_bytes().decode("cp1251").replace("по 11.09.2026", "по 12.09.2026")
    path.write_bytes(text.encode("cp1251"))
    with pytest.raises(ValueError, match="однодневный"):
        parse_finam_margins([path])


def calendar_positions(path, first="11.09.2026", last="11.09.2026", count="1 день", extra=""):
    text = "<html><body>" + html_table([
        ["", f"На начало периода {first}", f"На конец периода {last}", f"Изменение за период ({count})"],
        ["ИТОГО оценка состояния счета (руб.)", "100", "115", "15"],
    ]) + html_table([
        ["Оценка позиции по Производным финансовым инструментам, руб."],
        ["Инструмент", "Вариационная маржа факт", "Вариационная маржа план"],
        ["RIU6", "10", "5"],
    ]) + extra + "</body></html>"
    path.write_text(text, encoding="utf-8")
    return path


def test_actual_calendar_column_headers_identify_single_day_without_period_sentence(tmp_path):
    extra = html_table([["Дата сделки", "Инструмент"], ["10.09.2026", "TEST"]])
    path = calendar_positions(tmp_path / "calendar.html", extra=extra)
    result = parse_finam_margins([path])
    assert result["date"].to_list() == [date(2026, 9, 11)]
    assert result["gross_pnl"].to_list() == [Decimal("15")]


@pytest.mark.parametrize("first,last,count", [
    ("10.09.2026", "11.09.2026", "2 дня"),
    ("10.09.2026", "11.09.2026", "1 день"),
    ("11.09.2026", "11.09.2026", "2дня"),
    ("11.09.2026", "11.09.2026", "12 дней"),
])
def test_actual_calendar_headers_reject_multi_day_report_despite_supplied_day(tmp_path, first, last, count):
    path = calendar_positions(tmp_path / "calendar.html", first, last, count)
    with pytest.raises(ValueError, match="однодневный"):
        parse_finam_margins([path], report_dates={path: date(2026, 9, 11)})


def test_actual_calendar_column_day_must_match_supplied_day(tmp_path):
    path = calendar_positions(tmp_path / "calendar.html", count="1день")
    with pytest.raises(ValueError, match="противоречит запрошенной"):
        parse_finam_margins([path], report_dates={path: date(2026, 9, 12)})


def test_actual_calendar_headers_must_agree_with_legacy_period_sentence(tmp_path):
    path = calendar_positions(
        tmp_path / "calendar.html", extra="<p>за период с 12.09.2026 по 12.09.2026</p>",
    )
    with pytest.raises(ValueError, match="противоречит запрошенной"):
        parse_finam_margins([path])


def test_repeated_consistent_calendar_headers_are_allowed(tmp_path):
    path = calendar_positions(tmp_path / "calendar.html", extra=html_table([
        ["Начало периода 11.09.2026", "Конец периода 11.09.2026"],
    ]))
    assert parse_finam_margins([path])["date"].to_list() == [date(2026, 9, 11)]


def test_inconsistent_calendar_columns_cannot_claim_single_day(tmp_path):
    path = calendar_positions(tmp_path / "calendar.html", extra=html_table([
        ["На начало периода 10.09.2026", "На конец периода 11.09.2026"],
    ]))
    with pytest.raises(ValueError, match="однодневный"):
        parse_finam_margins([path], report_dates={path: date(2026, 9, 11)})


def test_multi_day_count_alone_cannot_be_overridden_by_supplied_date(tmp_path):
    path = positions(
        tmp_path / "calendar.xls", "11.09.2026", [["RIU6", "10", "5", "1"]],
        html_table([["Изменение за период (2 дня)"]]),
    )
    with pytest.raises(ValueError, match="однодневный"):
        parse_finam_margins([path], report_dates={path: date(2026, 9, 11)})


def test_same_daily_snapshot_is_not_counted_twice(tmp_path):
    first = positions(tmp_path / "one.xls", "11.09.2026", [["RIU6", "10", "5", "1"]])
    second = positions(tmp_path / "two.xls", "11.09.2026", [["RIU6", "10", "5", "1"]])
    assert parse_finam_margins([first, second]).height == 1
    positions(second, "11.09.2026", [["RIU6", "11", "5", "1"]])
    with pytest.raises(ValueError, match="противоречивые"):
        parse_finam_margins([first, second])


def test_missing_previously_pending_contract_is_rejected(tmp_path):
    first = positions(tmp_path / "one.xls", "10.09.2026", [["RIU6", "0", "100", "1"]])
    second = positions(tmp_path / "two.xls", "11.09.2026", [])
    with pytest.raises(ValueError, match="ранее начисленной"):
        parse_finam_margins([first, second])


def test_supplied_date_can_identify_daily_xlsx_position_report(tmp_path):
    path = workbook(tmp_path / "report.xlsx", [
        ["Оценка позиции по Производным финансовым инструментам, руб."],
        ["Инструмент", "Вариационная маржа факт", "Вариационная маржа план"],
        ["RIU6", "10", "5"],
    ])
    result = parse_finam_margins([path], report_dates={path: date(2026, 9, 11)})
    assert result["date"].to_list() == [date(2026, 9, 11)]
    assert result["gross_pnl"].to_list() == [Decimal("15")]


def test_html_trailing_rowspan_preserves_currency_column_alignment(tmp_path):
    source = tmp_path / "report.xls"
    source.write_text(
        "<html><table><tr><td colspan='4'>Оценка позиции по Производным</td></tr>"
        "<tr><td rowspan='2'>Инструмент</td><td colspan='2'>ВМ</td><td rowspan='2'>Валюта</td></tr>"
        "<tr><td>факт</td><td>план</td></tr>"
        "<tr><td>RIU6</td><td>10</td><td>5</td><td>RUB</td></tr></table></html>",
        encoding="utf-8",
    )
    result = parse_finam_margins([source], report_dates={source: date(2026, 9, 11)})
    assert result["gross_pnl"].to_list() == [Decimal("15")]
    assert result["currency"].to_list() == ["RUB"]


def test_position_reconciliation_preserves_decimal_precision(tmp_path):
    source = workbook(tmp_path / "precise.xlsx", [
        ["Оценка позиции по Производным, руб."],
        ["Инструмент", "Вариационная маржа факт", "Вариационная маржа план"],
        ["RIU6", "1234567890123.123456789012345678", "0.000000000000000001"],
    ])
    result = parse_finam_margins([source], report_dates={source: date(2026, 9, 11)})
    assert result["gross_pnl"].to_list() == [Decimal("1234567890123.123456789012345679")]


def test_unrelated_currencyless_trade_table_does_not_block_vm_table(tmp_path):
    path = workbook(tmp_path / "report.xlsx", [
        ["Дата", "Инструмент", "Операция", "Сумма"],
        ["11.09.2026", "SBER", "Покупка", "100000"],
        ["Вариационная маржа, руб."],
        ["Дата", "Инструмент", "Сумма"],
        ["11.09.2026", "RIU6", "15"],
    ])
    assert parse_finam_margins([path])["gross_pnl"].to_list() == [Decimal("15")]


def test_nonzero_vm_total_without_details_is_not_mistaken_for_no_activity(tmp_path):
    path = ledger(tmp_path / "total.xlsx", [["Итого", "", "100", "RUB", ""]])
    with pytest.raises(ValueError, match="ненулевой итог"):
        parse_finam_margins([path])
    position_path = positions(tmp_path / "position-total.xls", "11.09.2026", [["Итого", "100", "5", ""]])
    with pytest.raises(ValueError, match="ненулевой итог"):
        parse_finam_margins([position_path])
