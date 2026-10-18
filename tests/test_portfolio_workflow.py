from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import orjson
import polars as pl
import pytest

from trade_exporter import cli
from trade_exporter.domain.models import Account
from trade_exporter.services.portfolio_service import execute_portfolio


UTC = timezone.utc
OPENED = datetime(2025, 10, 17, tzinfo=UTC)
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def money(value, currency="rub"):
    return {"units": str(value), "nano": 0, "currency": currency}


def quotation(value):
    return {"units": str(value), "nano": 0}


def finam_money(value):
    return {"units": str(value), "nanos": 0, "currency_code": "RUB"}


def make_broker(name):
    broker = SimpleNamespace(name=name, settings={}, calls=[])
    if name == "finam":
        def request(method, path, **kwargs):
            broker.calls.append((method, path, kwargs))
            assert method == "GET"
            if path.endswith("/orders"):
                return {"orders": []}
            if path == "accounts/1":
                return {
                    "account_id": "1", "portfolio_mc": {}, "open_account_date": OPENED.isoformat(),
                    "first_trade_date": OPENED.isoformat(), "first_non_trade_date": OPENED.isoformat(),
                    "equity": {"value": "1500"}, "cash": [finam_money(300)],
                    "positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10"}, "current_price": {"value": "120"}, "average_price": {"value": "100"}, "current_price_currency": "RUB", "average_price_currency": "RUB", "unrealized_pnl": {"value": "200"}}],
                }
            if path.endswith("/transactions"):
                return {"transactions": [
                    {"id": "fund", "timestamp": OPENED.isoformat(), "transaction_category": "DEPOSIT", "change": finam_money(1000)},
                    {"id": "dividend", "timestamp": "2026-01-01T00:00:00Z", "transaction_category": "INCOME", "change": finam_money(300)},
                    {"id": "buy", "timestamp": OPENED.isoformat(), "transaction_category": "OTHERS", "transaction_name": "Покупка акции", "change": finam_money(-1000)},
                ]}
            pytest.fail(f"Unexpected endpoint {path}")

        broker._request = request
        broker._asset = lambda *args: {"ticker": "SBER", "type": "SHARES", "quote_currency": "RUB"}
    else:
        def post(method, payload):
            broker.calls.append(("POST", method, payload))
            if method.endswith("/GetPortfolio"):
                return {
                    "accountId": "1", "totalAmountPortfolio": money(1500),
                    "positions": [{"instrumentUid": "sber-id", "ticker": "SBER", "instrumentType": "share", "quantity": quotation(10), "currentPrice": money(120), "averagePositionPriceFifo": money(100), "expectedYieldFifo": quotation(200)}],
                    "expectedYield": quotation(99999),
                }
            if method.endswith("/GetPositions"):
                return {"accountId": "1", "money": [money(300)], "blocked": [money(100)]}
            if method.endswith("/GetOperationsByCursor"):
                return {"hasNext": False, "items": [
                    {"id": "fund", "date": OPENED.isoformat(), "type": "OPERATION_TYPE_INPUT", "state": "OPERATION_STATE_EXECUTED", "payment": money(1000)},
                    {"id": "dividend", "date": "2026-01-01T00:00:00Z", "type": "OPERATION_TYPE_DIVIDEND", "state": "OPERATION_STATE_EXECUTED", "payment": money(300)},
                    {"id": "buy", "date": OPENED.isoformat(), "type": "OPERATION_TYPE_BUY", "state": "OPERATION_STATE_EXECUTED", "payment": money(-1000)},
                ]}
            pytest.fail(f"Unexpected endpoint {method}")

        broker._post = post
    return broker


@pytest.mark.parametrize("broker_name", ["finam", "tbank"])
def test_year_old_unsold_share_is_valued_from_current_broker_portfolio(broker_name, tmp_path, monkeypatch, capsys):
    account = Account("1", "year old holding", broker_name, opened_at=OPENED)
    broker = make_broker(broker_name)
    broker.list_accounts = lambda: [account]
    broker.get_fills = lambda **kwargs: pytest.fail("an unsold holding does not need a closing trade")
    monkeypatch.setattr(cli, "create_broker", lambda *args: broker)
    monkeypatch.setattr(cli, "execute_portfolio", lambda *args, **kwargs: execute_portfolio(*args, **kwargs, captured_at=NOW))
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    paths = cli.main(["--portfolio", "--broker", broker_name, "--config", str(config), "--account-id", "1", "--output-dir", str(tmp_path)])
    assert all(path.exists() for path in paths)
    payload = orjson.loads(paths[1].read_bytes())["portfolio"]
    assert Decimal(payload["summary"]["equity"]) == 1500
    assert Decimal(payload["summary"]["cash_base"]) == 300
    assert Decimal(payload["summary"]["unrealized_pnl"]) == 200
    assert Decimal(payload["summary"]["lifetime_profit"]) == 500
    assert Decimal(payload["summary"]["income"]) == 300
    assert payload["summary"]["valuation_reconciled"] is True
    position = payload["positions"][0]
    assert Decimal(position["quantity"]) == 10
    assert Decimal(position["cost_basis"]) == 1000
    assert Decimal(position["market_value"]) == 1200
    assert Decimal(str(position["unrealized_return"])) == Decimal("0.2")
    from trade_exporter.services.analytics import load_report_portfolio
    restored = load_report_portfolio(paths[0])
    assert restored.positions.height == 1
    assert restored.positions["quantity"][0] == 10
    assert len(paths) == 2
    assert not list(tmp_path.rglob("*.html"))
    assert list(pl.read_excel(paths[0], sheet_id=0)) == ["Отчёт"]
    assert "Excel:" in capsys.readouterr().out
