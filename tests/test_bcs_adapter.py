from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from trade_exporter.brokers.bcs import BcsBroker
from trade_exporter.domain.models import Side


def utc(year, month, day):
    return datetime(year, month, day, tzinfo=timezone.utc)


def response(payload, status=200):
    return SimpleNamespace(status_code=status, content=json.dumps(payload).encode())


def position(ticker, kind, quantity, value):
    return {
        "agreementId": "000123", "board": "TQBR", "ticker": ticker,
        "instrumentType": kind, "currency": "RUB",
        "quantity": quantity, "currentValueRub": value,
    }


def trade(number, timestamp, side):
    return {
        "tradeNum": number, "tradeDateTime": timestamp, "side": side,
        "ticker": "SBER", "classCode": "TQBR", "instrumentType": "STOCK",
        "settlementCurrency": "RUB", "price": 123.45, "tradeQuantity": 10, "volume": 1234.5,
    }


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("B.INVEST_TOKEN", "synthetic-refresh-token")
    monkeypatch.setattr("trade_exporter.brokers.bcs.sleep", lambda delay: None)
    return BcsBroker({})


def test_token_scopes_account_and_portfolio(broker):
    class Session:
        def post(self, url, **options):
            assert url.endswith(broker.AUTH)
            assert parse_qs(options["data"].decode()) == {
                "client_id": ["trade-api-read"], "grant_type": ["refresh_token"],
                "refresh_token": ["synthetic-refresh-token"],
            }
            return response({"access_token": "synthetic-access", "expires_in": 3600})

        def get(self, url, **options):
            assert url.endswith(broker.PORTFOLIO)
            assert options["headers"] == {"Authorization": "Bearer synthetic-access"}
            return response({"positions": [position("RUB", "CASH", 100, 100), position("SBER", "STOCK", 10, 1234.5)]})

    broker.session = Session()
    account, = broker.list_accounts()
    snapshot = broker.get_portfolio_snapshot(account.id)
    assert account.id == "000123"
    assert snapshot.equity == Decimal("1334.5")
    assert snapshot.cash[0].amount == 100
    assert snapshot.positions[0].quantity == 10
    with pytest.raises(ValueError, match="другому счёту"):
        broker.get_portfolio_snapshot("other-account")


def test_paginated_trades_are_sorted_and_end_is_exclusive(broker, monkeypatch):
    def api(method, path, **options):
        if method == "get":
            return {"positions": [position("RUB", "CASH", 100, 100)]}
        page = parse_qs(urlsplit(path).query)["page"][0]
        assert options["json"] == {
            "startDateTime": "2026-01-25T21:00:00Z", "endDateTime": "2026-02-04T00:00:00Z",
        }
        rows = {
            "0": [trade(3, "2026-02-04T00:00:00Z", "2"), trade(2, "2026-02-03T10:00:00Z", "2")],
            "1": [trade(1, "2026-02-02T10:00:00Z", "1")],
        }[page]
        return {"records": rows, "totalPages": 2, "totalRecords": 3}

    monkeypatch.setattr(broker, "_api", api)
    fills = broker.get_fills("000123", utc(2025, 1, 1), utc(2026, 2, 4))
    assert [fill.side for fill in fills] == [Side.BUY, Side.SELL]
    assert all(fill.quantity == 10 and fill.pnl_price == Decimal("123.45") for fill in fills)
    assert "26.01.2026" in broker.fill_quality[0]
    assert broker.commission_quality


def test_empty_portfolio_uses_configured_agreement(broker, monkeypatch):
    broker.configured_account = "000123"
    monkeypatch.setattr(broker, "_api", lambda *args, **kwargs: {"positions": []})
    assert broker.list_accounts()[0].id == "000123"
    assert broker.get_portfolio_snapshot("000123").equity == 0
