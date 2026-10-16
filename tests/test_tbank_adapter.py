from datetime import datetime, timezone
from decimal import Decimal
import os
from types import SimpleNamespace

import pytest

from trade_exporter.brokers import tbank
from trade_exporter.brokers.tbank import TBankBroker
from trade_exporter.domain.models import Side

UTC = timezone.utc


class FakeTBank(TBankBroker):
    def __init__(self, pages, margin=None):
        os.environ["TEST_TBANK_TOKEN"] = "token"
        super().__init__({
            "token_env": "TEST_TBANK_TOKEN",
            "normalize_futures_to_points": True,
        })
        self.pages = list(pages)
        self.margin = margin or {
            "minPriceIncrement": {"units": "1", "nano": 0},
            "minPriceIncrementAmount": {"units": "10", "nano": 0},
        }
        self.operation_payloads = []

    def _post(self, method, payload):
        if method == self.OPERATIONS_BY_CURSOR:
            self.operation_payloads.append(payload)
            return self.pages.pop(0)
        if method == self.FUTURES_MARGIN:
            return self.margin
        if method == self.USERS_GET_ACCOUNTS:
            return {"accounts": []}
        raise AssertionError(method)


def op(op_type, price_units, qty, commission_units, date="2026-09-11T10:00:00Z"):
    return {
        "type": op_type,
        "date": date,
        "instrumentUid": "future-1",
        "ticker": "IMOEXF",
        "instrumentType": "futures",
        "quantityDone": str(qty),
        "price": {"currency": "rub", "units": str(price_units), "nano": 0},
        "commission": {"currency": "rub", "units": str(commission_units), "nano": 0},
        "tradesInfo": {
            "trades": [{
                "date": date,
                "quantity": str(qty),
                "price": {"currency": "rub", "units": str(price_units), "nano": 0},
            }]
        },
    }


def test_adapter_maps_operation_to_normalized_fill_and_future_points():
    broker = FakeTBank([{
        "items": [op("OPERATION_TYPE_BUY", 2300, 2, 4)],
        "hasNext": False,
    }])
    fills = broker.get_fills(
        "acc", datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC)
    )
    fill = fills[0]
    assert fill.side == Side.BUY
    assert fill.quantity == 2
    assert fill.pnl_price == Decimal("23000")
    assert fill.display_price == Decimal("2300")
    assert fill.commission == Decimal("4")


def test_cursor_pagination_is_followed():
    broker = FakeTBank([
        {"items": [], "hasNext": True, "nextCursor": "abc"},
        {"items": [], "hasNext": False},
    ])
    broker.get_fills(
        "acc", datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC)
    )
    assert len(broker.operation_payloads) == 2
    assert "cursor" not in broker.operation_payloads[0]
    assert broker.operation_payloads[1]["cursor"] == "abc"
    assert broker.operation_payloads[0]["limit"] == 1000


@pytest.fixture
def http_broker(monkeypatch):
    state = SimpleNamespace(options=None, calls=[], response=None)

    class Client:
        def __init__(self, **options):
            state.options = options

        def post(self, url, **options):
            state.calls.append((url, options))
            return state.response

    monkeypatch.setenv("TEST_HTTP_TOKEN", "  test-token  ")
    monkeypatch.setattr(tbank.primp, "Client", Client)
    broker = TBankBroker({
        "token_env": "TEST_HTTP_TOKEN",
        "base_url": "https://example.invalid/rest/",
        "request_timeout_seconds": 17,
    })
    return broker, state


def test_http_client_preserves_auth_timeout_and_certificate_setting(http_broker):
    _, state = http_broker
    assert state.options == {
        "headers": {
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
        "timeout": 17,
        "verify": False,
    }


@pytest.mark.parametrize("status_code", [200, 201, 302])
def test_post_sends_json_to_endpoint_and_returns_decoded_response(
    http_broker, status_code
):
    broker, state = http_broker
    decoded = {"accounts": [{"id": "account-1"}]}
    state.response = SimpleNamespace(
        status_code=status_code, text="response", json=lambda: decoded
    )
    payload = {"status": "ACCOUNT_STATUS_OPEN"}

    assert broker._post(broker.USERS_GET_ACCOUNTS, payload) is decoded
    assert state.calls == [(
        f"https://example.invalid/rest/{broker.USERS_GET_ACCOUNTS}",
        {"json": payload, "timeout": 17},
    )]


@pytest.mark.parametrize("status_code", [400, 401, 429, 503])
def test_post_reports_http_status_and_api_error_without_decoding(
    http_broker, status_code
):
    broker, state = http_broker

    def reject_json():
        raise AssertionError("error response must not be decoded")

    state.response = SimpleNamespace(
        status_code=status_code, text="api error", json=reject_json
    )
    with pytest.raises(RuntimeError, match=f"T-Bank API {status_code}: api error"):
        broker._post(broker.USERS_GET_ACCOUNTS, {})


def test_post_preserves_invalid_json_error(http_broker):
    broker, state = http_broker

    def invalid_json():
        raise ValueError("invalid json")

    state.response = SimpleNamespace(
        status_code=200, text="invalid", json=invalid_json
    )
    with pytest.raises(ValueError, match="invalid json"):
        broker._post(broker.USERS_GET_ACCOUNTS, {})
