from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_exporter.brokers import finam
from trade_exporter.brokers.finam import FinamBroker
from trade_exporter.domain.models import Side


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


def response(data=None, status=200, text="response"):
    return SimpleNamespace(status_code=status, text=text, json=lambda: data)


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(finam.time, "sleep", lambda seconds: None)
    state = SimpleNamespace(options=None, calls=[], responses=[])

    class Client:
        def __init__(self, **options):
            state.options = options

        def _call(self, method, url, options):
            state.calls.append((method, url, options))
            if not state.responses:
                raise AssertionError(f"Unexpected HTTP call: {method} {url}")
            return state.responses.pop(0)

        def post(self, url, **options):
            return self._call("POST", url, options)

        def get(self, url, **options):
            return self._call("GET", url, options)

    monkeypatch.setattr(finam.primp, "Client", Client)
    monkeypatch.delenv("FINAM_API_TOKEN", raising=False)
    monkeypatch.delenv("FINAM_TOKEN", raising=False)
    return state


def make_trade(trade_id="1", symbol="SBER@MISX", timestamp="2026-01-01T12:00:00Z", **overrides):
    item = {
        "trade_id": trade_id,
        "symbol": symbol,
        "price": {"value": "250.125"},
        "size": {"value": "10"},
        "side": "SIDE_BUY",
        "timestamp": timestamp,
        "order_id": "order-1",
        "account_id": "acc",
        "currency": "RUB",
        "commission": {"value": {"value": "1.25"}, "currency": "RUB"},
    }
    item.update(overrides)
    return item


def history_broker(monkeypatch, trades=None, asset=None, settings=None):
    broker = FinamBroker({"token": "secret", **(settings or {})})
    items = trades or []
    asset = asset or {"type": "SHARES", "ticker": "SBER", "mic": "MISX", "lot_size": {"value": "10"}}
    calls = []

    def request(method, path, *, params=None, token_details=False):
        calls.append((method, path, params))
        if path.endswith("/trades"):
            start = broker._timestamp(params["interval.start_time"])
            end = broker._timestamp(params["interval.end_time"])
            selected = [item for item in items if start <= broker._timestamp(item["timestamp"]) < end]
            return {"trades": selected[:params["limit"]]}
        if path.startswith("assets/"):
            return deepcopy(asset)
        raise AssertionError(path)

    monkeypatch.setattr(broker, "_request", request)
    return broker, calls


def test_constructor_is_network_free_and_uses_direct_token_before_environment(http, monkeypatch):
    monkeypatch.setenv("FINAM_API_TOKEN", "environment")
    broker = FinamBroker({"token": "  direct  ", "request_timeout_seconds": 17})
    assert broker.token == "direct"
    assert http.calls == []
    assert http.options == {
        "headers": {"Content-Type": "application/json"},
        "timeout": 17,
        "verify": False,
    }
    assert finam.os.environ["FINAM_API_TOKEN"] == "environment"


def test_default_environment_fallback_and_custom_environment_are_supported(http, monkeypatch):
    monkeypatch.setenv("FINAM_TOKEN", " fallback ")
    assert FinamBroker({}).token == "fallback"
    monkeypatch.setenv("CUSTOM_FINAM", " custom ")
    assert FinamBroker({"token_env": "CUSTOM_FINAM"}).token == "custom"
    with pytest.raises(RuntimeError, match="MISSING_FINAM"):
        FinamBroker({"token_env": "MISSING_FINAM"})


def test_api_secret_exchange_and_account_metadata(http):
    broker = FinamBroker({"token": "api-secret"})
    http.responses = [
        response({"token": "jwt-1"}),
        response({"account_ids": ["acc", "acc"], "expires_at": "2099-01-01T00:00:00Z"}),
        response({"account_id": "acc", "type": "MC", "status": "ACTIVE", "open_account_date": "2020-01-02T00:00:00Z"}),
    ]
    accounts = broker.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].id == "acc"
    assert accounts[0].broker == "finam"
    assert accounts[0].account_type == "MC"
    assert accounts[0].opened_at == datetime(2020, 1, 2, tzinfo=UTC)
    assert http.calls[0] == ("POST", "https://api.finam.ru/v1/sessions", {"json": {"secret": "api-secret"}, "timeout": 30})
    assert http.calls[1][2]["json"] == {"token": "jwt-1"}
    assert "headers" not in http.calls[1][2]
    assert http.calls[2][2]["headers"] == {"Authorization": "Bearer jwt-1"}


