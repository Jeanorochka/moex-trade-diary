from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

import orjson

from trade_exporter.brokers.portfolio import CashFlowHistory, load_cash_flows, load_portfolio
from trade_exporter.domain.portfolio import CashFlow, PortfolioSnapshot
from trade_exporter.exporters.portfolio import export_portfolio
from trade_exporter.services.portfolio import analyze_portfolio, normalize_cash_flows
from trade_exporter.services.risk import analyze_portfolio_risk


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


def execute_portfolio(
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
        history = CashFlowHistory((), False, ("История денежных потоков не получена.",), account.opened_at)
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
    report.summary["starting_capital"] = starting_capital
    report.summary["capital_started_at"] = capital_started_at.isoformat() if capital_started_at else None
    report.summary["result_period_start"] = (result_period_start or capital_started_at).isoformat() if result_period_start or capital_started_at else None
    report.summary["result_period_end"] = result_period_end.isoformat() if result_period_end else None
    report.summary["risk"] = analyze_portfolio_risk(
        report.summary, risk_history.flows,
        ledger_complete=risk_history.complete, ledger_start=risk_history.started_at,
        starting_capital=starting_capital, capital_started_at=capital_started_at, results=results,
    )
    report.summary["risk"]["quality"].extend(
        reason for reason in risk_history.reasons if reason not in report.summary["risk"]["quality"]
    )
    if results_quality:
        reason = "Ряд денежных результатов: " + " ".join(results_quality)
        report.summary["risk"]["quality"].append(reason)
        for row in report.summary["risk"]["metrics"]:
            if row["metric"].startswith("proxy_"):
                row["quality_reason"] = " ".join(filter(None, (row.get("quality_reason"), reason)))
    prefix = f"portfolio_{snapshot.broker}_{sha256(snapshot.account_id.encode()).hexdigest()[:8]}"
    status("Сохранение портфеля, финансовых показателей и графиков")
    paths = export_portfolio(report, output_dir, prefix=prefix)
    return report, paths
