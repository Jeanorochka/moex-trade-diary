from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from trade_exporter.domain.models import ClosedTrade, Fill, Side


@dataclass(slots=True)
class _OpenLot:
    side: Side
    remaining: int
    pnl_price: Decimal
    display_price: Decimal
    commission_per_unit: Decimal


class FifoMatcher:
    """Чистая FIFO-бизнес-логика без HTTP и Excel."""

    def match(self, fills: list[Fill]) -> list[ClosedTrade]:
        queues: dict[str, deque[_OpenLot]] = defaultdict(deque)
        result: list[ClosedTrade] = []

        for fill in sorted(fills, key=lambda item: item.executed_at):
            key = fill.instrument_id or fill.ticker
            queue = queues[key]
            remaining = fill.quantity
            commission_per_unit = (
                fill.commission / Decimal(fill.quantity)
                if fill.quantity
                else Decimal("0")
            )

            while remaining > 0 and queue and queue[0].side != fill.side:
                entry = queue[0]
                qty = min(remaining, entry.remaining)
                q = Decimal(qty)

                if entry.side == Side.BUY:
                    gross = (fill.pnl_price - entry.pnl_price) * q
                    direction = "LONG"
                else:
                    gross = (entry.pnl_price - fill.pnl_price) * q
                    direction = "SHORT"

                commission = (
                    entry.commission_per_unit + commission_per_unit
                ) * q

                result.append(
                    ClosedTrade(
                        exit_at=fill.executed_at,
                        instrument_id=key,
                        ticker=fill.ticker,
                        direction=direction,
                        quantity=qty,
                        entry_price=entry.display_price,
                        exit_price=fill.display_price,
                        gross_pnl=gross,
                        commission=commission,
                    )
                )

                entry.remaining -= qty
                remaining -= qty
                if entry.remaining == 0:
                    queue.popleft()

            if remaining > 0:
                queue.append(
                    _OpenLot(
                        side=fill.side,
                        remaining=remaining,
                        pnl_price=fill.pnl_price,
                        display_price=fill.display_price,
                        commission_per_unit=commission_per_unit,
                    )
                )

        return result
