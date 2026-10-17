from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from trade_exporter.exporters.excel import write_workbook
from trade_exporter.services import historical_valuation as service


DAY = date(2025, 9, 25)
ACCOUNT = SimpleNamespace(id="12345")


def report(path, *, account="12345", first="25.09.2025", last="25.09.2025", total="110.00", go="5000.00", reorder=False, mismatch=False, malformed=None):
    components = sorted(service.COMPONENTS)
    values = {label: "0,00" for label in components}
    values["оценка рублевой позиции (руб.)"] = "20,00"
    values["оценка позиции по ценным бумагам и иностранным финансовым инструментам, не квалифицированным в качестве ценных бумаг (руб.)"] = "100,00"
    values["оценка требований и обязательств по оплате по незавершенным сделкам (руб.)"] = "-30,00"
    values["оценка плановых позиций по незавершенным сделкам (руб.)"] = "20,00"
    if mismatch:
        values[components[0]] = "1.00"
    if malformed is not None:
        values[components[0]] = malformed
    rows = [["", f"На начало периода {first}", f"На конец периода {last}", "Изменение за период (1 день)"]]
    rows.extend([[label, "90,00", value, "0,00"] for label, value in values.items()])
    rows.append([service.EXCLUDED_GO, "0,00", go, "0,00"])
    rows.append([service.TOTAL, "90,00", total, "20,00"])
    if reorder:
        rows = [[row[index] for index in (0, 2, 3, 1)] for row in rows]
    html = "<html><body><h1>Отчет брокера за период с " + first + " по " + last + "</h1>"
    html += "<table><tr><td>По счету:</td><td>КлФ-" + account + "</td></tr></table><table>"
    html += "".join("<tr>" + "".join("<td>" + escape(str(cell)) + "</td>" for cell in row) + "</tr>" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html + "</table></body></html>", encoding="utf-8")
    return path


@pytest.fixture
def broker():
    return SimpleNamespace(name="finam", settings={})


def downloader(monkeypatch, path):
    calls = []

    class Reports:
        _account_id = staticmethod(lambda value: str(int(value)))

        def __init__(self, broker, output_dir):
            calls.append(Path(output_dir))

        def download(self, account_id, start, end, *, chunk_days):
            calls.append((account_id, start, end, chunk_days))
            return [path]

    monkeypatch.setattr(service, "FinamReports", Reports)
    return calls


def test_historical_nav_uses_labelled_end_column_and_excludes_go(tmp_path, broker, monkeypatch):
    path = report(tmp_path / "input.html", reorder=True)
    calls = downloader(monkeypatch, path)
    result = service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    assert result.equity == Decimal("110")
    assert result.currency == "RUB"
    assert result.as_of == datetime(2025, 9, 25, 23, 59, 59, 999999, timezone.utc)
    assert result.source_file == path
    assert calls[1] == ("12345", datetime(2025, 9, 25, tzinfo=timezone.utc), datetime(2025, 9, 26, tzinfo=timezone.utc), 1)


def test_valid_cached_report_is_verified_and_reused(tmp_path, broker, monkeypatch):
    path = report(tmp_path / "input.xls")
    downloader(monkeypatch, path)
    first = service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    cache = tmp_path / "output" / "historical_valuation" / service.sha256(ACCOUNT.id.encode()).hexdigest()[:16] / DAY.isoformat()
    report(cache / "broker_reports" / "saved.xls")
    monkeypatch.setattr(service.FinamReports, "download", lambda *a, **k: pytest.fail("valid cache was not reused"))
    second = service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    assert second.equity == first.equity
    assert second.source_file.name == "saved.xls"


def test_wrong_account_cached_file_is_not_trusted(tmp_path, broker, monkeypatch):
    cache = tmp_path / "output" / "historical_valuation" / service.sha256(ACCOUNT.id.encode()).hexdigest()[:16] / DAY.isoformat()
    report(cache / "wrong.html", account="99999", total="999999")
    calls = downloader(monkeypatch, report(tmp_path / "correct.html"))
    result = service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    assert result.equity == 110
    assert len(calls) == 2


@pytest.mark.parametrize("options,match", [
    ({"account": "99999"}, "счёт"),
    ({"first": "24.09.2025"}, "выбранный день"),
    ({"last": "26.09.2025"}, "выбранный день"),
    ({"mismatch": True}, "не сходится"),
    ({"malformed": "NaN"}, "денежная сумма"),
    ({"malformed": "неизвестно"}, "денежная сумма"),
])
def test_report_identity_dates_reconciliation_and_money_are_strict(tmp_path, broker, monkeypatch, options, match):
    downloader(monkeypatch, report(tmp_path / "invalid.html", **options))
    with pytest.raises(ValueError, match=match):
        service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")


def test_ruble_rounding_residual_is_explicit(tmp_path, broker, monkeypatch):
    downloader(monkeypatch, report(tmp_path / "rounded.html", total="110.02"))
    result = service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    assert result.equity == Decimal("110.02")
    assert any("Округление" in value for value in result.quality)


@pytest.mark.parametrize("name,currency,day", [
    ("tbank", "RUB", DAY),
    ("finam", "USD", DAY),
    ("finam", "RUB", datetime.now(timezone.utc).date()),
])
def test_unsupported_broker_currency_or_current_day_does_not_download(tmp_path, monkeypatch, name, currency, day):
    monkeypatch.setattr(service.FinamReports, "download", lambda *a, **k: pytest.fail("unsupported request downloaded a report"))
    with pytest.raises(ValueError):
        service.load_historical_valuation(SimpleNamespace(name=name, settings={"base_currency": currency}), ACCOUNT, day, tmp_path)


def test_missing_component_and_duplicate_total_are_rejected(tmp_path, broker, monkeypatch):
    path = report(tmp_path / "input.html")
    original = path.read_text(encoding="utf-8")
    label = sorted(service.COMPONENTS)[0]
    start = original.index("<tr><td>" + label)
    stop = original.index("</tr>", start) + len("</tr>")
    path.write_text(original[:start] + original[stop:], encoding="utf-8")
    downloader(monkeypatch, path)
    with pytest.raises(ValueError, match="полного состава"):
        service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")
    duplicate = "<tr><td>" + service.TOTAL + "</td><td>90</td><td>110</td><td>20</td></tr>"
    path.write_text(original.replace("</table></body>", duplicate + "</table></body>"), encoding="utf-8")
    with pytest.raises(ValueError, match="неоднозначный итог"):
        service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output")


def test_native_xlsx_preserves_account_and_end_value_validation(tmp_path, broker, monkeypatch):
    html = report(tmp_path / "input.html")
    tables = {name: rows for name, rows in service._tables(html.read_bytes(), html.name) if name != "document"}
    path = tmp_path / "native.xlsx"
    write_workbook(path, {
        name: pl.DataFrame(rows, schema=[f"column_{index}" for index in range(len(rows[0]))], orient="row")
        for name, rows in tables.items()
    })
    downloader(monkeypatch, path)
    assert service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output").equity == 110


def test_moscow_previous_day_is_accepted_during_utc_date_rollover(tmp_path, broker, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 9, 25, 22, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(service, "datetime", Clock)
    downloader(monkeypatch, report(tmp_path / "input.html"))
    assert service.load_historical_valuation(broker, ACCOUNT, DAY, tmp_path / "output").equity == 110
