from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import fastexcel
import orjson
import polars as pl

from trade_exporter.domain.models import ClosedTrade
from trade_exporter.services.performance import validate_portfolio_period


MONEY = pl.Decimal(precision=38, scale=18)
ZERO = Decimal("0")
MONEY_COLUMNS = ("entry_price", "exit_price", "gross_pnl", "commission")
TRADE_SCHEMA = {
    "date": pl.Date,
    "ticker": pl.String,
    **{name: MONEY for name in MONEY_COLUMNS},
    "net_pnl": MONEY,
}
COLUMN_NAMES = {
    "дата": "date",
    "тикер": "ticker",
    "цена входа": "entry_price",
    "цена выхода": "exit_price",
    "финрез": "gross_pnl",
    "комиссия": "commission",
    "тип результата": "event_type",
}


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    summary: dict[str, int | float | Decimal | None]
    trades: pl.DataFrame
    daily: pl.DataFrame
    monthly: pl.DataFrame
    by_ticker: pl.DataFrame
    details: dict = field(default_factory=dict)


def _money(value, row: int, column: str, *, round_excess: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"Строка {row}, {column}: требуется конечное число")
    try:
        number = Decimal("".join(str(value).split()).replace(",", "."))
        if not number.is_finite():
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            normalized = number.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
        if (normalized != number and not round_excess) or normalized.copy_abs() >= Decimal("1e20"):
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError(
            f"Строка {row}, {column}: некорректное число {value!r} "
            "(до 20 целых и 18 дробных разрядов)"
        ) from error
    return normalized


def _date(value, row: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, pattern)
                if pattern == "%d.%m.%y":
                    parsed = parsed.replace(year=2000 + int(raw[-2:]))
                return parsed.date()
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            pass
    raise ValueError(f"Строка {row}, date: некорректная дата {value!r}")


def _normalize(frame: pl.DataFrame, *, result_events: bool = False) -> pl.DataFrame:
    names = {}
    for column in frame.columns:
        normalized = column.strip().lower()
        normalized = COLUMN_NAMES.get(normalized, normalized)
        if normalized in names.values():
            raise ValueError(f"Повторяющаяся колонка: {normalized}")
        names[column] = normalized
    frame = frame.rename(names)
    required = [
        name for name in TRADE_SCHEMA
        if name != "net_pnl" and (not result_events or name not in {"entry_price", "exit_price"})
    ]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError("Не хватает колонок: " + ", ".join(missing))
    if result_events:
        frame = frame.with_columns(
            [pl.lit(None).alias(name) for name in ("entry_price", "exit_price", "event_type")
             if name not in frame.columns]
        )
        required = [name for name in TRADE_SCHEMA if name != "net_pnl"] + ["event_type"]

    records = []
    for row_number, row in enumerate(frame.select(required).iter_rows(named=True), 2):
        ticker = row["ticker"]
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError(f"Строка {row_number}, ticker: требуется непустой тикер")
        record = {
            "date": _date(row["date"], row_number),
            "ticker": ticker.strip(),
            **{
                name: None if result_events and name in {"entry_price", "exit_price"}
                and row[name] is None else _money(row[name], row_number, name)
                for name in MONEY_COLUMNS
            },
        }
        record["commission"] = record["commission"].copy_abs()
        if result_events:
            event_type = row["event_type"]
            if event_type is not None and (not isinstance(event_type, str) or not event_type.strip()):
                raise ValueError(f"Строка {row_number}, event_type: требуется непустой тип результата")
            record["event_type"] = event_type.strip() if event_type is not None else None
        records.append(record)

    schema = {name: dtype for name, dtype in TRADE_SCHEMA.items() if name != "net_pnl"}
    if result_events:
        schema["event_type"] = pl.String
    return (
        pl.DataFrame(records, schema=schema)
        .with_columns((pl.col("gross_pnl") - pl.col("commission")).alias("net_pnl"))
        .sort("date", maintain_order=True)
    )


