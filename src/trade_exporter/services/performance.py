from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math
import re


MONEY_FIELDS = (
    "starting_equity", "ending_equity", "external_inflows", "external_outflows",
    "net_external_flows", "total_profit",
)
RATE_FIELDS = ("period_return", "annualized_return", "xirr", "days")


def _amount(raw, name):
    try:
        number = Decimal(str(raw))
        if isinstance(raw, bool) or not number.is_finite() or abs(number) >= Decimal("1e20"):
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            if number.quantize(Decimal("1e-18")) != number:
                raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Некорректная сумма портфеля: {name}") from error
    return number


def _timestamp(raw, name):
    try:
        parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError(f"Дата результата портфеля должна содержать часовой пояс: {name}") from error
    return parsed.astimezone(timezone.utc)


def _currency(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Z]{3}", raw):
        raise ValueError("Некорректная валюта результата портфеля")
    return raw


def _microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _same_rate(actual, expected, name):
    if actual is None or expected is None:
        if actual is not expected:
            raise ValueError(f"Показатель портфеля не сходится с капиталом и потоками: {name}")
        return
    if not math.isclose(actual, expected, rel_tol=1e-11, abs_tol=1e-12):
        raise ValueError(f"Показатель портфеля не сходится с капиталом и потоками: {name}")


def _annualized(rate, days):
    if rate is None or days < 1 or rate < -1:
        return None
    if rate == -1:
        return -1.0
    exponent = math.log1p(rate) * 365 / float(days)
    return math.expm1(exponent) if exponent <= 700 else None


def validate_portfolio_period(value):
    if not isinstance(value, dict) or value.get("status") not in {"complete", "unavailable"}:
        raise ValueError("Некорректный статус результата портфеля")
    result = dict(value)
    currency = _currency(result.get("base_currency"))
    for name in MONEY_FIELDS:
        raw = result.get(name)
        result[name] = _amount(raw, name) if raw is not None else None
    for name in RATE_FIELDS:
        raw = result.get(name)
        if raw is not None:
            try:
                number = float(raw)
                if isinstance(raw, bool) or not math.isfinite(number):
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"Некорректный показатель портфеля: {name}") from error
            result[name] = number
    timestamps = {}
    for name in ("started_at", "ended_at", "requested_end"):
        if result.get(name) is not None:
            timestamps[name] = _timestamp(result[name], name)
    start, end = timestamps.get("started_at"), timestamps.get("ended_at")
    requested_end = timestamps.get("requested_end")
    if start is not None and end is not None and end <= start:
        raise ValueError("Окончание оценки портфеля должно быть позже начала")
    if requested_end is not None and ((start is not None and requested_end <= start) or (end is not None and requested_end <= end)):
        raise ValueError("Исключающая граница периода должна быть позже даты конечной оценки")
    if result.get("days") is not None:
        if result["days"] <= 0 or start is None or end is None:
            raise ValueError("Некорректная длительность результата портфеля")
        _same_rate(result["days"], _microseconds(end - start) / 86400_000_000, "days")
    quality = result.get("quality", [])
    if not isinstance(quality, list) or any(not isinstance(reason, str) for reason in quality):
        raise ValueError("Некорректные пояснения результата портфеля")
    if result["status"] == "complete" and "external_flows" not in result:
        raise ValueError("В полном результате портфеля отсутствует журнал внешних потоков")
    raw_flows = result.get("external_flows", [])
    if not isinstance(raw_flows, list):
        raise ValueError("Некорректный журнал внешних потоков портфеля")
    flows = []
    for raw in raw_flows:
        if not isinstance(raw, dict):
            raise ValueError("Некорректная запись внешнего потока портфеля")
        timestamp = _timestamp(raw.get("timestamp"), "external_flows.timestamp")
        amount = _amount(raw.get("amount"), "external_flows.amount")
        flow_currency = _currency(raw.get("currency"))
        if (start is not None and timestamp <= start) or (end is not None and timestamp > end):
            raise ValueError("Внешний поток находится вне периода оценки портфеля")
        if result["status"] == "complete" and flow_currency != currency:
            raise ValueError("Внешний поток портфеля требует валютного пересчёта")
        flows.append({"timestamp": timestamp.isoformat(), "amount": amount, "currency": flow_currency})
    result["external_flows"] = flows
    if result["status"] == "complete":
        if any(result.get(name) is None for name in MONEY_FIELDS):
            raise ValueError("В полном результате портфеля отсутствуют суммы")
        if start is None or end is None or result.get("days") is None:
            raise ValueError("В полном результате портфеля отсутствуют даты или длительность")
        if result["starting_equity"] <= 0 or result["external_inflows"] < 0 or result["external_outflows"] < 0:
            raise ValueError("Некорректный стартовый капитал или внешние потоки")
        with localcontext() as context:
            context.prec = 80
            inflows = sum((flow["amount"] for flow in flows if flow["amount"] > 0), Decimal(0))
            outflows = -sum((flow["amount"] for flow in flows if flow["amount"] < 0), Decimal(0))
            if result["external_inflows"] != inflows or result["external_outflows"] != outflows or result["net_external_flows"] != inflows - outflows:
                raise ValueError("Внешние потоки портфеля не сходятся с журналом")
            profit = result["ending_equity"] - result["starting_equity"] - result["net_external_flows"]
            if abs(result["total_profit"] - profit) > Decimal("1e-18"):
                raise ValueError("Прибыль портфеля не сходится с капиталом и внешними потоками")
            duration = Decimal(_microseconds(end - start))
            weighted = result["starting_equity"] * duration + sum((
                flow["amount"] * Decimal(_microseconds(end - _timestamp(flow["timestamp"], "external_flows.timestamp")))
                for flow in flows
            ), Decimal(0))
            rate = float(profit * duration / weighted) if weighted > 0 else None
            days = duration / 86400_000_000
            _same_rate(result.get("period_return"), rate, "period_return")
            _same_rate(result.get("annualized_return"), _annualized(rate, days), "annualized_return")
    return result


def attach_portfolio_performance(analysis, portfolio_report):
    if portfolio_report is None:
        return analysis
    period = portfolio_report.summary.get("portfolio_period")
    if period is not None:
        period = validate_portfolio_period(period)
        analysis.summary["portfolio_period"] = deepcopy(period)
        analysis.summary["headline_basis"] = "portfolio_period"
        analysis.summary["headline_profit"] = period.get("total_profit") if period["status"] == "complete" else None
        analysis.summary["headline_return"] = period.get("period_return") if period["status"] == "complete" else None
    risk = deepcopy(portfolio_report.summary.get("risk", {}))
    trading_risk = analysis.summary.get("risk", {})
    if risk:
        rows = [row for row in risk.get("metrics", []) if not row["metric"].startswith("proxy_")]
        rows.extend(deepcopy(trading_risk.get("metrics", [])))
        risk["metrics"] = rows
        risk["quality"] = list(dict.fromkeys([*risk.get("quality", []), *trading_risk.get("quality", [])]))
        analysis.summary["risk"] = risk
        portfolio_report.summary["risk"] = deepcopy(risk)
    portfolio_report.summary["trading_result"] = {
        name: deepcopy(analysis.summary[name]) for name in (
            "gross_pnl", "commission", "net_pnl", "commission_complete", "commission_quality",
            "result_complete", "result_quality", "excluded_instruments",
        ) if name in analysis.summary
    }
    return analysis
