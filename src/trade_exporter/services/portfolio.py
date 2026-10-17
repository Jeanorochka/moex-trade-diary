from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
import math
from collections.abc import Sequence

import polars as pl

from trade_exporter.domain.portfolio import CashFlow, PortfolioPosition, PortfolioReport, PortfolioSnapshot


ZERO = Decimal("0")
MONEY = pl.Decimal(38, 18)
SIMPLE_ASSETS = {"share", "shares", "stock", "stocks", "equity", "equities", "etf", "etfs", "fund", "funds"}
POSITION_SCHEMA = {
    "instrument_id": pl.String, "ticker": pl.String, "asset_type": pl.String,
    "quantity": MONEY, "average_price": MONEY, "current_price": MONEY,
    "price_currency": pl.String, "average_price_currency": pl.String,
    "market_value": MONEY, "cost_basis": MONEY, "unrealized_pnl": MONEY,
    "valuation_currency": pl.String, "unrealized_return": pl.Float64,
    "weight": pl.Float64, "equity_weight": pl.Float64,
    "long_exposure": MONEY, "short_exposure": MONEY,
    "price_as_of": pl.Datetime("us", "UTC"), "valuation_source": pl.String,
    "unrealized_source": pl.String, "valuation_status": pl.String,
    "price_status": pl.String, "quantity_includes_pending_orders": pl.Boolean,
}


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        with localcontext() as context:
            context.prec = 80
            result = value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
        if not result.is_finite() or result.copy_abs() >= Decimal("1e20"):
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Денежное значение не помещается в Decimal(38,18)") from error
    return result


def _derivative(asset_type: str) -> bool:
    kind = asset_type.lower()
    return any(word in kind for word in ("future", "option", "swap", "spread", "фьючер", "опцион"))


def _position(position: PortfolioPosition, snapshot: PortfolioSnapshot, quote_max_age: timedelta | None) -> dict:
    if position.price_as_of is not None and position.price_as_of > snapshot.captured_at:
        raise ValueError("Время котировки не может быть позже снимка портфеля")
    stale = (
        quote_max_age is not None and position.price_as_of is not None
        and snapshot.captured_at - position.price_as_of > quote_max_age
    )
    price_status = "stale" if stale else "timestamp_unknown" if position.price_as_of is None else "as_of_quote"
    current_price = None if stale else position.current_price
    average_currency = position.average_price_currency or position.price_currency
    simple = position.asset_type.lower() in SIMPLE_ASSETS
    derivative = _derivative(position.asset_type)
    pending = snapshot.positions_include_pending_orders if position.quantity_includes_pending_orders is None else position.quantity_includes_pending_orders
    market = position.market_value
    cost = position.cost_basis
    unrealized = position.unrealized_pnl
    valuation_source = position.valuation_source
    unrealized_source = position.unrealized_source
    if market is not None:
        status = "broker"
        valuation_source = valuation_source or "broker_market_value"
    elif position.quantity == 0 and not pending:
        market = ZERO
        status = "calculated"
        valuation_source = "zero_quantity"
    elif simple and not pending and not stale and current_price is not None and position.price_currency == position.valuation_currency:
        if current_price < 0:
            raise ValueError("Цена акции или фонда не может быть отрицательной")
        market = current_price * position.quantity
        status = "calculated"
        valuation_source = "current_price_times_signed_quantity"
    else:
        status = "pending_quantity" if pending else "stale_quote" if stale else "unsupported_price_basis" if derivative or not simple else "foreign_currency" if position.price_currency != position.valuation_currency else "missing_price"
    if cost is None and simple and not pending and position.average_price is not None and average_currency == position.valuation_currency:
        if position.average_price < 0:
            raise ValueError("Средняя цена акции или фонда не может быть отрицательной")
        cost = position.average_price * position.quantity
    if unrealized is not None:
        unrealized_source = unrealized_source or "broker_unrealized_pnl"
    elif not derivative and market is not None and cost is not None:
        unrealized = market - cost
        unrealized_source = "market_value_minus_signed_cost_basis"
    if position.quantity == 0 and not pending:
        cost = ZERO if cost is None else cost
        unrealized = ZERO if unrealized is None else unrealized
    if position.valuation_currency != snapshot.base_currency:
        status = "foreign_currency"
    base_market = market if position.valuation_currency == snapshot.base_currency else None
    funded = not derivative and base_market is not None
    return {
        "instrument_id": position.instrument_id,
        "ticker": position.ticker,
        "asset_type": position.asset_type,
        "quantity": _money(position.quantity),
        "average_price": _money(position.average_price),
        "current_price": _money(current_price),
        "price_currency": position.price_currency,
        "average_price_currency": average_currency,
        "market_value": _money(market),
        "cost_basis": _money(cost),
        "unrealized_pnl": _money(unrealized),
        "valuation_currency": position.valuation_currency,
        "unrealized_return": float(unrealized / cost.copy_abs()) if not derivative and unrealized is not None and cost else None,
        "weight": None,
        "equity_weight": float(base_market / snapshot.equity) if base_market is not None and snapshot.equity > 0 else None,
        "long_exposure": _money(max(base_market, ZERO)) if funded else None,
        "short_exposure": _money(max(-base_market, ZERO)) if funded else None,
        "price_as_of": position.price_as_of.astimezone(timezone.utc) if position.price_as_of is not None else None,
        "valuation_source": valuation_source,
        "unrealized_source": unrealized_source,
        "valuation_status": status,
        "price_status": price_status,
        "quantity_includes_pending_orders": pending,
    }


