from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import orjson
import polars as pl

from trade_exporter.brokers.portfolio import CashFlowHistory, load_cash_flows, load_portfolio
from trade_exporter.domain.portfolio import CashFlow, PortfolioSnapshot
from trade_exporter.exporters.portfolio import export_portfolio
from trade_exporter.services.portfolio import analyze_portfolio, normalize_cash_flows
from trade_exporter.services.risk import analyze_portfolio_risk
from trade_exporter.services.performance import validate_portfolio_period


def _datetime(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("В истории стоимости указана дата без часового пояса") from error
    return result.astimezone(timezone.utc)


def _money(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("В истории стоимости указана некорректная сумма") from error
    return result


@contextmanager
def _locked(path):
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_snapshot(snapshot: PortfolioSnapshot, output_dir: Path) -> list[dict]:
    directory = Path(output_dir) / "portfolio_history"
    directory.mkdir(parents=True, exist_ok=True)
    identity = sha256(f"{snapshot.broker}\0{snapshot.account_id}\0{snapshot.base_currency}".encode()).hexdigest()[:24]
    path = directory / f"{identity}.json"
    with _locked(path):
        records = []
        if path.exists():
            try:
                payload = orjson.loads(path.read_bytes())
            except orjson.JSONDecodeError as error:
                raise ValueError("Не удалось прочитать сохранённую историю стоимости") from error
            if not isinstance(payload, dict) or (
                payload.get("schema_version"), payload.get("broker"), payload.get("account_id"), payload.get("base_currency")
            ) != (1, snapshot.broker, snapshot.account_id, snapshot.base_currency):
                raise ValueError("Сохранённая история относится к другому счёту или формату")
            if not isinstance(payload.get("snapshots"), list):
                raise ValueError("Некорректная структура истории стоимости")
            previous = None
            for item in payload["snapshots"]:
                if not isinstance(item, dict) or item.get("currency") != snapshot.base_currency:
                    raise ValueError("Некорректная валюта сохранённой оценки")
                timestamp = _datetime(item.get("captured_at"))
                equity = _money(item.get("equity"))
                if previous is not None and timestamp <= previous:
                    raise ValueError("В истории стоимости нарушена последовательность наблюдений")
                records.append({"captured_at": timestamp.isoformat(), "equity": equity, "currency": snapshot.base_currency})
                previous = timestamp
        captured_at = snapshot.captured_at.astimezone(timezone.utc)
        current = {"captured_at": captured_at.isoformat(), "equity": snapshot.equity, "currency": snapshot.base_currency}
        if records:
            previous = _datetime(records[-1]["captured_at"])
            if previous > captured_at:
                raise ValueError("История содержит оценку из будущего: проверь часы компьютера")
            if previous == captured_at:
                if records[-1]["equity"] != snapshot.equity:
                    raise ValueError("Для одного времени получены разные оценки портфеля")
                return records
        records.append(current)
        payload = {
            "schema_version": 1, "broker": snapshot.broker, "account_id": snapshot.account_id,
            "base_currency": snapshot.base_currency, "snapshots": records,
        }
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(orjson.dumps(payload, default=str, option=orjson.OPT_INDENT_2))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return records


def observed_performance(records: list[dict], flows: tuple[CashFlow, ...], *, ledger_complete: bool, ledger_start=None) -> tuple[dict | None, list[str]]:
    if len(records) < 2:
        return None, ["Доходность между наблюдениями появится после следующей оценки портфеля."]
    if not ledger_complete:
        return None, ["Изменение стоимости сохранено; доходность периода требует полной истории внешних потоков."]
    opening, closing = records[0], records[-1]
    start, end = _datetime(opening["captured_at"]), _datetime(closing["captured_at"])
    if ledger_start is not None and ledger_start > start:
        return None, ["История внешних потоков не покрывает первое наблюдение стоимости."]
    flows = normalize_cash_flows(flows, end, ledger_start)
    duration = end - start
    microseconds = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
    if microseconds <= 0:
        return None, ["Для доходности нужны оценки в разные моменты времени."]
    if opening["currency"] != closing["currency"]:
        return None, ["Оценки начала и конца периода выражены в разных валютах."]
    selected = [flow for flow in flows if flow.kind == "external" and start < flow.timestamp <= end]
    if any(flow.currency != opening["currency"] and flow.amount != 0 for flow in selected):
        return None, ["Внешние потоки периода требуют исторического валютного пересчёта."]
    with localcontext() as context:
        context.prec = 50
        net = sum((flow.amount for flow in selected), Decimal(0))
        opening_equity, closing_equity = Decimal(opening["equity"]), Decimal(closing["equity"])
        profit = closing_equity - opening_equity - net
        weighted_capital = opening_equity
        for flow in selected:
            remaining = end - flow.timestamp
            remaining_us = (remaining.days * 86400 + remaining.seconds) * 1_000_000 + remaining.microseconds
            weighted_capital += flow.amount * Decimal(remaining_us) / Decimal(microseconds)
        return {
            "start": start.isoformat(), "end": end.isoformat(), "opening_equity": opening_equity,
            "closing_equity": closing_equity, "net_external_flows": net, "profit": profit,
            "weighted_capital": weighted_capital,
            "modified_dietz_return": profit / weighted_capital if weighted_capital > 0 else None,
            "days": Decimal(microseconds) / Decimal(86400_000_000), "basis": "observed_snapshots",
        }, ([] if weighted_capital > 0 else ["Доходность не определена: взвешенный вложенный капитал неположительный."])


def prepare_portfolio_report(
    broker, account, output_dir: Path, *, captured_at=None,
    starting_capital=Decimal(0), capital_started_at=None, results=None,
    result_period_start=None, result_period_end=None, results_quality=(), progress=None,
):
    status = progress or (lambda message: None)
    status("Получение текущей стоимости и открытых позиций")
    snapshot = load_portfolio(broker, account, captured_at=captured_at)
    history_error = None
    status("Получение денежных операций")
    try:
        if account.opened_at is None:
            history = CashFlowHistory((), False, ("Неизвестна дата открытия счёта; доходность за всю историю не определена.",), None)
        else:
            history = load_cash_flows(
                broker, account, account.opened_at, snapshot.captured_at + timedelta(microseconds=1),
            )
    except RuntimeError as error:
        history_error = str(error)
        history = CashFlowHistory((), False, ("История денежных потоков с открытия счёта не получена.",), account.opened_at)
    report = analyze_portfolio(
        snapshot, history.flows, ledger_complete=history.complete, ledger_start=history.started_at,
    )
    report.summary["cash_flow_quality"] = list(history.reasons)
    if history_error:
        report.summary["cash_flow_error"] = history_error
    try:
        records = record_snapshot(snapshot, output_dir)
    except (OSError, ValueError) as error:
        records, period, quality = [], None, [f"Не удалось сохранить историю стоимости: {error}"]
    else:
        period_history = history
        period_error = None
        try:
            if len(records) > 1 and not history.complete and history_error is None:
                period_history = load_cash_flows(
                    broker, account, _datetime(records[0]["captured_at"]), snapshot.captured_at + timedelta(microseconds=1),
                    require_inception=False,
                )
            period, quality = observed_performance(records, period_history.flows, ledger_complete=period_history.complete, ledger_start=period_history.started_at)
        except (RuntimeError, ValueError) as error:
            period, quality = None, [f"Не удалось сопоставить историю денежных потоков с оценками: {error}"]
            period_error = str(error)
        if period is None and not period_history.complete:
            quality.extend(period_history.reasons)
        if period_error:
            report.summary["observed_cash_flow_error"] = period_error
    report.summary["valuation_history"] = records
    report.summary["observed_period"] = period
    report.summary["period_quality"] = quality
    risk_history = history
    if starting_capital > 0 and capital_started_at is not None:
        status("Расчёт от введённого стартового капитала")
        if not history.complete or history.started_at is None or history.started_at > capital_started_at:
            try:
                risk_history = load_cash_flows(
                    broker, account, capital_started_at, snapshot.captured_at + timedelta(microseconds=1),
                    require_inception=False,
                )
            except RuntimeError:
                risk_history = CashFlowHistory((), False, ("Денежные операции периода не получены; изменение капитала может включать пополнения и выводы.",), capital_started_at)
    evaluation_at = snapshot.captured_at
    ending_equity = snapshot.equity
    valuation_basis = "current_broker_nav"
    valuation_source = snapshot.source
    valuation_quality = []
    if result_period_end is not None and result_period_end <= snapshot.captured_at:
        evaluation_at = result_period_end - timedelta(microseconds=1)
        try:
            if starting_capital <= 0:
                raise ValueError("Для результата исторического периода укажи стартовый капитал")
            broker_end = result_period_end.astimezone(ZoneInfo("Europe/Moscow"))
            if getattr(broker, "name", None) == "finam" and (broker_end.hour, broker_end.minute, broker_end.second, broker_end.microsecond) != (0, 0, 0, 0):
                raise ValueError("Исторический отчёт Финама подтверждает только конец календарного дня по Москве; выбери timezone=Europe/Moscow")
            from trade_exporter.services.historical_valuation import load_historical_valuation

            status("Получение исторической стоимости портфеля на конец периода")
            valuation = load_historical_valuation(broker, account, (broker_end - timedelta(microseconds=1)).date(), output_dir)
            if valuation.currency != snapshot.base_currency:
                raise RuntimeError("Историческая оценка выражена в другой валюте")
            ending_equity = valuation.equity
            valuation_source = str(valuation.source_file)
            valuation_basis = "historical_broker_report"
            valuation_quality.extend(valuation.quality)
        except (RuntimeError, ValueError) as error:
            ending_equity = None
            valuation_basis = "unavailable_historical_nav"
            valuation_source = ""
            valuation_quality.append(f"Оценка портфеля на выбранную конечную дату не получена: {error}")
    period_flows = tuple(
        flow for flow in risk_history.flows
        if (capital_started_at is None or flow.timestamp > capital_started_at) and flow.timestamp <= evaluation_at
    )
    report = replace(report, period_flows=pl.DataFrame([{
        "timestamp": flow.timestamp.astimezone(timezone.utc), "amount": flow.amount,
        "currency": flow.currency, "kind": flow.kind, "id": flow.id,
        "amount_base": flow.amount if flow.currency == snapshot.base_currency else None,
    } for flow in period_flows], schema=report.flows.schema))
    report.summary["period_ledger_complete"] = risk_history.complete
    report.summary["period_ledger_start"] = risk_history.started_at.isoformat() if risk_history.started_at else None
    report.summary["period_cash_flow_quality"] = list(risk_history.reasons)
    report.summary["starting_capital"] = starting_capital
    report.summary["capital_started_at"] = capital_started_at.isoformat() if capital_started_at else None
    report.summary["result_period_start"] = (result_period_start or capital_started_at).isoformat() if result_period_start or capital_started_at else None
    report.summary["result_period_end"] = result_period_end.isoformat() if result_period_end else None
    risk_summary = dict(report.summary)
    if evaluation_at != snapshot.captured_at:
        risk_summary.update({
            "captured_at": evaluation_at.isoformat(), "equity": ending_equity or Decimal(0),
            "valuation_history": [row for row in records if _datetime(row["captured_at"]) <= evaluation_at],
            "observed_period": None, "returns_complete": False, "xirr": None,
            "gross_exposure": None, "positions_count": 0, "allocation_complete": False,
        })
    report.summary["risk"] = analyze_portfolio_risk(
        risk_summary, tuple(flow for flow in risk_history.flows if flow.timestamp <= evaluation_at),
        ledger_complete=risk_history.complete, ledger_start=risk_history.started_at,
        starting_capital=starting_capital, capital_started_at=capital_started_at, results=results,
    )
    metrics = {row["metric"]: row for row in report.summary["risk"]["metrics"]}
    if ending_equity is None:
        for name, row in metrics.items():
            if name.startswith("capital_") or name == "current_equity":
                row["value"] = None
                row["quality_reason"] = " ".join(valuation_quality)
    if evaluation_at != snapshot.captured_at:
        metrics["current_equity"]["label"] = "Стоимость портфеля на конец выбранного периода"
    external = [flow for flow in period_flows if flow.kind == "external" and flow.currency == snapshot.base_currency]
    complete = ending_equity is not None and metrics["capital_period_profit"]["value"] is not None
    reason = metrics["capital_period_profit"].get("quality_reason")
    period_quality = list(dict.fromkeys([*risk_history.reasons, *valuation_quality, *([reason] if reason else [])]))
    report.summary["portfolio_period"] = validate_portfolio_period({
        "status": "complete" if complete else "unavailable", "base_currency": snapshot.base_currency,
        "started_at": capital_started_at.isoformat() if capital_started_at else None,
        "ended_at": evaluation_at.isoformat(), "requested_end": result_period_end.isoformat() if result_period_end else None,
        "starting_equity": starting_capital if starting_capital > 0 else None,
        "ending_equity": ending_equity,
        "external_inflows": sum((flow.amount for flow in external if flow.amount > 0), Decimal(0)) if complete else None,
        "external_outflows": -sum((flow.amount for flow in external if flow.amount < 0), Decimal(0)) if complete else None,
        "net_external_flows": sum((flow.amount for flow in external), Decimal(0)) if complete else None,
        "external_flows": [{"timestamp": flow.timestamp.isoformat(), "amount": flow.amount, "currency": flow.currency} for flow in external],
        "total_profit": metrics["capital_period_profit"]["value"] if complete else None,
        "period_return": metrics["capital_period_return"]["value"] if complete else None,
        "annualized_return": metrics["capital_period_annualized"]["value"] if complete else None,
        "xirr": metrics["capital_period_xirr"]["value"] if complete else None,
        "days": (evaluation_at - capital_started_at).total_seconds() / 86400 if capital_started_at else None,
        "valuation_basis": valuation_basis, "source": valuation_source, "quality": period_quality,
    })
    report.summary["risk"]["quality"].extend(
        reason for reason in risk_history.reasons if reason not in report.summary["risk"]["quality"]
    )
    if results_quality:
        reason = "Ряд денежных результатов: " + " ".join(results_quality)
        report.summary["risk"]["quality"].append(reason)
        for row in report.summary["risk"]["metrics"]:
            if row["metric"].startswith("proxy_"):
                row["quality_reason"] = " ".join(filter(None, (row.get("quality_reason"), reason)))
    return report


def save_portfolio_report(report, output_dir: Path, *, progress=None):
    (progress or (lambda message: None))("Сохранение портфеля, финансовых показателей и графиков")
    summary = report.summary
    prefix = f"portfolio_{summary['broker']}_{sha256(summary['account_id'].encode()).hexdigest()[:8]}"
    return export_portfolio(report, output_dir, prefix=prefix)


def execute_portfolio(broker, account, output_dir: Path, **options):
    report = prepare_portfolio_report(broker, account, output_dir, **options)
    paths = save_portfolio_report(report, output_dir, progress=options.get("progress"))
    return report, paths
