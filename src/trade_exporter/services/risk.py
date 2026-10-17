from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import math
from statistics import NormalDist, mean, stdev
from zoneinfo import ZoneInfo

import polars as pl

from trade_exporter.domain.portfolio import CashFlow, PortfolioSnapshot
from trade_exporter.services.portfolio import _xirr, normalize_cash_flows


ZERO = Decimal("0")
MOSCOW = ZoneInfo("Europe/Moscow")


def _decimal(value, field, *, optional=False):
    if value is None and optional:
        return None
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
    except (ValueError, InvalidOperation) as error:
        raise ValueError(f"Риск: некорректное число {field}") from error
    return result


def _time(value, field, *, normalize=True):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError) as error:
        raise ValueError(f"Риск: требуется дата с часовым поясом {field}") from error
    return parsed.astimezone(timezone.utc) if normalize else parsed


def _money(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 80
        return value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)


def _metric(metric, label, value, unit, method, observations=0, basis="actual_nav", reason=""):
    if isinstance(value, float) and not math.isfinite(value):
        value = None
        reason = reason or "Результат выходит за конечный числовой диапазон."
    return {
        "metric": metric, "label": label, "value": value, "unit": unit,
        "method": method, "observations": observations, "basis": basis, "quality_reason": reason,
    }


def _annualize(rate, days):
    if rate is None or days is None or days < 1:
        return None, "Для годового пересчёта нужен период не короче суток."
    value = float(rate)
    if not math.isfinite(value) or value < -1:
        return None, "Годовой пересчёт не определён при результате ниже −100%."
    if value == -1:
        return -1.0, "Годовой пересчёт темпа за период; это не прогноз."
    exponent = math.log1p(value) * 365 / float(days)
    if exponent > 700:
        return None, "Годовой пересчёт слишком велик для устойчивого представления."
    return math.expm1(exponent), "Годовой пересчёт темпа за период; это не прогноз."


def _covered(start, flows, currency, ledger_complete, ledger_start):
    if not ledger_complete or ledger_start is None or ledger_start > start:
        return False, "Не подтверждена полная история внешних потоков за этот период."
    if any(flow.kind == "external" and flow.amount and flow.currency != currency for flow in flows):
        return False, "Для внешних потоков требуется исторический валютный пересчёт."
    return True, ""


def _dietz(start, end, opening, closing, flows):
    seconds = Decimal(str((end - start).total_seconds()))
    if seconds <= 0:
        return None, None
    selected = [flow for flow in flows if flow.kind == "external" and start < flow.timestamp <= end]
    net = sum((flow.amount for flow in selected), ZERO)
    capital = opening + sum((
        flow.amount * Decimal(str((end - flow.timestamp).total_seconds())) / seconds
        for flow in selected
    ), ZERO)
    profit = closing - opening - net
    return profit, profit / capital if capital > 0 else None


def _quantile(values, probability):
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    left = int(position)
    fraction = position - left
    return ordered[left] * (1 - fraction) + ordered[min(left + 1, len(ordered) - 1)] * fraction


def _var_rows(prefix, label, values, equity, currency, *, basis, reason="", normal=False):
    rows = []
    count = len(values)
    for confidence, minimum in ((95, 20), (99, 100)):
        enough = count >= (2 if normal else minimum)
        warning = reason
        loss = None
        if enough:
            if normal:
                floats = [float(value) for value in values]
                z = NormalDist().inv_cdf(confidence / 100)
                candidate = z * stdev(floats) - mean(floats)
                loss = Decimal(str(max(0.0, candidate))) if math.isfinite(candidate) else None
                warning = "Нормальная эвристика по короткой выборке; не риск текущих открытых позиций."
            else:
                loss = max(ZERO, -_quantile(values, Decimal(100 - confidence) / 100))
        else:
            warning = (warning + " " if warning else "") + f"Нужно не менее {2 if normal else minimum} суточных наблюдений; доступно {count}."
        method = (
            f"max(0, z{confidence}% × σ − средняя); σ выборочная, горизонт 1 сутки."
            if normal else f"Отрицательный квантиль {100-confidence}% исторических суточных результатов, ограниченный снизу нулём."
        )
        name = f"{prefix}{confidence}_1d"
        rows.append(_metric(name + "_fraction", f"{label} {confidence}%, 1 день", float(loss) if loss is not None else None, "fraction", method, count, basis, warning))
        rows.append(_metric(name + "_money", f"{label} {confidence}%, 1 день, {currency}", _money(loss * equity) if loss is not None and equity > 0 else None, currency, method + " Денежная оценка на указанном капитале.", count, basis, warning or ("Нужен положительный капитал." if equity <= 0 else "")))
    return rows


