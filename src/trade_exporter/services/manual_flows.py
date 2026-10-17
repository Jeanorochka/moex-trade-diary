from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

from trade_exporter.brokers.portfolio import CashFlowHistory
from trade_exporter.domain.portfolio import CashFlow


MANUAL_QUALITY = "Ручной журнал: пользователь указал все внешние движения; даты учитываются на конец дня"


def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
        if isinstance(value, bool) or not amount.is_finite() or amount == 0 or amount.copy_abs() >= Decimal("1e20"):
            raise ValueError
        if amount.as_tuple().exponent < -18:
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            if amount.quantize(Decimal("1e-18")) != amount:
                raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Сумма движения должна быть ненулевой, конечной, меньше 1e20 по модулю и не длиннее 18 дробных знаков") from error
    return amount


@dataclass(frozen=True, slots=True)
class ManualCashFlow:
    day: date
    amount: Decimal

    def __post_init__(self):
        if not isinstance(self.day, date) or isinstance(self.day, datetime):
            raise ValueError("Для ручного движения требуется календарная дата")
        object.__setattr__(self, "amount", _amount(self.amount))


def _day(raw: str) -> date:
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), pattern).date()
        except ValueError:
            pass
    raise ValueError("Дата движения должна быть в формате дд.мм.гггг, дд.мм.гг или гггг-мм-дд")


def parse_cash_flows(values: Iterable[str] | None) -> tuple[ManualCashFlow, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bool)):
        raise ValueError("Ручные движения должны быть списком строк дата:сумма")
    entries = []
    try:
        iterator = iter(values)
    except TypeError as error:
        raise ValueError("Ручные движения должны быть списком строк дата:сумма") from error
    for index, raw in enumerate(iterator, 1):
        try:
            if not isinstance(raw, str) or raw.count(":") != 1:
                raise ValueError("Используй формат дд.мм.гггг:+1000.00 или дд.мм.гггг:-1000.00")
            day, amount = raw.split(":")
            normalized = "".join(amount.split()).replace(",", ".").replace("−", "-")
            entries.append(ManualCashFlow(_day(day), _amount(normalized)))
        except ValueError as error:
            raise ValueError(f"Ручное движение {index}: {error}") from error
    return tuple(entries)


def _aware(value, field):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}: требуется дата с часовым поясом")
    return value.astimezone(timezone.utc)


def manual_cash_flow_history(
    entries: Iterable[ManualCashFlow],
    started_at: datetime,
    captured_at: datetime,
    base_currency: str,
) -> CashFlowHistory:
    start = _aware(started_at, "started_at")
    end = _aware(captured_at, "captured_at")
    if start >= end:
        raise ValueError("Начало ручного журнала должно быть раньше конечной оценки")
    if not isinstance(base_currency, str) or not re.fullmatch(r"[A-Z]{3}", base_currency.strip().upper()):
        raise ValueError("Для ручного журнала требуется трёхбуквенная валюта счёта")
    currency = base_currency.strip().upper()
    local_timezone = started_at.tzinfo
    first_day = started_at.date()
    last_day = captured_at.astimezone(local_timezone).date()
    flows = []
    clamped = False
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, ManualCashFlow):
            raise ValueError("Ручной журнал должен содержать ManualCashFlow")
        if not first_day <= entry.day <= last_day:
            raise ValueError("Дата ручного движения находится вне выбранного периода")
        timestamp = datetime.combine(entry.day, time.max, local_timezone).astimezone(timezone.utc)
        if timestamp > end:
            timestamp = end
            clamped = True
        if timestamp <= start:
            raise ValueError("Ручное движение должно быть позже начальной оценки")
        flows.append(CashFlow(timestamp, entry.amount, currency, "external", f"manual:{index:08d}"))
    flows.sort(key=lambda flow: flow.timestamp)
    reasons = (MANUAL_QUALITY,)
    if clamped:
        reasons += ("Для даты конечной оценки движения учтены на момент оценки, поскольку внутридневное время не указано.",)
    return CashFlowHistory(tuple(flows), True, reasons, start)