def _totals(frame: pl.DataFrame, key: str) -> pl.DataFrame:
    return (
        frame.group_by(key)
        .agg(
            pl.len().cast(pl.UInt64).alias("trades"),
            pl.col("gross_pnl").sum(),
            pl.col("commission").sum(),
            pl.col("net_pnl").sum(),
        )
        .sort(key)
    )


def _sequence_metrics(pnl: list[Decimal], capital: Decimal) -> tuple[int, int, Decimal, Decimal | None]:
    win_streak = loss_streak = max_wins = max_losses = 0
    equity = peak = capital
    max_drawdown = ZERO
    max_drawdown_pct = ZERO if capital > 0 else None
    for value in pnl:
        win_streak = win_streak + 1 if value > 0 else 0
        loss_streak = loss_streak + 1 if value < 0 else 0
        max_wins = max(max_wins, win_streak)
        max_losses = max(max_losses, loss_streak)
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if max_drawdown_pct is not None:
            max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak)
    return max_wins, max_losses, max_drawdown, max_drawdown_pct


def analyze_frame(
    frame: pl.DataFrame,
    starting_capital: Decimal = ZERO,
) -> AnalysisReport:
    return _analyze(_normalize(frame), starting_capital)


def analyze_results(
    frame: pl.DataFrame,
    starting_capital: Decimal = ZERO,
) -> AnalysisReport:
    return _analyze(_normalize(frame, result_events=True), starting_capital)


def _analyze(
    trades: pl.DataFrame,
    starting_capital: Decimal,
) -> AnalysisReport:
    capital = _money(starting_capital, 0, "starting_capital")
    if capital < 0:
        raise ValueError("Стартовый капитал не может быть отрицательным")
    daily = _totals(trades, "date").with_columns(
        pl.col("net_pnl").cum_sum().alias("cumulative_pnl")
    )
    daily = daily.with_columns(
        (pl.col("cumulative_pnl") + pl.lit(capital, dtype=MONEY)).alias("equity")
    )
    daily = daily.with_columns(
        (pl.col("equity").cum_max().clip(lower_bound=capital) - pl.col("equity")).alias("drawdown")
    )
    monthly = _totals(
        trades.with_columns(pl.col("date").dt.truncate("1mo")), "date"
    )
    by_ticker = (
        trades.group_by("ticker")
        .agg(
            pl.len().cast(pl.UInt64).alias("trades"),
            (pl.col("net_pnl") > 0).sum().cast(pl.UInt64).alias("wins"),
            (pl.col("net_pnl") < 0).sum().cast(pl.UInt64).alias("losses"),
            pl.col("gross_pnl").sum(),
            pl.col("commission").sum(),
            pl.col("net_pnl").sum(),
        )
        .with_columns((pl.col("wins") / pl.col("trades")).alias("win_rate"))
        .select("ticker", "trades", "wins", "losses", "win_rate", "gross_pnl", "commission", "net_pnl")
        .sort("net_pnl", "ticker", descending=[True, False])
    )
    pnl = trades["net_pnl"].to_list()
    winners = trades.filter(pl.col("net_pnl") > 0)["net_pnl"]
    losers = trades.filter(pl.col("net_pnl") < 0)["net_pnl"]
    count = len(pnl)
    wins = len(winners)
    losses = len(losers)
    with localcontext() as context:
        context.prec = 80
        total_win = winners.sum() or ZERO
        total_loss = abs(losers.sum() or ZERO)
        net = trades["net_pnl"].sum() or ZERO
        commissions = trades["commission"].sum() or ZERO
        gross_abs = trades["gross_pnl"].abs().sum() or ZERO
        avg_win = total_win / wins if wins else None
        avg_loss = total_loss / losses if losses else None
        max_wins, max_losses, drawdown, drawdown_pct = _sequence_metrics(pnl, capital)
        summary = {
            "trades": count,
            "wins": wins,
            "losses": losses,
            "breakeven": count - wins - losses,
            "gross_pnl": trades["gross_pnl"].sum() or ZERO,
            "commission": commissions,
            "net_pnl": net,
            "win_rate": wins / count if count else None,
            "avg_trade": net / count if count else None,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": total_win / total_loss if total_loss else None,
            "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
            "best_trade": max(pnl) if pnl else None,
            "worst_trade": min(pnl) if pnl else None,
            "max_win_streak": max_wins,
            "max_loss_streak": max_losses,
            "trading_days": daily.height,
            "max_drawdown": drawdown,
            "max_daily_drawdown": daily["drawdown"].max() or ZERO,
            "commission_load": commissions / gross_abs if gross_abs else None,
            "starting_capital": capital,
            "return_on_capital": net / capital if capital > 0 else None,
            "max_drawdown_pct": drawdown_pct,
        }
    return AnalysisReport(summary, trades, daily, monthly, by_ticker)


