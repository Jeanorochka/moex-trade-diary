from datetime import datetime, timezone
from decimal import Decimal
import os

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
        "items": [op("OPERATION_TYPE_BUY", 23000, 2, 4)],
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