def _nav_rows(summary, flows, currency, captured_at, ledger_complete, ledger_start):
    raw = summary.get("valuation_history", [])
    if not isinstance(raw, list):
        raise ValueError("Риск: история стоимости должна быть списком наблюдений")
    by_time = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Риск: некорректное наблюдение стоимости")
        timestamp = _time(item.get("captured_at"), "valuation_history")
        equity = _decimal(item.get("equity"), "valuation_history.equity")
        if timestamp > captured_at or item.get("currency") != currency:
            raise ValueError("Риск: наблюдение из будущего или в другой валюте")
        if timestamp in by_time and by_time[timestamp] != equity:
            raise ValueError("Риск: противоречивые оценки в одно время")
        by_time[timestamp] = equity
    values = sorted(by_time.items())
    if ledger_start is not None:
        values = [value for value in values if value[0] >= ledger_start]
    count = len(values)
    drawdown = None
    daily = []
    reason = "Нужны минимум две фактические оценки стоимости портфеля."
    if count >= 2:
        relevant_flows = tuple(flow for flow in flows if values[0][0] < flow.timestamp <= values[-1][0])
        covered, reason = _covered(values[0][0], relevant_flows, currency, ledger_complete, ledger_start)
        if covered:
            index = peak = Decimal("1")
            worst = ZERO
            valid = True
            for (start, opening), (end, closing) in zip(values, values[1:]):
                _, rate = _dietz(start, end, opening, closing, flows)
                if rate is None or rate < -1:
                    valid = False
                    reason = "Не удалось построить индекс при неположительном вложенном капитале или потере свыше 100%."
                    break
                index *= 1 + rate
                peak = max(peak, index)
                worst = max(worst, (peak - index) / peak)
            if valid:
                drawdown = float(worst)
                reason = "Просадка только по сохранённым точкам; внутрипериодные провалы не наблюдались. Индекс приближённо очищен от потоков методом Modified Dietz."
                last_of_day = {}
                for value in values:
                    last_of_day[value[0].astimezone(MOSCOW).date()] = value
                days = sorted(last_of_day.items())
                for (left_day, (start, opening)), (right_day, (end, closing)) in zip(days, days[1:]):
                    duration = (end - start).total_seconds()
                    if right_day - left_day != timedelta(days=1) or abs(duration - 86400) > 1800:
                        continue
                    _, rate = _dietz(start, end, opening, closing, flows)
                    if rate is not None and rate >= -1:
                        daily.append(rate)
    rows = [_metric("max_drawdown_observed", "Просадка по фактическим наблюдениям", drawdown, "fraction", "Связанный индекс из доходностей Modified Dietz между фактическими оценками.", count, "actual_nav", reason)]
    rows.append(_metric("daily_returns_count", "Регулярных суточных доходностей", len(daily), "count", "Последние снимки соседних московских календарных дней с интервалом 24 часа ±30 минут.", count, "actual_nav"))
    rows.extend(_var_rows("historical_var", "Историческая VaR портфеля", daily, _decimal(summary.get("equity"), "equity"), currency, basis="actual_nav"))
    return rows