def test_jwt_is_reused_without_secret_exchange(http):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature"
    broker = FinamBroker({"token": jwt})
    http.responses = [response({"account_ids": []})]
    assert broker.list_accounts() == []
    assert len(http.calls) == 1
    assert http.calls[0][1].endswith("/sessions/details")
    assert "headers" not in http.calls[0][2]
    assert http.calls[0][2]["json"] == {"token": jwt}


def test_unauthorized_request_refreshes_once_and_replaces_token_details_body(http):
    broker = FinamBroker({"token": "secret"})
    http.responses = [
        response({"token": "old-jwt"}),
        response(status=401, text="expired"),
        response({"token": "new-jwt"}),
        response({"account_ids": []}),
    ]
    assert broker.list_accounts() == []
    assert http.calls[1][2]["json"] == {"token": "old-jwt"}
    assert http.calls[3][2]["json"] == {"token": "new-jwt"}
    assert "headers" not in http.calls[3][2]


def test_expiring_cached_token_is_refreshed_before_request(http):
    broker = FinamBroker({"token": "secret"})
    broker._jwt = "old-jwt"
    broker._expires_at = datetime.now(UTC) - timedelta(seconds=1)
    http.responses = [response({"token": "new-jwt"}), response({"account_ids": []})]
    assert broker.list_accounts() == []
    assert http.calls[0][1].endswith("/sessions")


def test_repeated_unauthorized_request_stops_and_masks_credentials(http):
    broker = FinamBroker({"token": "secret"})
    http.responses = [
        response({"token": "jwt1"}),
        response(status=401),
        response({"token": "jwt2"}),
        response(status=401, text="secret jwt2 denied"),
    ]
    with pytest.raises(RuntimeError, match=r"Finam API 401: \*\*\* \*\*\* denied"):
        broker.list_accounts()
    assert len(http.calls) == 4


@pytest.mark.parametrize("payload", [{}, {"token": ""}, []])
def test_auth_response_requires_a_token(http, payload):
    http.responses = [response(payload)]
    with pytest.raises(RuntimeError, match="отсутствует JWT"):
        FinamBroker({"token": "secret"}).list_accounts()


def test_account_first_trade_date_is_used_when_open_date_is_absent(http):
    http.responses = [
        response({"token": "jwt"}),
        response({"account_ids": ["acc"]}),
        response({"account_id": "acc", "first_trade_date": "2021-02-03T04:05:06Z"}),
    ]
    assert FinamBroker({"token": "secret"}).list_accounts()[0].opened_at == datetime(2021, 2, 3, 4, 5, 6, tzinfo=UTC)


def test_normalized_stock_fill_keeps_pieces_exact_symbol_price_and_fee(http, monkeypatch):
    broker, calls = history_broker(monkeypatch, [make_trade()])
    fill = broker.get_fills("acc", START, END)[0]
    assert fill.instrument_id == "SBER@MISX"
    assert fill.ticker == "SBER"
    assert fill.quantity == 10
    assert fill.pnl_price == fill.display_price == Decimal("250.125")
    assert fill.side == Side.BUY
    assert fill.commission == Decimal("1.25")
    assert fill.currency == "rub"
    assert calls[1] == ("GET", "assets/SBER%40MISX", {"account_id": "acc"})