def normalize_cash_flows(
    flows: Sequence[CashFlow], captured_at: datetime, ledger_start: datetime | None = None,
) -> tuple[CashFlow, ...]:
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("Для денежных потоков требуется время снимка с часовым поясом")
    result = []
    ids = {}
    for flow in flows:
        if not isinstance(flow, CashFlow):
            raise ValueError("flows должен содержать CashFlow")
        if flow.timestamp > captured_at:
            raise ValueError("Денежная операция не может быть позже снимка портфеля")
        if ledger_start is not None and flow.timestamp < ledger_start:
            raise ValueError("Денежная операция раньше заявленного начала полного журнала")
        if flow.id:
            if flow.id in ids:
                if ids[flow.id] != flow:
                    raise ValueError("Противоречивые денежные операции с одинаковым id")
                continue
            ids[flow.id] = flow
        result.append(flow)
    return tuple(sorted(result, key=lambda flow: flow.timestamp))


def _xirr(flows: Sequence[CashFlow], snapshot: PortfolioSnapshot) -> tuple[float | None, str]:
    external = [flow for flow in flows if flow.kind == "external" and flow.amount]
    if not any(flow.amount > 0 for flow in external):
        return None, "no_inception_funding"
    values: dict[datetime, Decimal] = defaultdict(lambda: ZERO)
    for flow in external:
        values[flow.timestamp] -= flow.amount
    values[snapshot.captured_at] += snapshot.equity
    entries = sorted((timestamp, amount) for timestamp, amount in values.items() if amount)
    if not entries or (entries[-1][0] - entries[0][0]).total_seconds() < 86400:
        return None, "same_day"
    if not any(amount > 0 for _, amount in entries) or not any(amount < 0 for _, amount in entries):
        return None, "no_sign_change"
    base_time = entries[0][0]
    scale = max(amount.copy_abs() for _, amount in entries)
    terms = [
        ((timestamp - base_time).total_seconds() / (365 * 86400), float(amount / scale))
        for timestamp, amount in entries
    ]

    def value(log_rate):
        exponents = [-years * log_rate for years, _ in terms]
        maximum = max(exponents)
        return math.fsum(amount * math.exp(exponent - maximum) for (_, amount), exponent in zip(terms, exponents))

    roots = []
    previous_x = -20.0
    previous_value = value(previous_x)
    for step in range(1, 1601):
        current_x = -20.0 + step * 0.025
        current_value = value(current_x)
        if current_value == 0:
            roots.append(current_x)
        elif previous_value * current_value < 0:
            low, high = previous_x, current_x
            low_value = previous_value
            for _ in range(90):
                middle = (low + high) / 2
                middle_value = value(middle)
                if middle_value == 0:
                    low = high = middle
                    break
                if low_value * middle_value < 0:
                    high = middle
                else:
                    low, low_value = middle, middle_value
            roots.append((low + high) / 2)
        previous_x, previous_value = current_x, current_value
    distinct = []
    for root in roots:
        if not distinct or abs(root - distinct[-1]) > 1e-7:
            distinct.append(root)
    if len(distinct) != 1:
        return None, "multiple_roots" if len(distinct) > 1 else "no_bracketed_root"
    signs = [amount > 0 for _, amount in entries]
    if sum(left != right for left, right in zip(signs, signs[1:])) > 1:
        return None, "non_conventional_cash_flows"
    return math.expm1(distinct[0]), "unique_bracketed_root"


