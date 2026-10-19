from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trade_exporter.exporters.report import _summary
from trade_exporter.services.analytics import analyze_file, load_report_portfolio
from trade_exporter.services.report_view import report_metrics, report_rows


@dataclass(frozen=True)
class FileAnalysis:
    path: Path
    summary: dict
    currency: str
    portfolio_period: bool
    cards: tuple
    metrics: tuple
    trades: tuple
    assets: tuple
    daily: tuple
    best: tuple
    worst: tuple
    quality: tuple
    gross: bool


def analyze_local_file(path, starting_capital=Decimal(0)):
    path = Path(str(path).strip().strip('"')).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in {".xlsx", ".csv", ".tsv"}:
        raise ValueError("Выберите существующий файл Excel, CSV или TSV.")
    report = analyze_file(path, starting_capital)
    portfolio = load_report_portfolio(path)
    summary = _summary(report, portfolio)
    closed, opened, best, worst, assets, gross, currency = report_rows(report, portfolio, summary)
    period = summary.get("portfolio_period") or {}
    has_period = bool(period)
    complete = period.get("status") == "complete"
    loaded = summary.get("trading_history_loaded") is not False
    if has_period:
        cards = (
            ("Стоимость портфеля", period.get("ending_equity"), "money"),
            ("Результат за период", period.get("total_profit") if complete else None, "money"),
            ("Доходность за период", period.get("period_return") if complete else None, "percent"),
            ("Текущая годовая", period.get("annualized_return") if complete else None, "percent"),
        )
    else:
        cards = (
            ("Результат сделок", summary.get("net_pnl") if loaded else None, "money"),
            ("Комиссии", summary.get("commission") if loaded else None, "money"),
            ("Прибыльных строк", summary.get("win_rate") if loaded else None, "percent"),
            ("Доходность сделок", summary.get("return_on_capital") if loaded else None, "percent"),
        )
    metrics, extrapolation = report_metrics(summary, portfolio)
    if not has_period:
        metrics = [(label, value, fmt) for label, value, fmt in metrics if value is not None]
    metrics.extend(extrapolation if has_period else [])
    metrics.extend([
        ("Результат сделок после известных комиссий", summary.get("net_pnl") if loaded else None, "#,##0.00"),
        ("Прибыльных строк сделок", summary.get("win_rate") if loaded else None, "0.00%"),
        ("Профит-фактор сделок", summary.get("profit_factor") if loaded else None, "0.00"),
    ])
    if portfolio is not None:
        metrics.append(("Нереализованный результат", portfolio.summary.get("unrealized_pnl"), "#,##0.00"))
    trades = tuple((
        row.day, row.ticker,
        "Открыта" if row.opened else "Закрыта" if row.entry is not None else "Денежный результат",
        row.entry, row.exit, row.result,
    ) for row in [*closed, *opened])
    daily = []
    for row in report.daily.iter_rows(named=True):
        day = row["date"]
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        elif isinstance(day, datetime):
            day = day.date()
        daily.append((day, Decimal(str(row["cumulative_pnl"]))))
    quality = []
    if has_period and not complete:
        quality.append("Полная доходность портфеля недоступна для выбранного периода.")
    if not has_period:
        quality.append("В файле нет полной оценки портфеля, показаны результаты сделок.")
    if summary.get("commission_complete") is False:
        quality.append("Учтены только известные комиссии.")
    if summary.get("result_complete") is False:
        quality.append("Часть инструментов не входит в расчёт сделок.")
    if gross:
        quality.append("Результат по активам показан до комиссий, общая сумма расходов указана отдельно.")
    return FileAnalysis(path, summary, currency, has_period, cards, tuple(metrics), trades, tuple(assets), tuple(daily), tuple(best), tuple(worst), tuple(quality), gross)


def analysis_worker(path, capital, emit):
    try:
        emit("progress", "Читаем файл и рассчитываем аналитику…")
        result = analyze_local_file(path, capital)
        emit("analysis", result)
    except (Exception, SystemExit) as error:
        from trade_exporter.gui import _redact

        emit("error", _redact(error))
    finally:
        emit("idle", None)