def test_asset_cache_keeps_same_ticker_different_mics_separate(http, monkeypatch):
    items = [make_trade(), make_trade("2", "SBER@SPBE")]
    broker, calls = history_broker(monkeypatch, items, asset={"type": "SHARES"})
    fills = broker.get_fills("acc", START, END)
    assert [fill.instrument_id for fill in fills] == ["SBER@MISX", "SBER@SPBE"]
    assert len([call for call in calls if call[1].startswith("assets/")]) == 2


def test_history_splits_saturated_windows_without_losing_boundary_trades(http, monkeypatch):
    items = [
        make_trade(str(hour), timestamp=f"2026-01-01T{hour:02d}:00:00Z")
        for hour in [1, 8, 12, 18]
    ]
    broker, calls = history_broker(monkeypatch, items, settings={"history_limit": 2})
    fills = broker.get_fills("acc", START, END)
    assert [fill.executed_at.hour for fill in fills] == [1, 8, 12, 18]
    assert len([call for call in calls if call[1].endswith("/trades")]) > 1
    assert len([call for call in calls if call[1].startswith("assets/")]) == 1


def test_history_uses_nonoverlapping_bounded_windows(http, monkeypatch):
    broker, calls = history_broker(monkeypatch, settings={"history_batch_days": 1})
    assert broker.get_fills("acc", START, START + timedelta(days=3)) == []
    assert [call[2]["interval.start_time"] for call in calls] == [
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
    ]
    assert calls[-1][2]["interval.end_time"] == "2026-01-04T00:00:00Z"
    assert all(call[2]["limit"] == 50 for call in calls)


def test_history_limit_is_capped_to_documented_example_size(http):
    assert FinamBroker({"token": "secret", "history_limit": 1000}).trade_limit == 50


def test_saturated_single_microsecond_fails_instead_of_silent_truncation(http, monkeypatch):
    time = "2026-01-01T00:00:00Z"
    broker, _ = history_broker(monkeypatch, [make_trade("1", timestamp=time), make_trade("2", timestamp=time)], settings={"history_limit": 2})
    with pytest.raises(RuntimeError, match="полная выгрузка не подтверждена"):
        broker.get_fills("acc", START, START + timedelta(microseconds=1))


def test_duplicate_id_is_removed_but_distinct_identical_fills_remain(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade(), make_trade(), make_trade("2")])
    assert len(broker.get_fills("acc", START, END)) == 2


def test_conflicting_duplicate_trade_fails(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade(), make_trade(price={"value": "251"})])
    with pytest.raises(RuntimeError, match="противоречивые данные"):
        broker.get_fills("acc", START, END)


@pytest.mark.parametrize("quantity", ["1.5", "0", "-1", "NaN", "bad"])
def test_invalid_or_fractional_quantity_is_not_truncated_or_skipped(http, monkeypatch, quantity):
    broker, _ = history_broker(monkeypatch, [make_trade(size={"value": quantity})])
    with pytest.raises(RuntimeError):
        broker.get_fills("acc", START, END)


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "SIDE_UNSPECIFIED"},
        {"symbol": "SBER"},
        {"price": {"value": "NaN"}},
        {"commission": None},
        {"commission": {"value": {"value": "1"}, "currency": "USD"}},
        {"currency": ""},
        {"trade_id": ""},
        {"account_id": "other"},
    ],
)
def test_incomplete_fill_data_causes_error(http, monkeypatch, overrides):
    broker, _ = history_broker(monkeypatch, [make_trade(**overrides)])
    with pytest.raises(RuntimeError):
        broker.get_fills("acc", START, END)


def test_unknown_fee_is_distinct_from_explicit_zero(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade(commission={"value": {"value": "0"}, "currency": ""})])
    assert broker.get_fills("acc", START, END)[0].commission == Decimal("0")


def test_mixed_currencies_do_not_become_a_meaningless_combined_pnl(http, monkeypatch):
    second = make_trade("2", currency="USD", commission={"value": {"value": "1"}, "currency": "USD"})
    broker, _ = history_broker(monkeypatch, [make_trade(), second])
    with pytest.raises(RuntimeError, match="разные валюты"):
        broker.get_fills("acc", START, END)