def _result_pnl(results, capital_started_at, captured_at, end_exclusive=None, *, calendar_timezone=MOSCOW):
    if not isinstance(results, pl.DataFrame) or not {"date", "net_pnl"} <= set(results.columns):
        raise ValueError("Риск: результаты должны содержать date и net_pnl")
    pnl = defaultdict(lambda: ZERO)
    start_day = capital_started_at.astimezone(calendar_timezone).date()
    last_day = ((end_exclusive - timedelta(microseconds=1)) if end_exclusive is not None else captured_at).astimezone(calendar_timezone).date()
    for item in results.select("date", "net_pnl").iter_rows(named=True):
        day = item["date"]
        if isinstance(day, datetime):
            day = day.astimezone(calendar_timezone).date() if day.tzinfo else day.date()
        if isinstance(day, str):
            try:
                day = date.fromisoformat(day)
            except ValueError as error:
                raise ValueError("Риск: некорректная дата дневного результата") from error
        if not isinstance(day, date):
            raise ValueError("Риск: некорректная дата дневного результата")
        amount = _decimal(item["net_pnl"], "results.net_pnl")
        if start_day <= day <= last_day:
            pnl[day] += amount
    return pnl


def _proxy_rows(results, capital, capital_started_at, captured_at, currency, *, end_exclusive=None, calendar_timezone=MOSCOW):
    if results is not None and not isinstance(results, pl.DataFrame):
        raise ValueError("Риск: результаты должны быть DataFrame")
    if results is None or results.is_empty() or capital <= 0 or capital_started_at is None:
        reason = "Нужны введённый положительный капитал, дата начала и дневные денежные результаты выбранного периода."
        rows = [_metric("proxy_max_drawdown", "Просадка кривой результатов", None, "fraction", "Стартовый капитал плюс накопленные денежные результаты; открытые акции не переоцениваются.", 0, "closed_pnl_proxy", reason)]
        rows.extend(_var_rows("proxy_var", "Историческая VaR кривой результатов", [], capital, currency, basis="closed_pnl_proxy", reason=reason))
        rows.extend(_var_rows("proxy_normal_var", "Эвристическая VaR кривой результатов", [], capital, currency, basis="closed_pnl_proxy", reason=reason, normal=True))
        return rows
    pnl = _result_pnl(results, capital_started_at, captured_at, end_exclusive, calendar_timezone=calendar_timezone)
    if not pnl:
        return _proxy_rows(None, capital, capital_started_at, captured_at, currency, calendar_timezone=calendar_timezone)
    first = capital_started_at.astimezone(calendar_timezone).date()
    last = (end_exclusive - timedelta(microseconds=1)).astimezone(calendar_timezone).date() if end_exclusive is not None else max(pnl)
    equity = peak = capital
    worst = ZERO
    returns = []
    insolvent = False
    day = first
    while day <= last:
        change = pnl[day]
        if equity > 0:
            returns.append(change / equity)
        else:
            insolvent = True
        equity += change
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
        day += timedelta(days=1)
    if equity <= 0:
        insolvent = True
    reason = "Результатная кривая, не полная рыночная стоимость портфеля: FIFO акций и денежные строки фьючерсов; дни без строк равны нулю."
    values = [] if insolvent else returns
    if insolvent:
        reason += " Капитал кривой стал неположительным; VaR по относительным результатам не рассчитывается."
    rows = [_metric("proxy_max_drawdown", "Просадка кривой результатов", float(worst), "fraction", "Стартовый капитал плюс накопленные net P&L, включая стартовую точку до первой потери.", len(returns), "closed_pnl_proxy", reason)]
    rows.append(_metric("proxy_daily_returns_count", "Дней в кривой результатов", len(returns), "count", "Календарные дни выбранного периода; дни без результатных строк равны нулю.", len(returns), "closed_pnl_proxy", reason))
    rows.extend(_var_rows("proxy_var", "Историческая VaR кривой результатов", values, max(equity, ZERO), currency, basis="closed_pnl_proxy", reason=reason))
    rows.extend(_var_rows("proxy_normal_var", "Эвристическая VaR кривой результатов", values, max(equity, ZERO), currency, basis="closed_pnl_proxy", reason=reason, normal=True))
    return rows