def analyze_trades(
    trades: list[ClosedTrade],
    display_timezone,
    starting_capital: Decimal = ZERO,
) -> AnalysisReport:
    if isinstance(display_timezone, str):
        display_timezone = ZoneInfo(display_timezone)
    records = []
    for row_number, trade in enumerate(trades, 2):
        if trade.exit_at.tzinfo is None or trade.exit_at.utcoffset() is None:
            raise ValueError(f"Строка {row_number}: время закрытия должно содержать часовой пояс")
    ordered = sorted(trades, key=lambda trade: trade.exit_at.astimezone(UTC))
    for row_number, trade in enumerate(ordered, 2):
        records.append({
            "date": trade.exit_at.astimezone(display_timezone).date(),
            "ticker": trade.ticker,
            **{
                name: _money(getattr(trade, name), row_number, name, round_excess=True)
                for name in MONEY_COLUMNS
            },
        })
    schema = {
        name: pl.Object if name in MONEY_COLUMNS else dtype
        for name, dtype in TRADE_SCHEMA.items()
        if name != "net_pnl"
    }
    return analyze_frame(pl.DataFrame(records, schema=schema), starting_capital)


def load_diary(path: str | Path) -> pl.DataFrame:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    try:
        if path.suffix.lower() == ".xlsx":
            sheets = fastexcel.read_excel(path).sheet_names
            if not sheets:
                raise ValueError("В книге нет листов")
            if sheets == ["Отчёт"]:
                payload = _clean_payload(path)
                return pl.DataFrame(payload["results"]) if payload["results"] else pl.DataFrame(schema=TRADE_SCHEMA)
            if "Результаты" in sheets:
                results = pl.read_excel(
                    path, sheet_name="Результаты", engine="calamine", infer_schema_length=None,
                    drop_empty_rows=False, raise_if_empty=False,
                )
                normalized = {COLUMN_NAMES.get(name.strip().lower(), name.strip().lower()) for name in results.columns}
                if "event_type" in normalized:
                    return results
            sheet = "Сделки" if "Сделки" in sheets else sheets[0]
            return pl.read_excel(
                path,
                sheet_name=sheet,
                engine="calamine",
                infer_schema_length=None,
                drop_empty_rows=False,
                raise_if_empty=False,
            )
        if path.suffix.lower() in {".csv", ".tsv"}:
            return pl.read_csv(
                path,
                separator="\t" if path.suffix.lower() == ".tsv" else ",",
                infer_schema=False,
                raise_if_empty=False,
            )
        raise ValueError("Поддерживаются файлы .xlsx, .csv и .tsv")
    except Exception as error:
        raise ValueError(f"Не удалось прочитать {path.name}: {error}") from error