def test_future_uses_explicit_money_per_quote_point_and_retains_display_price(http, monkeypatch):
    symbol = "SiZ6@FORTS"
    broker, _ = history_broker(monkeypatch, [make_trade(symbol=symbol)], asset={
        "type": "FUTURES", "ticker": "SiZ6", "mic": "FORTS", "future_details": {"contract_size": {"value": "1000"}},
    }, settings={"price_multipliers": {symbol: "2.5"}})
    fill = broker.get_fills("acc", START, END)[0]
    assert fill.display_price == Decimal("250.125")
    assert fill.pnl_price == Decimal("625.3125")
    assert fill.quantity == 10


def test_future_does_not_guess_money_multiplier_from_contract_size(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade(symbol="SiZ6@FORTS")], asset={
        "type": "FUTURES", "future_details": {"contract_size": {"value": "1000"}},
    })
    with pytest.raises(RuntimeError, match="денежная цена не определена"):
        broker.get_fills("acc", START, END)


def test_price_factor_extension_receives_exact_symbol_metadata_and_time(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade()])
    received = []

    def factor(symbol, asset, executed_at):
        received.append((symbol, asset["type"], executed_at))
        return Decimal("3")

    monkeypatch.setattr(broker, "_price_factor", factor)
    assert broker.get_fills("acc", START, END)[0].pnl_price == Decimal("750.375")
    assert received == [("SBER@MISX", "SHARES", datetime(2026, 1, 1, 12, tzinfo=UTC))]


def test_invalid_intervals_fail_before_network_access(http):
    broker = FinamBroker({"token": "secret"})
    with pytest.raises(ValueError, match="timezone-aware"):
        broker.get_fills("acc", datetime(2026, 1, 1), END)
    with pytest.raises(ValueError, match="Начало периода"):
        broker.get_fills("acc", END, START)
    assert broker.get_fills("acc", START, START) == []
    assert http.calls == []


def test_fill_order_preserves_nanoseconds_before_datetime_conversion(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [
        make_trade("late", timestamp="2026-01-01T12:00:00.123456900Z", side="SIDE_SELL"),
        make_trade("early", timestamp="2026-01-01T12:00:00.123456100Z"),
    ])
    fills = broker.get_fills("acc", START, END)
    assert [fill.side for fill in fills] == [Side.BUY, Side.SELL]
    assert fills[0].executed_at == fills[1].executed_at


def test_settlement_currency_extension_allows_ruble_fees_for_usd_quoted_future(http, monkeypatch):
    broker, _ = history_broker(monkeypatch, [make_trade(currency="USD")])
    monkeypatch.setattr(broker, "_price_factor", lambda *args: Decimal("80"))
    monkeypatch.setattr(broker, "_price_currency", lambda *args: "RUB")
    fill = broker.get_fills("acc", START, END)[0]
    assert fill.pnl_price == Decimal("20010")
    assert fill.currency == "rub"


def test_rate_limit_waits_then_retries_the_same_read(http, monkeypatch):
    delays = []
    monkeypatch.setattr(finam.time, "sleep", delays.append)
    broker = FinamBroker({"token": "api-secret", "request_interval_seconds": 0})
    broker._jwt = "jwt"
    limited = response(status=429)
    limited.headers = {"Retry-After": "2"}
    http.responses = [limited, response({"trades": []})]
    assert broker._request("GET", "accounts/acc/trades", params={"limit": 50}) == {"trades": []}
    assert delays == [2]
    assert http.calls[0] == http.calls[1]


def test_requests_are_paced_before_exceeding_broker_quota(http, monkeypatch):
    state = {"now": 0.0, "delays": []}

    def sleep(seconds):
        state["delays"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(finam.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(finam.time, "sleep", sleep)
    broker = FinamBroker({"token": "api-secret"})
    broker._jwt = "jwt"
    http.responses = [response({}), response({})]
    broker._request("GET", "assets/clock")
    broker._request("GET", "assets/clock")
    assert state["delays"] == [0.32]
