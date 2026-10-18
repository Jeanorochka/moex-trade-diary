from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

from trade_exporter.services.rankings import _day, _event, _number


@dataclass(frozen=True)
class ResultRow:
    day: date | None
    ticker: str
    entry: Decimal | None
    exit: Decimal | None
    result: Decimal | None
    opened: bool = False


def report_dates(analysis, summary, period_from=None, period_to=None):
    period = summary.get("portfolio_period") or {}
    start = period_from or period.get("started_at")
    end = period_to
    if end is None and period.get("requested_end"):
        end = datetime.fromisoformat(period["requested_end"]) - timedelta(microseconds=1)
    end = end or period.get("ended_at")
    days = analysis.trades["date"].drop_nulls().to_list()
    return _day(start) or min(days, default=date.today()), _day(end) or max(days, default=date.today())


def report_rows(analysis, portfolio, summary):
    events = analysis.trades.to_dicts()
    gross = summary.get("commission_basis") == "transactions_period" or summary.get("commission_complete") is False or any(_event(row.get("event_type")) == "broker_fee" for row in events)
    currency = (summary.get("portfolio_period") or {}).get("base_currency") or (portfolio.summary.get("base_currency") if portfolio is not None else None) or summary.get("base_currency") or "RUB"
    positions = portfolio.positions.to_dicts() if portfolio is not None else []
    aliases = defaultdict(set)
    for position in positions:
        aliases[position["ticker"]].add(position.get("instrument_id") or position["ticker"])

    def identity(ticker):
        candidates = aliases.get(ticker, ())
        return next(iter(candidates)) if len(candidates) == 1 else ticker

    closed, opened, candidates = [], [], []
    totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    margin_tickers = set()
    unknown_open = set()
    with localcontext() as context:
        context.prec = 80
        for event in events:
            kind = _event(event.get("event_type")) if "event_type" in analysis.trades.columns else "fifo"
            if kind not in {"fifo", "margin", "future_fee"}:
                continue
            ticker = identity(event["ticker"])
            amount = _number(event["gross_pnl"] if gross else event["net_pnl"])
            totals[ticker][0] += amount
            if kind == "margin":
                margin_tickers.add(ticker)
            row = ResultRow(_day(event["date"]), ticker, _number(event.get("entry_price"), optional=True), _number(event.get("exit_price"), optional=True), amount)
            closed.append(row)
            if kind == "fifo":
                candidates.append(row)
        period = summary.get("portfolio_period") or {}
        historical = period.get("valuation_basis") not in {None, "current_broker_nav"}
        captured = (portfolio.summary.get("captured_at") if portfolio is not None else None)
        if not historical:
            for position in positions:
                pending = position.get("quantity_includes_pending_orders", portfolio.summary.get("positions_include_pending_orders", False))
                if _number(position.get("quantity"), optional=True) == 0 and not pending:
                    continue
                ticker = position.get("instrument_id") or position["ticker"]
                amount = _number(position.get("unrealized_pnl"), optional=True)
                if pending or position.get("valuation_currency") != currency or position.get("price_status") == "stale":
                    amount = None
                derivative = str(position.get("asset_type", "")).lower() in {"future", "futures", "option", "options"}
                row = ResultRow(_day(captured), ticker, _number(position.get("average_price"), optional=True), _number(position.get("current_price"), optional=True), amount, True)
                opened.append(row)
                if derivative and ticker in margin_tickers:
                    continue
                if amount is not None:
                    candidates.append(row)
                    totals[ticker][1] += amount
                else:
                    unknown_open.add(ticker)
                    totals[ticker]
        assets = [(ticker, realized, None if ticker in unknown_open else unrealized, None if ticker in unknown_open else realized + unrealized) for ticker, (realized, unrealized) in totals.items()]
    closed.sort(key=lambda row: (row.day or date.min, row.ticker))
    opened.sort(key=lambda row: row.ticker)
    best = sorted((row for row in candidates if row.result > 0), key=lambda row: (-row.result, row.ticker, row.day or date.min))[:5]
    worst = sorted((row for row in candidates if row.result < 0), key=lambda row: (row.result, row.ticker, row.day or date.min))[:5]
    assets.sort(key=lambda row: (row[3] is None, row[3] or Decimal(0), row[0]))
    return closed, opened, best, worst, assets, gross, currency


def report_metrics(summary, portfolio):
    period = summary.get("portfolio_period") or {}
    complete = period.get("status") == "complete"
    metrics = []
    for key, label in (
        ("starting_equity", "капитал в начале"), ("ending_equity", "капитал в конце"),
        ("external_inflows", "пополнения"), ("external_outflows", "выводы"),
        ("total_profit", "финрез портфеля"), ("period_return", "доходность за период"),
    ):
        value = period.get(key)
        if key in {"total_profit", "period_return"} and not complete:
            value = None
        metrics.append((label, value, "0.00%" if key == "period_return" else "#,##0.00"))
    if not period and portfolio is not None:
        metrics[1] = ("капитал сейчас", portfolio.summary.get("equity"), "#,##0.00")
    metrics.append(("комиссии сделок", summary.get("commission") if summary.get("trading_history_loaded") is not False else None, "#,##0.00"))
    risk = {row["metric"]: row for row in (summary.get("risk") or {}).get("metrics", [])}
    for keys, label, format_code in (
        (("max_drawdown_observed", "proxy_max_drawdown"), "максимальная просадка", "0.00%"),
        (("historical_var95_1d_money", "proxy_var95_1d_money", "proxy_normal_var95_1d_money"), "var 95%, 1 день", "#,##0.00"),
        (("historical_var99_1d_money", "proxy_var99_1d_money", "proxy_normal_var99_1d_money"), "var 99%, 1 день", "#,##0.00"),
    ):
        chosen = next((risk[key] for key in keys if risk.get(key, {}).get("value") is not None), None)
        heuristic = chosen is not None and chosen["metric"].startswith("proxy_")
        metrics.append((label + (", оценка" if heuristic else ""), chosen.get("value") if chosen else None, format_code))
    extrapolation = [
        ("текущая годовая доходность", period.get("annualized_return") if complete else None, "0.00%"),
        ("годовая доходность xirr", period.get("xirr") if complete else None, "0.00%"),
    ]
    return metrics, extrapolation