def _label_result_period(rows, start, end, calendar_timezone):
    if start is None or end is None:
        return rows
    first = start.astimezone(calendar_timezone).date()
    last = (end - timedelta(microseconds=1)).astimezone(calendar_timezone).date()
    for row in rows:
        row["as_of"] = (end - timedelta(microseconds=1)).isoformat()
        row["period_start"] = start.isoformat()
        row["period_end_exclusive"] = end.isoformat()
        row["method"] += f" Период: {first:%d.%m.%Y}–{last:%d.%m.%Y}."
    return rows


def analyze_portfolio_risk(
    summary: dict,
    flows: Sequence[CashFlow] = (),
    *,
    ledger_complete: bool = False,
    ledger_start: datetime | None = None,
    starting_capital: Decimal = ZERO,
    capital_started_at: datetime | None = None,
    results: pl.DataFrame | None = None,
) -> dict:
    captured_at = _time(summary.get("captured_at"), "captured_at")
    currency = str(summary.get("base_currency") or "RUB").upper()
    equity = _decimal(summary.get("equity"), "equity")
    capital = _decimal(starting_capital, "starting_capital")
    if capital < 0:
        raise ValueError("Риск: стартовый капитал не может быть отрицательным")
    ledger_start = _time(ledger_start, "ledger_start") if ledger_start is not None else None
    capital_calendar_start = _time(capital_started_at, "capital_started_at", normalize=False) if capital_started_at is not None else None
    capital_started_at = capital_calendar_start.astimezone(timezone.utc) if capital_calendar_start is not None else None
    if capital_started_at is not None and capital_started_at >= captured_at:
        raise ValueError("Риск: дата стартового капитала должна быть раньше текущей оценки")
    ordered = normalize_cash_flows(flows, captured_at, ledger_start)
    quality = []
    with localcontext() as context:
        context.prec = 80
        rows = _nav_rows(summary, ordered, currency, captured_at, ledger_complete, ledger_start)
        rows.append(_metric("starting_capital", "Введённый стартовый капитал", capital if capital > 0 else None, currency, "Исходная стоимость на указанную пользователем дату.", basis="starting_capital_assumption"))
        rows.append(_metric("current_equity", "Текущая стоимость портфеля", equity, currency, "Авторитетная оценка брокера; нереализованный результат повторно не прибавляется.", 1, "actual_nav"))
        profit = rate = annualized = baseline_xirr = None
        period_days = None
        baseline_reason = "Нужны положительный стартовый капитал и дата начала."
        annual_reason = baseline_reason
        xirr_reason = baseline_reason
        if capital > 0 and capital_started_at is not None:
            period_days = Decimal(str((captured_at - capital_started_at).total_seconds())) / 86400
            selected = tuple(flow for flow in ordered if capital_started_at < flow.timestamp <= captured_at)
            covered, baseline_reason = _covered(capital_started_at, selected, currency, ledger_complete, ledger_start)
            if covered:
                profit, rate = _dietz(capital_started_at, captured_at, capital, equity, selected)
                if rate is None:
                    baseline_reason = "Взвешенный вложенный капитал неположительный; доходность Modified Dietz не определена."
                annualized, annual_reason = _annualize(rate, period_days)
                synthetic = (CashFlow(capital_started_at, capital, currency),) + tuple(flow for flow in selected if flow.kind == "external")
                baseline_snapshot = PortfolioSnapshot(str(summary.get("account_id") or "portfolio"), str(summary.get("broker") or "broker"), captured_at, currency, equity)
                baseline_xirr, xirr_status = _xirr(synthetic, baseline_snapshot)
                xirr_reason = "Ставка по введённому начальному капиталу, внешним потокам и конечной оценке." if baseline_xirr is not None else f"XIRR не определена: {xirr_status}."
            else:
                annual_reason = xirr_reason = baseline_reason
        rows.extend([
            _metric("capital_value_change_unadjusted", "Изменение стоимости к введённому капиталу", (equity - capital) / capital if capital > 0 else None, "fraction", "(Текущая стоимость − введённый капитал) / капитал; без поправки на вводы и выводы.", 2 if capital > 0 else 0, "starting_capital_assumption", "Этот показатель не является чистой доходностью при дополнительных вводах или выводах."),
            _metric("capital_period_profit", "Прибыль с даты стартового капитала", _money(profit), currency, "Текущая стоимость − стартовый капитал − чистые внешние потоки периода.", len(ordered), "starting_capital_assumption", baseline_reason),
            _metric("capital_period_return", "Доходность с даты стартового капитала", float(rate) if rate is not None else None, "fraction", "Modified Dietz: прибыль / капитал с весами внешних потоков по времени.", len(ordered), "starting_capital_assumption", baseline_reason),
            _metric("capital_period_xirr", "Годовая денежно-взвешенная доходность от стартового капитала", baseline_xirr, "fraction", "XIRR: введённый капитал, внешние потоки и конечная стоимость; год 365 дней.", len(ordered) + 2, "money_weighted_xirr", xirr_reason),
            _metric("capital_period_annualized", "Годовой пересчёт доходности от стартового капитала", annualized, "fraction", "(1 + доходность Modified Dietz)^(365 / дни) − 1.", 2 if capital > 0 else 0, "annualized_extrapolation", annual_reason),
        ])
        observed = summary.get("observed_period") or {}
        observed_rate = _decimal(observed.get("modified_dietz_return"), "observed_period.return", optional=True)
        observed_days = _decimal(observed.get("days"), "observed_period.days", optional=True)
        observed_annual, observed_reason = _annualize(observed_rate, observed_days)
        rows.append(_metric("annualized_dietz_return", "Годовой пересчёт между фактическими оценками", observed_annual, "fraction", "(1 + Modified Dietz за наблюдаемый период)^(365 / дни) − 1.", len(summary.get("valuation_history", [])), "annualized_extrapolation", observed_reason))
        lifetime_xirr = summary.get("xirr") if summary.get("returns_complete") else None
        lifetime_xirr = float(lifetime_xirr) if lifetime_xirr is not None else None
        rows.append(_metric("annualized_money_weighted_return", "Годовая денежно-взвешенная доходность с открытия", lifetime_xirr, "fraction", "XIRR полного журнала внешних потоков с открытия счёта.", len(ordered), "money_weighted_xirr", "" if lifetime_xirr is not None else "Не подтверждены полный журнал с открытия, валюта потоков или единственность ставки."))
        result_calendar_start = _time(summary["result_period_start"], "result_period_start", normalize=False) if summary.get("result_period_start") else capital_calendar_start
        result_start = result_calendar_start.astimezone(timezone.utc) if result_calendar_start is not None else None
        result_end = _time(summary["result_period_end"], "result_period_end") if summary.get("result_period_end") else None
        result_calendar_timezone = result_calendar_start.tzinfo if result_calendar_start is not None else MOSCOW
        proxy_rows = _proxy_rows(results, capital, result_start, captured_at, currency, end_exclusive=result_end, calendar_timezone=result_calendar_timezone)
        rows.extend(_label_result_period(proxy_rows, result_start, result_end, result_calendar_timezone))
        exposure = _decimal(summary.get("gross_exposure"), "gross_exposure", optional=True)
        partial = not summary.get("allocation_complete", False) or int(summary.get("derivative_positions_count", 0)) > 0
        scenario_reason = "Только известная стоимость акций и фондов; номинал и риск фьючерсов/опционов не включены." if partial else "Сценарий неблагоприятного движения отдельных позиций, не вероятностная VaR."
        for shock in (5, 10):
            rows.append(_metric(f"adverse_position_shock_{shock}pct", f"Неблагоприятный сдвиг известных позиций на {shock}%", _money(exposure * Decimal(shock) / 100) if exposure is not None else None, currency, "Длинные позиции падают, короткие растут на заданную долю; убыток = доля × (long + |short|).", int(summary.get("positions_count", 0)), "scenario", scenario_reason))
        if capital > 0:
            quality.append("Стартовая оценка задана пользователем; она не заменяет подтверждённую брокером историческую стоимость.")
        if partial:
            quality.append("Стресс по позициям частичный: производные инструменты и неизвестные оценки не моделируются.")
        if results is not None:
            quality.append("VaR и просадка результатной кривой отделены от рыночной стоимости открытого портфеля.")
    return {
        "basis": "portfolio_snapshot_and_observed_nav", "as_of": captured_at.isoformat(),
        "metrics": rows, "quality": quality,
        "sources": [
            {"title": "GIPS: Modified Dietz", "url": "https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/"},
            {"title": "Microsoft: XIRR", "url": "https://support.microsoft.com/en-us/excel/functions/xirr-function"},
            {"title": "BIS: VaR holding period", "url": "https://www.bis.org/committees/bcbs/basel-framework/standard/dis/50/inforce/2019-12-15/published/2019-12-15"},
        ],
    }