def analyze_file(
    path: str | Path,
    starting_capital: Decimal = ZERO,
) -> AnalysisReport:
    frame = load_diary(path)
    normalized = {COLUMN_NAMES.get(name.strip().lower(), name.strip().lower()) for name in frame.columns}
    if "event_type" in normalized:
        report = analyze_results(frame, starting_capital)
    else:
        report = analyze_frame(frame, starting_capital)
    path = Path(path)
    if path.suffix.lower() == ".xlsx" and fastexcel.read_excel(path).sheet_names == ["Отчёт"]:
        payload = _clean_payload(path)
        saved = dict(payload["summary"])
        period = saved.get("portfolio_period")
        if period is not None:
            period = validate_portfolio_period(period)
            capital = period.get("starting_equity")
            if capital is not None and starting_capital > 0 and capital != starting_capital:
                raise ValueError("Стартовый капитал отличается от записанного в оценке портфеля")
            saved["portfolio_period"] = period
        for key, value in saved.items():
            if value is not None and isinstance(report.summary.get(key), Decimal):
                saved[key] = Decimal(str(value))
        for key in ("headline_profit", "headline_return"):
            if saved.get(key) is not None:
                saved[key] = Decimal(str(saved[key]))
        for metric in (saved.get("risk") or {}).get("metrics", []):
            if metric.get("value") is not None:
                metric["value"] = _money(metric["value"], 0, metric["metric"], round_excess=True)
        report.summary.update(saved)
        report.details.update(payload.get("details") or {})
        return report
    if path.suffix.lower() != ".xlsx" or "Параметры расчёта" not in fastexcel.read_excel(path).sheet_names:
        return report
    try:
        parameters = pl.read_excel(path, sheet_name="Параметры расчёта", engine="calamine", infer_schema_length=None)
        if parameters.columns != ["ключ", "значение"]:
            raise ValueError("требуются колонки ключ и значение")
        metadata = {}
        types = {
            "trading_history_loaded": bool, "base_currency": str,
            "commission_complete": bool, "commission_basis": str,
            "commission_quality": list, "net_pnl_basis": str,
            "synthetic_trade_ids": int, "execution_quality": list,
            "result_complete": bool, "result_quality": list, "excluded_instruments": list,
        }
        types.update({"portfolio_period": dict, "risk": dict})
        raw_metadata = {}
        parts = {}
        for key, raw in parameters.iter_rows():
            match = re.fullmatch(r"(risk|portfolio_period)__part_([0-9]{4})", str(key))
            if match:
                name, index = match.group(1), int(match.group(2))
                entries = parts.setdefault(name, {})
                if index in entries or not isinstance(raw, str):
                    raise ValueError("повторяющаяся или некорректная часть параметра")
                entries[index] = raw
                continue
            if key not in types:
                continue
            if key in raw_metadata:
                raise ValueError(f"повторяющийся параметр {key}")
            raw_metadata[key] = raw
        for name, entries in parts.items():
            if name in raw_metadata or sorted(entries) != list(range(1, len(entries) + 1)):
                raise ValueError("неполные или неоднозначные части параметра")
            raw_metadata[name] = "".join(entries[index] for index in sorted(entries))
        for key, raw in raw_metadata.items():
            if key not in types:
                continue
            if key in metadata:
                raise ValueError(f"повторяющийся параметр {key}")
            if not isinstance(raw, str):
                raise ValueError(f"параметр {key} должен содержать JSON")
            value = orjson.loads(raw)
            if type(value) is not types[key]:
                raise ValueError(f"неверный тип параметра {key}")
            if key in {"commission_quality", "execution_quality", "result_quality", "excluded_instruments"} and any(not isinstance(reason, str) for reason in value):
                raise ValueError("причины неполноты должны быть текстом")
            metadata[key] = value
        fee_fields = {"commission_basis", "commission_quality", "net_pnl_basis"}
        if fee_fields.intersection(metadata) and "commission_complete" not in metadata:
            raise ValueError("не указана полнота комиссий")
        if metadata.get("commission_complete") is False:
            metadata["net_pnl_basis"] = "gross_less_known_fees"
            if not metadata.get("commission_quality"):
                metadata["commission_quality"] = ["Полные комиссии исходной выгрузки не подтверждены; учтены только известные расходы."]
        if "portfolio_period" in metadata:
            period = validate_portfolio_period(metadata["portfolio_period"])
            saved_capital = period.get("starting_equity")
            if saved_capital is not None and starting_capital > 0 and starting_capital != saved_capital:
                raise ValueError("Стартовый капитал отличается от записанного в оценке портфеля")
            if saved_capital is not None and starting_capital == 0:
                report = analyze_results(frame, saved_capital) if "event_type" in normalized else analyze_frame(frame, saved_capital)
            metadata["portfolio_period"] = period
            metadata["headline_basis"] = "portfolio_period"
            metadata["headline_profit"] = period.get("total_profit") if period["status"] == "complete" else None
            metadata["headline_return"] = period.get("period_return") if period["status"] == "complete" else None
        if "risk" in metadata:
            risk = metadata["risk"]
            if not isinstance(risk.get("metrics"), list) or not isinstance(risk.get("quality", []), list):
                raise ValueError("неверная структура показателей риска")
            names = set()
            for row in risk["metrics"]:
                if not isinstance(row, dict) or not isinstance(row.get("metric"), str) or row["metric"] in names:
                    raise ValueError("неверный или повторяющийся показатель риска")
                names.add(row["metric"])
                if row.get("value") is not None:
                    row["value"] = _money(row["value"], 0, row["metric"], round_excess=True)
            period = metadata.get("portfolio_period")
            if period is not None:
                from trade_exporter.services.risk import analyze_result_risk

                start, end = period.get("started_at"), period.get("requested_end") or period.get("ended_at")
                if start and end:
                    proxy = analyze_result_risk(report.daily, period.get("starting_equity") or ZERO, datetime.fromisoformat(start), datetime.fromisoformat(end), period["base_currency"])
                    risk["metrics"] = [row for row in risk["metrics"] if not row["metric"].startswith("proxy_")] + proxy["metrics"]
                    for row in proxy["metrics"]:
                        row["quality_reason"] = " ".join(filter(None, (row.get("quality_reason"), *metadata.get("commission_quality", ()), *metadata.get("result_quality", ()))))
        report.summary.update(metadata)
    except (ValueError, TypeError, pl.exceptions.PolarsError) as error:
        raise ValueError(f"Некорректные параметры расчёта в {path.name}: {error}") from error
    return report


