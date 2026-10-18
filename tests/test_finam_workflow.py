from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import orjson
import polars as pl
import pytest

from trade_exporter import cli
from trade_exporter.brokers import finam


def trade(trade_id, symbol, side, price, quantity, fee, timestamp, currency="RUB"):
    return {
        "trade_id": trade_id, "account_id": "123", "symbol": symbol,
        "side": side, "price": {"value": price}, "size": {"value": quantity},
        "commission": {"value": {"value": fee}, "currency": "RUB"},
        "currency": currency, "timestamp": timestamp,
    }


def statement(day, paid, pending):
    formatted = day.strftime("%d.%m.%Y")
    return (
        "<!doctype html><html><body>"
        f"<p>Справка о состоянии обязательств за период с {formatted} по {formatted}</p>"
        "<table><tr><td colspan='3'>Оценка позиции по Производным, руб.</td></tr>"
        "<tr><td rowspan='2'>Инструмент</td><td colspan='2'>Вариационная маржа</td></tr>"
        "<tr><td>факт</td><td>план</td></tr>"
        f"<tr><td>SiZ6</td><td>{paid}</td><td>{pending}</td></tr>"
        "</table></body></html>"
    ).encode("utf-8")


def test_token_to_broker_report_to_whole_portfolio_results(tmp_path, monkeypatch, capsys):
    calls = []
    generated = {}
    token = "synthetic-finam-secret"
    jwt = "synthetic-session-token"
    raw = [
        trade("b1", "SBER@MISX", "SIDE_BUY", "100", "2", "2", "2026-09-10T09:00:00Z"),
        trade("b2", "SiZ6@RTSX", "SIDE_BUY", "99999", "1", "4", "2026-09-10T10:00:00Z", "USD"),
        trade("s1", "SBER@MISX", "SIDE_SELL", "110", "1", "1", "2026-09-11T09:00:00Z"),
        trade("s2", "SiZ6@RTSX", "SIDE_SELL", "125001", "1", "3", "2026-09-11T10:00:00Z", "USD"),
    ]

    def response(data=None, content=b"", headers=None):
        return SimpleNamespace(status_code=200, text="", content=content, headers=headers or {}, json=lambda: data)

    class Client:
        def __init__(self, **options):
            assert options["verify"] is False

        def post(self, url, **options):
            path = urlsplit(url).path
            calls.append(("POST", path, options))
            if path == "/v1/sessions":
                assert options["json"] == {"secret": token}
                return response({"token": jwt})
            if path == "/v1/sessions/details":
                assert "Authorization" not in options.get("headers", {})
                return response({"account_ids": ["123"]})
            if path == "/v1/report":
                assert options["headers"]["Authorization"] == f"Bearer {jwt}"
                assert options["json"]["report_form"] == "REPORT_FORM_LONG"
                report_id = f"report-{len(generated) + 1}"
                generated[report_id] = datetime.fromisoformat(
                    options["json"]["date_range"]["date_begin"].replace("Z", "+00:00")
                ).date()
                return response({"report_id": report_id})
            pytest.fail(f"Unexpected request: {path}")

        def get(self, url, **options):
            path = unquote(urlsplit(url).path)
            calls.append(("GET", path, options))
            if path == "/v1/accounts/123":
                return response({
                    "account_id": "123", "type": "MC", "status": "ACTIVE",
                    "open_account_date": "2026-09-01T00:00:00Z",
                    "equity": {"value": "5165"},
                    "cash": [{"currency_code": "RUB", "units": "5000", "nanos": 0}],
                    "positions": [
                        {"symbol": "SiZ6@RTSX", "quantity": {"value": "1"}, "current_price": {"value": "125001"}, "unrealized_pnl": {"value": "55"}},
                        {"symbol": "SBER@MISX", "quantity": {"value": "1"}, "current_price": {"value": "110"}, "average_price": {"value": "100"}, "current_price_currency": "RUB", "average_price_currency": "RUB", "unrealized_pnl": {"value": "10"}},
                    ],
                })
            if path == "/v1/accounts/123/transactions":
                return response({"transactions": []})
            if path == "/v1/accounts/123/orders":
                return response({"orders": []})
            if path == "/v1/accounts/123/trades":
                return response({"trades": raw})
            if path == "/v1/assets/SBER@MISX":
                return response({"type": "SHARES", "ticker": "SBER", "mic": "MISX"})
            if path == "/v1/assets/SiZ6@RTSX":
                return response({
                    "type": "FUTURES", "ticker": "SiZ6", "mic": "RTSX",
                    "future_details": {"contract_size": {"value": "1000"}},
                })
            if path.startswith("/v1/report/") and path.endswith("/info"):
                report_id = path.split("/")[3]
                return response({"info": {
                    "report_id": report_id, "account_id": "123", "status": "SUCCESS",
                    "url": f"https://api.finam.ru/download/{report_id}",
                }})
            if path.startswith("/download/"):
                report_id = path.rsplit("/", 1)[1]
                day = generated[report_id]
                paid, pending = ("10", "40") if day.day == 10 else ("500", "55")
                assert options["headers"]["Authorization"] == f"Bearer {jwt}"
                assert options["follow_redirects"] is False
                return response(content=statement(day, paid, pending), headers={"content-type": "text/html"})
            pytest.fail(f"Unexpected request: {path}")

    monkeypatch.setattr(finam.primp, "Client", Client)
    monkeypatch.setattr(finam.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("F.INVEST_TOKEN", token)
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    path = cli.main([
        "--config", str(config), "--broker", "finam", "--account-id", "123",
        "--from", "11.09.26", "--to", "11.09.26", "--output-dir", str(tmp_path),
    ])
    assert path.exists()
    assert len(generated) == 2
    assert set(generated.values()) == {
        datetime(2026, 9, 10, tzinfo=timezone.utc).date(),
        datetime(2026, 9, 11, tzinfo=timezone.utc).date(),
    }
    payload = orjson.loads(path.with_suffix(".json").read_bytes())
    sources = payload["details"]["source_reports"]
    assert len(sources) == 2
    assert all(source["content"].startswith("<!doctype html>") for source in sources)
    assert not list(tmp_path.rglob("*.html"))
    assert list(tmp_path.glob("*.xlsx")) == [path]
    assert list(tmp_path.glob("*.json")) == [path.with_suffix(".json")]
    workbook = pl.read_excel(path, sheet_id=0)
    assert list(workbook) == ["Отчёт"]
    assert len(payload["details"]["raw_executions"]) == 2
    assert Decimal(payload["details"]["closed_records"][0]["net_pnl"]) == 8
    assert Decimal(payload["summary"]["gross_pnl"]) == 525
    assert Decimal(payload["summary"]["commission"]) == 5
    assert Decimal(payload["summary"]["net_pnl"]) == 520
    future = next(row for row in payload["results"] if row["ticker"] == "SiZ6@RTSX")
    assert future["entry_price"] is None and future["exit_price"] is None
    assert Decimal(future["gross_pnl"]) == 515
    assert "including_open_positions" in payload["futures_basis"]
    portfolio = payload["portfolio"]
    assert Decimal(portfolio["summary"]["equity"]) == 5165
    assert len(portfolio["positions"]) == 2
    assert any(row["instrument_id"] == "SBER@MISX" for row in portfolio["positions"])
    requests_before_offline = len(calls)
    offline = cli.main(["--analyze", str(path)])
    offline_payload = orjson.loads(offline[1].read_bytes())
    assert Decimal(offline_payload["summary"]["net_pnl"]) == 520
    assert len(calls) == requests_before_offline
    stdout = capsys.readouterr().out
    assert token not in stdout and jwt not in stdout
    assert "Excel:" in stdout