def analyze_result_risk(
    results: pl.DataFrame,
    starting_capital: Decimal,
    period_from: datetime,
    period_to: datetime,
    base_currency: str = "RUB",
) -> dict:
    capital = _decimal(starting_capital, "starting_capital")
    if capital < 0:
        raise ValueError("Риск: стартовый капитал должен быть неотрицательным")
    start = _time(period_from, "period_from")
    end = _time(period_to, "period_to")
    calendar_timezone = _time(period_from, "period_from", normalize=False).tzinfo
    if start >= end:
        raise ValueError("Риск: начало периода должно быть раньше окончания")
    currency = str(base_currency).upper()
    with localcontext() as context:
        context.prec = 80
        pnl = _result_pnl(results, start, end, end, calendar_timezone=calendar_timezone)
        net = sum(pnl.values(), ZERO)
        terminal = capital + net
        rate = net / capital if capital > 0 else None
        days = Decimal(str((end - start).total_seconds())) / 86400
        annualized, reason = _annualize(rate, days)
        basis_reason = "Модель по денежным результатным строкам выбранного периода; не текущая рыночная стоимость открытого портфеля."
        rows = [
            _metric("proxy_starting_capital", "Стартовый капитал кривой результатов", capital if capital > 0 else None, currency, "Введённая исходная оценка на дату начала выбранного периода.", basis="closed_pnl_proxy"),
            _metric("proxy_net_pnl", "Денежный результат выбранного периода", _money(net), currency, "Сумма net P&L результатных строк в пределах выбранных дат.", len(pnl), "closed_pnl_proxy", basis_reason),
            _metric("proxy_terminal_capital", "Итоговый капитал результатной модели", _money(terminal) if capital > 0 else None, currency, "Введённый капитал + net P&L строк; это не фактическая текущая стоимость счёта.", len(pnl), "closed_pnl_proxy", basis_reason),
            _metric("proxy_period_return", "Результат периода / стартовый капитал", float(rate) if rate is not None else None, "fraction", "Net P&L выбранных строк / введённый капитал; внешние потоки и переоценка открытых акций не моделируются.", len(pnl), "closed_pnl_proxy", basis_reason),
            _metric("proxy_annualized_return", "Годовая экстраполяция результатной модели", annualized, "fraction", "(1 + net P&L / стартовый капитал)^(365 / дней выбранного периода) − 1; не прогноз.", len(pnl), "annualized_extrapolation", reason + " " + basis_reason),
        ]
        rows.extend(_proxy_rows(results, capital, start, end, currency, end_exclusive=end, calendar_timezone=calendar_timezone))
        _label_result_period(rows, start, end, calendar_timezone)
    return {
        "basis": "closed_pnl_proxy", "as_of": end.isoformat(),
        "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
        "metrics": rows,
        "quality": [basis_reason, "VaR результатной модели и её просадка не заменяют риск текущих открытых позиций."],
    }
