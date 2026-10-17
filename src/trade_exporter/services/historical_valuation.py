from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from trade_exporter.brokers.finam_reports import FinamReports
from trade_exporter.services.finam_report import _label, _tables


TOTAL = "итого оценка состояния счета (руб.)"
COMPONENTS = frozenset({
    "оценка рублевой позиции (руб.)",
    "оценка валютной позиции (руб.)",
    "оценка позиции по немаржируемым пфи (руб.)",
    "оценка позиции по ценным бумагам и иностранным финансовым инструментам, не квалифицированным в качестве ценных бумаг (руб.)",
    "оценка позиции по драгоценным металлам (руб.)",
    "оценка требований и обязательств по оплате по незавершенным сделкам (руб.)",
    "оценка плановых позиций по незавершенным сделкам (руб.)",
})
EXCLUDED_GO = "оценка позиции по пфи (руб.) (го, не учитывается в итоговой оценке состояния счета)"
ROUNDING_TOLERANCE = Decimal("0.05")
SUFFIXES = {".html", ".htm", ".xls", ".xlsx", ".zip"}


@dataclass(frozen=True, slots=True)
class HistoricalValuation:
    equity: Decimal
    currency: str
    as_of: datetime
    source_file: Path
    quality: tuple[str, ...] = ()


def _number(value) -> Decimal:
    raw = "".join(str(value).split()).replace("−", "-").replace(",", ".")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        number = Decimal(raw)
        if not number.is_finite() or abs(number) >= Decimal("1e20"):
            raise InvalidOperation
        with localcontext() as context:
            context.prec = 80
            normalized = number.quantize(Decimal("1e-18"))
        if normalized != number:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise ValueError("ФИНАМ: некорректная денежная сумма в исторической оценке") from error
    return normalized


def _date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError as error:
        raise ValueError("ФИНАМ: некорректная дата исторического отчёта") from error


def _identity(tables, account_id: str) -> None:
    identities = set()
    for name, rows in tables:
        if name == "document":
            continue
        for row in rows:
            text = " ".join(_label(cell) for cell in row)
            match = re.fullmatch(r"по счету\s*:\s*(?:клф\s*[-–—]\s*)?([0-9]+)", text.strip())
            if match:
                identities.add(str(int(match.group(1))))
    if identities != {account_id}:
        raise ValueError("ФИНАМ: исторический отчёт не подтверждает выбранный счёт")


def _header_day(tables, day: date) -> None:
    ranges = set()
    for name, rows in tables:
        if name == "document" or not any(table_name == "document" for table_name, _ in tables):
            text = " ".join(_label(cell) for row in rows for cell in row)
            ranges.update(
                (_date(first), _date(last))
                for first, last in re.findall(r"за период\s+[сc]\s*(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", text)
            )
    if ranges and ranges != {(day, day)}:
        raise ValueError("ФИНАМ: исторический отчёт должен охватывать только выбранный день")


def _parse(path: Path, account_id: str, day: date) -> HistoricalValuation:
    tables = list(_tables(path.read_bytes(), path.name))
    _identity(tables, account_id)
    _header_day(tables, day)
    valuations = []
    for name, rows in tables:
        if name == "document":
            continue
        totals = [index for index, row in enumerate(rows) if row and _label(row[0]) == TOTAL]
        if not totals:
            continue
        if len(totals) != 1:
            raise ValueError("ФИНАМ: неоднозначный итог исторической оценки")
        total_index = totals[0]
        headers = []
        for index, row in enumerate(rows[:total_index]):
            beginning = []
            ending = []
            for column, value in enumerate(row):
                label = _label(value)
                begin = re.fullmatch(r"на начало периода\s+(\d{2}\.\d{2}\.\d{4})", label)
                end = re.fullmatch(r"на конец периода\s+(\d{2}\.\d{2}\.\d{4})", label)
                if begin:
                    beginning.append((column, _date(begin.group(1))))
                if end:
                    ending.append((column, _date(end.group(1))))
            if beginning or ending:
                if len(beginning) != 1 or len(ending) != 1 or beginning[0][1] != day or ending[0][1] != day:
                    raise ValueError("ФИНАМ: даты колонок не подтверждают однодневную историческую оценку")
                headers.append((index, ending[0][0]))
        if len(headers) != 1:
            raise ValueError("ФИНАМ: не определена колонка оценки на конец выбранного дня")
        header_index, column = headers[0]
        components = {}
        for row in rows[header_index + 1:total_index]:
            if not row or not any(_label(value) for value in row):
                continue
            label = _label(row[0])
            if label == EXCLUDED_GO:
                continue
            if label not in COMPONENTS:
                raise ValueError("ФИНАМ: неизвестная компонента исторической оценки счёта")
            if label in components or column >= len(row):
                raise ValueError("ФИНАМ: неполные или повторяющиеся компоненты исторической оценки")
            components[label] = _number(row[column])
        if set(components) != COMPONENTS or column >= len(rows[total_index]):
            raise ValueError("ФИНАМ: историческая оценка не содержит полного состава капитала")
        equity = _number(rows[total_index][column])
        with localcontext() as context:
            context.prec = 80
            residual = sum(components.values(), Decimal(0)) - equity
        if abs(residual) > ROUNDING_TOLERANCE:
            raise ValueError("ФИНАМ: итог исторической оценки не сходится с денежными компонентами")
        quality = (
            "Оценка на конец календарного дня брокерского отчёта; внутридневное время не предоставлено.",
            "ГО исключено из суммы компонент; итог уже учитывает требования и плановые позиции незавершённых сделок.",
        )
        if residual:
            quality += (f"Округление опубликованных компонент: расхождение {residual} RUB, допуск {ROUNDING_TOLERANCE} RUB.",)
        valuations.append(HistoricalValuation(
            equity, "RUB", datetime.combine(day + timedelta(days=1), time.min, timezone.utc) - timedelta(microseconds=1), path, quality,
        ))
    if len(valuations) != 1:
        raise ValueError("ФИНАМ: не найден единственный итог оценки состояния счёта в RUB")
    return valuations[0]


def load_historical_valuation(broker, account, day: date, output_dir: str | Path) -> HistoricalValuation:
    if getattr(broker, "name", None) != "finam":
        raise ValueError("Историческая оценка портфеля этого брокера пока не поддерживается")
    if not isinstance(day, date) or isinstance(day, datetime) or day >= datetime.now(ZoneInfo("Europe/Moscow")).date():
        raise ValueError("Для исторической оценки требуется прошедшая календарная дата")
    if str(broker.settings.get("base_currency", "RUB")).upper() not in {"RUB", "RUR"}:
        raise ValueError("Историческая оценка ФИНАМ поддерживается только в RUB")
    account_id = FinamReports._account_id(account.id)
    cache_dir = Path(output_dir) / "historical_valuation" / sha256(account_id.encode()).hexdigest()[:16] / day.isoformat()
    cached = []
    if cache_dir.is_dir():
        for path in sorted(cache_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("~$") or path.suffix.lower() not in SUFFIXES:
                continue
            try:
                cached.append(_parse(path, account_id, day))
            except (ValueError, OSError):
                continue
    if cached and len({value.equity for value in cached}) == 1:
        return cached[-1]
    start = datetime.combine(day, time.min, timezone.utc)
    paths = FinamReports(broker, cache_dir).download(account.id, start, start + timedelta(days=1), chunk_days=1)
    if len(paths) != 1:
        raise ValueError("ФИНАМ: получено неоднозначное число однодневных исторических отчётов")
    return _parse(Path(paths[0]), account_id, day)