def _clean_payload(path):
    companion = Path(path).with_suffix(".json")
    if not companion.is_file():
        raise ValueError(f"Для анализа чистого отчёта нужен JSON рядом с Excel: {companion.name}")
    payload = orjson.loads(companion.read_bytes())
    if not isinstance(payload, dict) or payload.get("kind") != "trade_portfolio_report" or payload.get("version") != 1 or not isinstance(payload.get("results"), list) or not isinstance(payload.get("summary"), dict):
        raise ValueError("Некорректный JSON отчёта")
    return payload


def load_report_portfolio(path):
    path = Path(path)
    if path.suffix.lower() != ".xlsx" or fastexcel.read_excel(path).sheet_names != ["Отчёт"]:
        return None
    payload = _clean_payload(path).get("portfolio")
    if payload is None:
        return None
    from trade_exporter.domain.portfolio import PortfolioReport, PortfolioSnapshot
    from trade_exporter.services.portfolio import analyze_portfolio

    template = analyze_portfolio(PortfolioSnapshot("schema", "finam", datetime.now(UTC), "RUB", ZERO))

    def frame(name):
        records = payload.get(name)
        if records is None:
            return None
        schema = getattr(template, name if name != "period_flows" else "flows").schema
        rows = [dict(row) for row in records]
        for row in rows:
            for column, dtype in schema.items():
                value = row.get(column)
                if value is None:
                    continue
                if isinstance(dtype, pl.Decimal):
                    row[column] = Decimal(str(value))
                elif isinstance(dtype, pl.Datetime):
                    row[column] = datetime.fromisoformat(value)
        return pl.DataFrame(rows, schema=schema)

    return PortfolioReport(payload["summary"], *(frame(name) for name in ("positions", "allocation", "cash", "flows", "period_flows")))
