from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest

from trade_exporter.brokers import finam_reports
from trade_exporter.brokers.finam_reports import FinamReports


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)
URL = "https://api.finam.ru/v1/report/r1/download"


def response(content=b"%PDF-1.7\nreport", status=200, headers=None):
    return SimpleNamespace(status_code=status, content=content, headers=headers or {})


def archive_content(xlsx=False):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        if xlsx:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("xl/workbook.xml", "workbook")
        else:
            archive.writestr("report.xml", "<report />")
    return buffer.getvalue()


@pytest.fixture
def broker(monkeypatch):
    state = SimpleNamespace(
        requests=[], downloads=[], responses=[], statuses=["SUCCESS"], urls=[URL],
        created=0, current_report=None, refreshed=0, now=0,
    )

    class Client:
        def get(self, url, **options):
            state.downloads.append((url, options))
            return state.responses.pop(0) if state.responses else response()

    value = SimpleNamespace(settings={}, _jwt="jwt-1", _secret="secret", timeout=30, session=Client())

    def request(method, path, **options):
        state.requests.append((method, path, options))
        if method == "POST" and path == "report":
            state.created += 1
            state.current_report = f"r{state.created}"
            return {"report_id": state.current_report}
        if method == "GET" and path.endswith("/info"):
            status = state.statuses.pop(0) if len(state.statuses) > 1 else state.statuses[0]
            url = state.urls.pop(0) if len(state.urls) > 1 else state.urls[0]
            return {"info": {"report_id": state.current_report, "status": status, "account_id": "123", "url": url}}
        raise AssertionError((method, path, options))

    def authenticate():
        state.refreshed += 1
        value._jwt = "jwt-2"

    def sleep(seconds):
        state.now += seconds

    value._request = request
    value._authenticate = authenticate
    monkeypatch.setattr(finam_reports.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(finam_reports.time, "sleep", sleep)
    return value, state


def test_full_report_is_created_downloaded_and_saved_unchanged(broker, tmp_path):
    value, state = broker
    content = b"%PDF-1.7\noriginal bytes"
    state.responses = [response(content, headers={"Content-Disposition": 'attachment; filename="report.pdf"'})]
    paths = FinamReports(value, tmp_path).download("123", START, END)
    assert len(paths) == 1
    assert paths[0].parent == tmp_path / "broker_reports"
    assert paths[0].suffix == ".pdf"
    assert paths[0].read_bytes() == content
    assert state.requests[0] == ("POST", "report", {"json": {
        "date_range": {"date_begin": "2026-01-01T00:00:00Z", "date_end": "2026-01-02T00:00:00Z"},
        "report_form": "REPORT_FORM_LONG", "account_id": "123",
    }})
    assert state.downloads == [(URL, {
        "headers": {"Authorization": "Bearer jwt-1"}, "timeout": 30, "follow_redirects": False,
    })]


def test_period_is_split_into_contiguous_chunks_of_at_most_92_days(broker, tmp_path):
    value, state = broker
    end = START + timedelta(days=185)
    paths = FinamReports(value, tmp_path).download("123", START, end)
    requests = [call[2]["json"]["date_range"] for call in state.requests if call[0] == "POST"]
    assert len(paths) == len(requests) == 3
    boundaries = []
    for request in requests:
        begin = datetime.fromisoformat(request["date_begin"].replace("Z", "+00:00"))
        stop = datetime.fromisoformat(request["date_end"].replace("Z", "+00:00"))
        assert stop - begin <= timedelta(days=92)
        boundaries.append((begin, stop))
    assert boundaries[0][0] == START
    assert boundaries[-1][1] == end
    assert boundaries[0][1] == boundaries[1][0]
    assert boundaries[1][1] == boundaries[2][0]


def test_pending_and_in_progress_are_polled_until_success(broker, tmp_path):
    value, state = broker
    state.statuses = ["PENDING", "IN_PROGRESS", "SUCCESS"]
    assert len(FinamReports(value, tmp_path).download("123", START, END)) == 1
    assert state.now == 4
    assert len([call for call in state.requests if call[0] == "GET"]) == 3


@pytest.mark.parametrize("status", [0, "NOT_FOUND", 4, "ERROR", "CREATED"])
def test_terminal_or_unknown_status_fails_without_download(broker, tmp_path, status):
    value, state = broker
    state.statuses = [status]
    with pytest.raises(RuntimeError):
        FinamReports(value, tmp_path).download("123", START, END)
    assert state.downloads == []
    assert not (tmp_path / "broker_reports").exists()


def test_numeric_success_status_and_wrapped_url_are_supported(broker, tmp_path):
    value, state = broker
    state.statuses = [3]
    state.urls = [{"value": URL}]
    assert len(FinamReports(value, tmp_path).download("123", START, END)) == 1


def test_report_polling_has_a_finite_deadline(broker, tmp_path):
    value, state = broker
    value.settings = {"report_timeout_seconds": 3, "report_poll_seconds": 2}
    state.statuses = ["PENDING"]
    with pytest.raises(RuntimeError, match="время ожидания"):
        FinamReports(value, tmp_path).download("123", START, END)
    assert state.now == 3
    assert len([call for call in state.requests if call[0] == "GET"]) == 2
    assert state.downloads == []


@pytest.mark.parametrize("url", [
    "http://api.finam.ru/report",
    "https://evil.example/report",
    "https://finam.ru.evil.example/report",
    "https://evilfinam.ru/report",
    "https://finam.ru@evil.example/report",
    "https://user:password@api.finam.ru/report",
    "https://api.finam.ru:444/report",
    "https://api.finam.ru/report#fragment",
    "https://api.finam.ru/\nreport",
])
def test_untrusted_download_urls_are_rejected_before_sending_credentials(broker, tmp_path, url):
    value, state = broker
    state.urls = [url]
    with pytest.raises(RuntimeError):
        FinamReports(value, tmp_path).download("123", START, END)
    assert state.downloads == []


def test_each_redirect_is_validated_before_credentials_are_sent(broker, tmp_path):
    value, state = broker
    state.responses = [response(status=302, headers={"Location": "https://evil.example/stolen"})]
    with pytest.raises(RuntimeError, match="только по HTTPS"):
        FinamReports(value, tmp_path).download("123", START, END)
    assert len(state.downloads) == 1
    assert state.downloads[0][0] == URL


def test_trusted_absolute_and_relative_redirects_keep_authentication(broker, tmp_path):
    value, state = broker
    state.responses = [
        response(status=302, headers={"Location": "https://reports.finam.ru/start"}),
        response(status=307, headers={"Location": "/final"}),
        response(),
    ]
    assert len(FinamReports(value, tmp_path).download("123", START, END)) == 1
    assert [item[0] for item in state.downloads] == [URL, "https://reports.finam.ru/start", "https://reports.finam.ru/final"]
    assert all(item[1]["headers"] == {"Authorization": "Bearer jwt-1"} for item in state.downloads)
    assert all(item[1]["follow_redirects"] is False for item in state.downloads)


def test_redirect_loop_is_bounded(broker, tmp_path):
    value, state = broker
    state.responses = [response(status=302, headers={"Location": URL}) for _ in range(6)]
    with pytest.raises(RuntimeError, match="слишком много перенаправлений"):
        FinamReports(value, tmp_path).download("123", START, END)
    assert len(state.downloads) == 6


def test_download_refreshes_expired_jwt_once(broker, tmp_path):
    value, state = broker
    state.responses = [response(status=401), response()]
    assert len(FinamReports(value, tmp_path).download("123", START, END)) == 1
    assert state.refreshed == 1
    assert state.downloads[1][1]["headers"] == {"Authorization": "Bearer jwt-2"}


def test_repeated_download_unauthorized_error_does_not_leak_url_or_credentials(broker, tmp_path):
    value, state = broker
    state.urls = [URL + "?secret=private-token"]
    state.responses = [response(status=401), response(status=401)]
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        FinamReports(value, tmp_path).download("123", START, END)
    assert "private-token" not in str(error.value)
    assert "jwt" not in str(error.value)
    assert state.refreshed == 1


def test_jwt_only_download_does_not_try_secret_refresh(broker, tmp_path):
    value, state = broker
    value._secret = None
    state.responses = [response(status=401)]
    with pytest.raises(RuntimeError, match="HTTP 401"):
        FinamReports(value, tmp_path).download("123", START, END)
    assert state.refreshed == 0


@pytest.mark.parametrize(
    ("content", "headers", "extension"),
    [
        (archive_content(True), {"content-disposition": 'attachment; filename="wrong.pdf"'}, ".xlsx"),
        (archive_content(), {}, ".zip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old excel", {}, ".xls"),
        (b"<?xml version='1.0'?><report />", {}, ".xml"),
        (b"<!DOCTYPE html><html>report</html>", {}, ".html"),
        (b'{"report":1}', {}, ".json"),
        (b"date;pnl\n2026;1", {"Content-Type": "text/csv"}, ".csv"),
        (b"date\tpnl\n2026\t1", {"Content-Disposition": 'attachment; filename="../../report.tsv"'}, ".tsv"),
        (b"unrecognized original content", {}, ".bin"),
    ],
)
def test_type_is_detected_and_raw_original_is_preserved(broker, tmp_path, content, headers, extension):
    value, state = broker
    state.responses = [response(content, headers=headers)]
    saved = FinamReports(value, tmp_path).download("123", START, END)[0]
    assert saved.suffix == extension
    assert saved.parent == tmp_path / "broker_reports"
    assert saved.read_bytes() == content


def test_repeat_downloads_create_distinct_files_without_overwriting(broker, tmp_path):
    value, state = broker
    reports = FinamReports(value, tmp_path)
    first = reports.download("123", START, END)[0]
    state.responses = [response(b"%PDF-second")]
    second = reports.download("123", START, END)[0]
    assert first != second
    assert first.read_bytes() == b"%PDF-1.7\nreport"
    assert second.read_bytes() == b"%PDF-second"


def test_empty_download_is_not_persisted(broker, tmp_path):
    value, state = broker
    state.responses = [response(b"")]
    with pytest.raises(RuntimeError, match="пустой брокерский отчёт"):
        FinamReports(value, tmp_path).download("123", START, END)
    assert not (tmp_path / "broker_reports").exists()


@pytest.mark.parametrize("account_id", ["TRQD05:123", "", "-1", "0", "1.5", "9223372036854775808", "../../123"])
def test_reports_numeric_account_id_is_validated_before_network_access(broker, tmp_path, account_id):
    value, state = broker
    with pytest.raises(ValueError, match="числовой account_id"):
        FinamReports(value, tmp_path).download(account_id, START, END)
    assert state.requests == []


def test_interval_validation_and_empty_period(broker, tmp_path):
    value, state = broker
    reports = FinamReports(value, tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        reports.download("123", datetime(2026, 1, 1), END)
    with pytest.raises(ValueError, match="Начало периода"):
        reports.download("123", END, START)
    assert reports.download("123", START, START) == []
    assert state.requests == []


def test_daily_reports_follow_local_calendar_days_and_do_not_include_next_day(broker, tmp_path):
    value, state = broker
    reports = FinamReports(value, tmp_path)
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 3, 29, tzinfo=zone)
    end = datetime(2026, 3, 31, tzinfo=zone)
    paths = reports.download("123", start, end, chunk_days=1)
    ranges = [options["json"]["date_range"] for method, path, options in state.requests if method == "POST"]
    assert ranges == [
        {"date_begin": "2026-03-28T23:00:00Z", "date_end": "2026-03-29T21:59:59.999999Z"},
        {"date_begin": "2026-03-29T22:00:00Z", "date_end": "2026-03-30T21:59:59.999999Z"},
    ]
    assert [reports.report_dates[path].isoformat() for path in paths] == ["2026-03-29", "2026-03-30"]
