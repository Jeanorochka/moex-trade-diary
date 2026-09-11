from decimal import Decimal

from trade_exporter.domain.models import Side
from trade_exporter.services.matcher import FifoMatcher
from conftest import make_fill


def test_long_trade_pnl_and_commission():
    fills = [
        make_fill(10, Side.BUY, "100", qty=2, commission="2"),
        make_fill(11, Side.SELL, "106", qty=2, commission="2"),
    ]
    trade = FifoMatcher().match(fills)[0]
    assert trade.direction == "LONG"
    assert trade.quantity == 2
    assert trade.gross_pnl == Decimal("12")
    assert trade.commission == Decimal("4")
    assert trade.net_pnl == Decimal("8")


def test_short_trade_pnl():
    fills = [
        make_fill(10, Side.SELL, "106", commission="1"),
        make_fill(11, Side.BUY, "100", commission="1"),
    ]
    trade = FifoMatcher().match(fills)[0]
    assert trade.direction == "SHORT"
    assert trade.gross_pnl == Decimal("6")
    assert trade.commission == Decimal("2")


def test_fifo_uses_oldest_entry_first():
    fills = [
        make_fill(9, Side.BUY, "100"),
        make_fill(10, Side.BUY, "110"),
        make_fill(11, Side.SELL, "120"),
    ]
    trades = FifoMatcher().match(fills)
    assert len(trades) == 1
    assert trades[0].entry_price == Decimal("100")
    assert trades[0].gross_pnl == Decimal("20")


def test_partial_close_allocates_commission_per_unit():
    fills = [
        make_fill(9, Side.BUY, "100", qty=2, commission="4"),
        make_fill(10, Side.SELL, "110", qty=1, commission="3"),
    ]
    trade = FifoMatcher().match(fills)[0]
    # На закрытый 1 контракт приходится 2 комиссии входа + 3 выхода.
    assert trade.commission == Decimal("5")
    assert trade.gross_pnl == Decimal("10")


def test_reversal_closes_then_opens_opposite_position():
    fills = [
        make_fill(9, Side.BUY, "100"),
        make_fill(10, Side.SELL, "105", qty=2),
        make_fill(11, Side.BUY, "101"),
    ]
    trades = FifoMatcher().match(fills)
    assert len(trades) == 2
    assert trades[0].direction == "LONG"
    assert trades[0].gross_pnl == Decimal("5")
    assert trades[1].direction == "SHORT"
    assert trades[1].gross_pnl == Decimal("4")


def test_instruments_with_same_ticker_are_isolated_by_uid():
    fills = [
        make_fill(9, Side.BUY, "100", ticker="X", instrument_id="uid-1"),
        make_fill(10, Side.SELL, "110", ticker="X", instrument_id="uid-2"),
    ]
    assert FifoMatcher().match(fills) == []
