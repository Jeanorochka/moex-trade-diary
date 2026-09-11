from datetime import datetime, timezone
from decimal import Decimal

from trade_exporter.domain.models import Fill, Side

UTC = timezone.utc


def make_fill(
    hour: int,
    side: Side,
    price: str,
    qty: int = 1,
    commission: str = "0",
    ticker: str = "TEST",
    instrument_id: str = "uid-test",
):
    return Fill(
        executed_at=datetime(2026, 9, 11, hour, tzinfo=UTC),
        instrument_id=instrument_id,
        ticker=ticker,
        instrument_type="share",
        side=side,
        quantity=qty,
        pnl_price=Decimal(price),
        display_price=Decimal(price),
        commission=Decimal(commission),
        currency="rub",
    )