def analyze_portfolio(
    snapshot: PortfolioSnapshot,
    flows: Sequence[CashFlow] = (),
    *,
    ledger_complete: bool = False,
    ledger_start: datetime | None = None,
    quote_max_age: timedelta | None = None,
) -> PortfolioReport:
    if not isinstance(snapshot, PortfolioSnapshot):
        raise ValueError("Требуется PortfolioSnapshot")
    if ledger_start is not None:
        if ledger_start.tzinfo is None or ledger_start.utcoffset() is None or ledger_start > snapshot.captured_at:
            raise ValueError("Некорректная дата начала полного журнала")
    if ledger_complete and ledger_start is None:
        raise ValueError("Для полного журнала требуется дата начала истории счёта")
    if quote_max_age is not None and (not isinstance(quote_max_age, timedelta) or quote_max_age.total_seconds() < 0):
        raise ValueError("quote_max_age должен быть неотрицательным timedelta")
    ordered_flows = normalize_cash_flows(flows, snapshot.captured_at, ledger_start)
    with localcontext() as context:
        context.prec = 80
        rows = [_position(position, snapshot, quote_max_age) for position in snapshot.positions]
        known_positions = sum((row["market_value"] for row in rows if row["market_value"] is not None and row["valuation_currency"] == snapshot.base_currency), ZERO)
        valuation_complete = all(row["market_value"] is not None and row["valuation_currency"] == snapshot.base_currency for row in rows)
        known_unrealized = sum((row["unrealized_pnl"] for row in rows if row["unrealized_pnl"] is not None and row["valuation_currency"] == snapshot.base_currency), ZERO)
        unrealized_complete = all(not row["quantity_includes_pending_orders"] and row["unrealized_pnl"] is not None and row["valuation_currency"] == snapshot.base_currency for row in rows)
        funded_rows = [row for row in rows if not _derivative(row["asset_type"])]
        allocation_complete = all(row["market_value"] is not None and row["valuation_currency"] == snapshot.base_currency for row in funded_rows)
        long_value = sum((row["long_exposure"] or ZERO for row in funded_rows), ZERO)
        short_value = sum((row["short_exposure"] or ZERO for row in funded_rows), ZERO)
        gross_exposure = long_value + short_value
        for row in funded_rows:
            if allocation_complete and gross_exposure > 0:
                row["weight"] = float(row["market_value"].copy_abs() / gross_exposure)
        cost_complete = all(row["cost_basis"] is not None and row["unrealized_pnl"] is not None and row["valuation_currency"] == snapshot.base_currency for row in funded_rows)
        cost_base = sum((row["cost_basis"].copy_abs() for row in funded_rows if row["cost_basis"] is not None and row["valuation_currency"] == snapshot.base_currency), ZERO)
        funded_unrealized = sum((row["unrealized_pnl"] for row in funded_rows if row["unrealized_pnl"] is not None and row["valuation_currency"] == snapshot.base_currency), ZERO)
        cash_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for balance in snapshot.cash:
            cash_totals[balance.currency] += balance.amount
        cash_rows = [{"currency": currency, "amount": _money(amount), "amount_base": _money(amount) if currency == snapshot.base_currency else None} for currency, amount in sorted(cash_totals.items())]
        cash_complete = all(balance.currency == snapshot.base_currency for balance in snapshot.cash)
        known_cash = cash_totals.get(snapshot.base_currency, ZERO)
        residual = snapshot.equity - known_cash - known_positions
        residual_threshold = Decimal("0.01")
        components_complete = valuation_complete and cash_complete and not any(row["quantity_includes_pending_orders"] for row in rows)
        reconciled = components_complete and residual.copy_abs() <= residual_threshold
        allocation_groups = defaultdict(lambda: [ZERO, ZERO])
        for row in funded_rows:
            if row["long_exposure"] is not None:
                allocation_groups[row["asset_type"]][0] += row["long_exposure"]
                allocation_groups[row["asset_type"]][1] += row["short_exposure"]
        allocation_rows = [{
            "asset_type": asset_type, "long_market_value": _money(long), "short_market_value": _money(short),
            "gross_exposure": _money(long + short),
            "long_weight": float(long / long_value) if allocation_complete and long_value > 0 else None,
            "short_weight": float(short / short_value) if allocation_complete and short_value > 0 else None,
            "equity_weight": float((long - short) / snapshot.equity) if snapshot.equity > 0 else None,
        } for asset_type, (long, short) in sorted(allocation_groups.items())]
        flow_rows = [{
            "timestamp": flow.timestamp.astimezone(timezone.utc), "amount": _money(flow.amount),
            "currency": flow.currency, "kind": flow.kind, "id": flow.id,
            "amount_base": _money(flow.amount) if flow.currency == snapshot.base_currency else None,
        } for flow in ordered_flows]
        base_flows = [flow for flow in ordered_flows if flow.currency == snapshot.base_currency]
        external = [flow for flow in base_flows if flow.kind == "external"]
        inflows = sum((flow.amount for flow in external if flow.amount > 0), ZERO)
        outflows = -sum((flow.amount for flow in external if flow.amount < 0), ZERO)
        net_funding = inflows - outflows
        external_currency_complete = all(flow.currency == snapshot.base_currency or flow.amount == 0 for flow in ordered_flows if flow.kind == "external")
        has_funding = any(flow.kind == "external" and flow.amount > 0 for flow in ordered_flows)
        zero_empty = snapshot.equity == 0 and not ordered_flows
        funding_status = "evidenced_external_funding" if has_funding else "empty_zero_equity" if zero_empty else "missing_inception_funding"
        returns_complete = ledger_complete and external_currency_complete and (has_funding or zero_empty)
        lifetime_profit = snapshot.equity - net_funding if returns_complete else None
        xirr, xirr_status = _xirr(ordered_flows, snapshot) if returns_complete else (None, "incomplete_ledger_or_currency")
        if ledger_complete and funding_status == "missing_inception_funding":
            xirr_status = "no_inception_funding"
        summary = {
            "account_id": snapshot.account_id, "broker": snapshot.broker,
            "captured_at": snapshot.captured_at.isoformat(), "base_currency": snapshot.base_currency,
            "equity": snapshot.equity, "equity_authoritative": True,
            "cash_base": known_cash if cash_complete else None, "known_cash_base": known_cash,
            "positions_market_value": known_positions if valuation_complete else None,
            "known_positions_market_value": known_positions,
            "valuation_residual": residual,
            "valuation_residual_basis": "equity_minus_known_base_currency_components",
            "valuation_components_complete": components_complete,
            "valuation_complete": reconciled, "valuation_reconciled": reconciled,
            "valuation_residual_threshold": residual_threshold,
            "valuation_reconciliation_status": "reconciled" if reconciled else "unexplained_balance" if components_complete else "partial_components",
            "positions_count": len(rows),
            "derivative_positions_count": sum(_derivative(row["asset_type"]) for row in rows),
            "priced_positions_count": sum(row["market_value"] is not None and row["valuation_currency"] == snapshot.base_currency for row in rows),
            "unvalued_positions_count": sum(row["market_value"] is None or row["valuation_currency"] != snapshot.base_currency for row in rows),
            "long_market_value": long_value, "short_market_value": short_value,
            "gross_exposure": gross_exposure, "allocation_complete": allocation_complete,
            "unrealized_pnl": known_unrealized if unrealized_complete else None,
            "known_unrealized_pnl": known_unrealized, "unrealized_complete": unrealized_complete,
            "unrealized_cost_basis": cost_base if cost_complete else None,
            "holdings_return": funded_unrealized / cost_base if cost_complete and cost_base > 0 else None,
            "holdings_return_basis": "non_derivative_unrealized_over_absolute_cost",
            "total_external_inflows": inflows, "total_external_outflows": outflows,
            "net_external_funding": net_funding,
            "lifetime_profit": lifetime_profit,
            "profit_to_net_funding_ratio": lifetime_profit / net_funding if lifetime_profit is not None and net_funding > 0 else None,
            "xirr": xirr, "xirr_status": xirr_status,
            "ledger_complete": ledger_complete, "returns_complete": returns_complete,
            "funding_status": funding_status,
            "flows_currency_complete": all(flow.currency == snapshot.base_currency for flow in ordered_flows),
            "external_flows_currency_complete": external_currency_complete,
            "ledger_start": ledger_start.isoformat() if ledger_start is not None else None,
            "income": sum((flow.amount for flow in base_flows if flow.kind == "income"), ZERO) if all(flow.currency == snapshot.base_currency or flow.amount == 0 for flow in ordered_flows if flow.kind == "income") else None,
            "fees": -sum((flow.amount for flow in base_flows if flow.kind == "fee"), ZERO) if all(flow.currency == snapshot.base_currency or flow.amount == 0 for flow in ordered_flows if flow.kind == "fee") else None,
            "taxes": -sum((flow.amount for flow in base_flows if flow.kind == "tax"), ZERO) if all(flow.currency == snapshot.base_currency or flow.amount == 0 for flow in ordered_flows if flow.kind == "tax") else None,
            "source": snapshot.source,
            "positions_include_pending_orders": snapshot.positions_include_pending_orders,
            "stale_quotes_count": sum(row["price_status"] == "stale" for row in rows),
            "unknown_quote_times_count": sum(row["price_status"] == "timestamp_unknown" for row in rows),
        }
    return PortfolioReport(
        summary=summary,
        positions=pl.DataFrame(rows, schema=POSITION_SCHEMA),
        allocation=pl.DataFrame(allocation_rows, schema={
            "asset_type": pl.String, "long_market_value": MONEY, "short_market_value": MONEY,
            "gross_exposure": MONEY, "long_weight": pl.Float64, "short_weight": pl.Float64, "equity_weight": pl.Float64,
        }),
        cash=pl.DataFrame(cash_rows, schema={"currency": pl.String, "amount": MONEY, "amount_base": MONEY}),
        flows=pl.DataFrame(flow_rows, schema={
            "timestamp": pl.Datetime("us", "UTC"), "amount": MONEY, "currency": pl.String,
            "kind": pl.String, "id": pl.String, "amount_base": MONEY,
        }),
    )
