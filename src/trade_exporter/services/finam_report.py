from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

import fastexcel
import polars as pl


MARGIN_SCHEMA = {
    "date": pl.Date,
    "ticker": pl.String,
    "gross_pnl": pl.Decimal(precision=38, scale=18),
    "currency": pl.String,
    "source_id": pl.String,
    "source_file": pl.String,
}
ALIASES = {
    "date": {"дата", "дата операции", "дата начисления", "торговая дата", "дата расчетов"},
    "ticker": {"тикер", "инструмент", "код инструмента", "контракт", "наименование контракта"},
    "currency": {"валюта", "валюта расчетов", "валюта операции"},
    "operation": {"операция", "вид операции", "наименование операции", "содержание операции"},
    "source_id": {"id", "идентификатор операции", "номер операции", "№ операции", "номер транзакции", "№ транзакции"},
    "amount": {"сумма", "сумма операции"},
    "credit": {"зачислено", "получено", "сумма зачисления"},
    "debit": {"списано", "уплачено", "сумма списания"},
}
RUB = {"rub", "rur", "руб", "руб.", "рубль", "рубли", "рублей", "₽"}
TOTALS = {"итого", "всего", "итого:", "всего:", "total", "subtotal"}


def _text(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()) if value is not None else ""


def _label(value) -> str:
    return _text(value).casefold().replace("ё", "е")


def _currency(value) -> str:
    value = _text(value)
    if _label(value) in RUB:
        return "RUB"
    if re.fullmatch(r"[A-Za-z]{3}", value):
        return value.upper()
    raise ValueError(f"Неизвестная валюта вариационной маржи: {value!r}")


def _margin_label(value) -> tuple[bool, str | None]:
    value = _label(value)
    match = re.fullmatch(r"вариационная маржа(?:\s*[,;(]?\s*(rub|rur|руб\.?|рублей|₽)\s*\)?)?", value)
    return (True, "RUB" if match.group(1) else None) if match else (False, None)


def _amount(value) -> Decimal:
    raw = _text(value).replace(" ", "").replace("−", "-")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        number = Decimal(raw.replace(",", "."))
        with localcontext() as context:
            context.prec = 80
            normalized = number.quantize(Decimal("1e-18"))
        if not number.is_finite() or normalized != number or number.copy_abs() >= Decimal("1e20"):
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Некорректная сумма вариационной маржи: {value!r}") from error
    return normalized


def _day(value) -> date:
    value = _text(value)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d.%m.%y":
                parsed = parsed.replace(year=2000 + int(value[-2:]))
            return parsed.date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as error:
        raise ValueError(f"Некорректная дата вариационной маржи: {value!r}") from error


class _HTMLTables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.stack = []
        self.document_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None, "spans": {}, "col": 0})
        elif self.stack:
            table = self.stack[-1]
            if tag == "tr":
                table["row"] = []
                table["col"] = 0
            elif tag in {"td", "th"} and table["row"] is not None:
                attributes = dict(attrs)
                table["cell"] = [[], int(attributes.get("colspan", 1)), int(attributes.get("rowspan", 1))]
            elif tag == "br" and table["cell"] is not None:
                table["cell"][0].append(" ")

    def handle_data(self, data):
        self.document_text.append(data)
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"][0].append(data)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        table = self.stack[-1]
        if tag in {"td", "th"} and table["cell"] is not None:
            while table["col"] in table["spans"]:
                text, remaining = table["spans"][table["col"]]
                table["row"].append(text)
                if remaining == 1:
                    del table["spans"][table["col"]]
                else:
                    table["spans"][table["col"]] = (text, remaining - 1)
                table["col"] += 1
            fragments, colspan, rowspan = table["cell"]
            for offset in range(colspan):
                value = _text("".join(fragments)) if offset == 0 else ""
                table["row"].append(value)
                if rowspan > 1:
                    table["spans"][table["col"]] = (value, rowspan - 1)
                table["col"] += 1
            table["cell"] = None
        elif tag == "tr" and table["row"] is not None:
            while table["spans"] and table["col"] <= max(table["spans"]):
                if table["col"] in table["spans"]:
                    text, remaining = table["spans"][table["col"]]
                    table["row"].append(text)
                    if remaining == 1:
                        del table["spans"][table["col"]]
                    else:
                        table["spans"][table["col"]] = (text, remaining - 1)
                else:
                    table["row"].append("")
                table["col"] += 1
            table["rows"].append(table["row"])
            table["row"] = None
        elif tag == "table":
            self.tables.append(self.stack.pop()["rows"])


def _tables(content: bytes, source_name: str):
    lead = content[:2048].lstrip()
    if b"<html" in lead.lower() or b"<table" in lead.lower() or b"<!doctype html" in lead.lower():
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        else:
            raise ValueError(f"Не удалось прочитать кодировку HTML: {source_name}")
        parser = _HTMLTables()
        parser.feed(text)
        return [("document", [[" ".join(parser.document_text)]])] + [
            (f"table-{index}", rows) for index, rows in enumerate(parser.tables, 1)
        ]
    if content.startswith(b"PK"):
        try:
            with ZipFile(BytesIO(content)) as archive:
                if "xl/workbook.xml" not in archive.namelist():
                    members = [
                        info for info in archive.infolist()
                        if not info.is_dir() and Path(info.filename).suffix.lower() in {".xlsx", ".xls", ".html", ".htm"}
                    ]
                    if not members:
                        raise ValueError(f"ZIP не содержит поддерживаемого отчета: {source_name}")
                    tables = []
                    for member in members:
                        if member.file_size > 100_000_000:
                            raise ValueError(f"Слишком большой отчет в ZIP: {source_name}")
                        tables.extend(
                            (f"{member.filename}:{name}", rows)
                            for name, rows in _tables(archive.read(member), member.filename)
                        )
                    return tables
        except BadZipFile as error:
            raise ValueError(f"Поврежденный XLSX/ZIP: {source_name}") from error
    try:
        workbook = fastexcel.read_excel(content)
        return [
            (
                name,
                workbook.load_sheet(name, header_row=None, dtypes="string").to_polars().rows(),
            )
            for name in workbook.sheet_names
        ]
    except Exception as error:
        raise ValueError(f"Неподдерживаемый формат отчета Финама: {source_name}") from error


@dataclass(frozen=True)
class _Columns:
    fields: dict[str, int]
    currency: str | None
    operation_rows: bool
    ambiguous: tuple[str, ...] = ()


def _columns(row, section: bool, section_currency: str | None) -> _Columns | None:
    fields = {}
    duplicate = set()
    currency = section_currency
    for index, value in enumerate(row):
        margin, header_currency = _margin_label(value)
        label = _label(value)
        role = "amount" if margin else next((key for key, aliases in ALIASES.items() if label in aliases), None)
        if not role:
            continue
        if role in fields:
            duplicate.add(role)
        fields[role] = index
        if margin:
            fields["margin_header"] = index
            currency = header_currency or currency
    explicit = section or "margin_header" in fields
    operation_rows = not explicit and "operation" in fields
    monetary = "amount" in fields or {"credit", "debit"} <= fields.keys()
    if not {"date", "ticker"} <= fields.keys() or not monetary or not (explicit or operation_rows):
        return None
    if duplicate and explicit:
        raise ValueError("Неоднозначные колонки вариационной маржи: " + ", ".join(sorted(duplicate)))
    if "currency" not in fields and currency is None and explicit:
        raise ValueError("В таблице вариационной маржи отсутствует явно указанная валюта")
    return _Columns(fields, currency, operation_rows, tuple(sorted(duplicate)))


def _parse_tables(tables, source_name: str):
    records = []
    recognized = False
    for name, rows in tables:
        table_start = len(records)
        nonzero_total = False
        section = False
        section_currency = None
        columns = None
        for row_number, row in enumerate(rows, 1):
            values = [_text(value) for value in row]
            nonempty = [value for value in values if value]
            if not nonempty:
                continue
            margin, currency = _margin_label(nonempty[0])
            if len(nonempty) == 1 and margin:
                section, section_currency, columns = True, currency, None
                continue
            try:
                candidate = _columns(values, section, section_currency)
            except ValueError as error:
                raise ValueError(f"{source_name}, {name}, строка {row_number}: {error}") from error
            if candidate is not None:
                columns = candidate
                recognized |= not candidate.operation_rows
                continue
            if columns is None:
                continue
            fields = columns.fields
            cell = lambda key: values[fields[key]] if key in fields and fields[key] < len(values) else ""
            if _label(nonempty[0]) in TOTALS or re.match(r"^(итого|всего)(?:\s|:|\()", _label(nonempty[0])):
                if not columns.operation_rows:
                    for role in ("amount", "credit", "debit"):
                        if cell(role) and _amount(cell(role)) != 0:
                            nonzero_total = True
                continue
            if columns.operation_rows and not _margin_label(cell("operation"))[0]:
                continue
            if len(nonempty) == 1 and not re.match(r"^\d", nonempty[0]):
                section, section_currency, columns = False, None, None
                continue
            try:
                if columns.ambiguous:
                    raise ValueError("Неоднозначные колонки вариационной маржи: " + ", ".join(columns.ambiguous))
                day = _day(cell("date"))
                ticker = cell("ticker")
                if not ticker:
                    raise ValueError("Отсутствует инструмент вариационной маржи")
                row_currency = _currency(cell("currency")) if cell("currency") else columns.currency
                if row_currency is None:
                    raise ValueError("Отсутствует валюта вариационной маржи")
                if columns.currency and row_currency != columns.currency:
                    raise ValueError("Валюта строки противоречит валюте заголовка")
                if "amount" in fields:
                    amount = _amount(cell("amount"))
                else:
                    credit = _amount(cell("credit") or "0")
                    debit = _amount(cell("debit") or "0")
                    if credit < 0 or debit < 0:
                        raise ValueError("Колонки зачисления и списания должны быть неотрицательными")
                    with localcontext() as context:
                        context.prec = 80
                        amount = credit - debit
                explicit_id = cell("source_id")
                records.append(({
                    "date": day,
                    "ticker": ticker,
                    "gross_pnl": amount,
                    "currency": row_currency,
                    "source_id": explicit_id or f"{source_name}:{name}:{row_number}",
                    "source_file": source_name,
                }, bool(explicit_id)))
                recognized = True
            except ValueError as error:
                raise ValueError(f"{source_name}, {name}, строка {row_number}: {error}") from error
        if nonzero_total and len(records) == table_start:
            raise ValueError(f"{source_name}, {name}: ненулевой итог ВМ без датированных строк")
    if not recognized:
        raise ValueError(
            f"{source_name}: не найдена явная таблица вариационной маржи с датой, "
            "инструментом, суммой и валютой. Исходный отчет сохранен; "
            "финрез из оборота или цены фьючерса не вычисляется."
        )
    return records


def _report_day(tables, supplied: date | None, source_name: str) -> date | None:
    text = " ".join(_text(cell) for _, rows in tables for row in rows for cell in row)
    periods = re.findall(
        r"за\s+период\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    for start, end in periods:
        if start != end:
            raise ValueError(f"{source_name}: для сверки фактической и плановой ВМ нужен однодневный отчет")
        if supplied is not None and _day(start) != supplied:
            raise ValueError(f"{source_name}: дата отчета противоречит запрошенной дате")
        supplied = _day(start)
    return supplied


def _position_title(value) -> bool:
    return _label(value).startswith("оценка позиции по производным")


def _position_columns(history, section_currency: str | None):
    width = max((len(row) for row in history), default=0)
    fields = {}
    currency = section_currency
    group = ""
    for index in range(width):
        labels = [_label(row[index]) for row in history if index < len(row) and _text(row[index])]
        for label in labels:
            if label in ALIASES["ticker"]:
                if "ticker" in fields and fields["ticker"] != index:
                    raise ValueError("Неоднозначная колонка инструмента в таблице ПФИ")
                fields["ticker"] = index
            if label in ALIASES["currency"]:
                fields["currency"] = index
        combined = re.sub(r"\bвм\b", "вариационная маржа", " ".join(dict.fromkeys(labels)))
        if "вариационная маржа" in combined:
            group = "вариационная маржа"
        elif labels and any("маржа" in label or "позици" in label or "обеспечен" in label for label in labels):
            group = ""
        margin = "вариационная маржа" in combined or group == "вариационная маржа"
        if not margin:
            continue
        if re.search(r"\b(?:rub|rur|руб(?:лей)?)(?:\b|\.)|₽", combined):
            currency = "RUB"
        paid = re.search(r"\bфакт(?:ическая)?\b|получено\s*/\s*уплачено|полученная\s*/\s*уплаченная", combined)
        pending = re.search(r"\bплан(?:овая)?\b|начисленная|к получению\s*/\s*уплате", combined)
        if paid and pending:
            continue
        role = "paid" if paid else "pending" if pending else None
        if role:
            if role in fields:
                raise ValueError("Неоднозначные колонки фактической/плановой ВМ")
            fields[role] = index
    if not {"ticker", "paid", "pending"} <= fields.keys():
        return None
    if "currency" not in fields and currency is None:
        raise ValueError("В таблице ПФИ отсутствует явно указанная валюта ВМ")
    return fields, currency


def _positions(tables, source_name: str):
    recognized = False
    positions = []
    for name, rows in tables:
        table_start = len(positions)
        nonzero_total = False
        section = False
        currency = None
        columns = None
        history = []
        for row_number, row in enumerate(rows, 1):
            values = [_text(value) for value in row]
            nonempty = [value for value in values if value]
            if not nonempty:
                continue
            if any(_position_title(value) for value in nonempty) and len(nonempty) <= 2:
                section, columns, history = True, None, []
                currency = "RUB" if re.search(r"\b(?:rub|rur|руб(?:лей)?)(?:\b|\.)|₽", " ".join(nonempty), flags=re.IGNORECASE) else None
                continue
            if not section:
                continue
            if columns is None:
                history.append(values)
                history = history[-3:]
                try:
                    candidate = _position_columns(history, currency)
                except ValueError as error:
                    raise ValueError(f"{source_name}, {name}, строка {row_number}: {error}") from error
                if candidate is not None:
                    columns = candidate
                    recognized = True
                elif len(nonempty) == 1 and _label(nonempty[0]).startswith(("оценка ", "го срочного", "торговые движения")):
                    section = False
                continue
            fields, header_currency = columns
            cell = lambda key: values[fields[key]] if key in fields and fields[key] < len(values) else ""
            if _label(nonempty[0]) in TOTALS or re.match(r"^(итого|всего)(?:\s|:|\()", _label(nonempty[0])):
                if any(cell(role) and _amount(cell(role)) != 0 for role in ("paid", "pending")):
                    nonzero_total = True
                continue
            if len(nonempty) == 1 and not cell("paid") and not cell("pending"):
                section, columns, history = False, None, []
                continue
            try:
                ticker = cell("ticker")
                if not ticker:
                    raise ValueError("Отсутствует инструмент ВМ в таблице ПФИ")
                row_currency = _currency(cell("currency")) if cell("currency") else header_currency
                if not row_currency:
                    raise ValueError("Отсутствует валюта ВМ в таблице ПФИ")
                if header_currency and row_currency != header_currency:
                    raise ValueError("Валюта строки противоречит валюте заголовка")
                positions.append({
                    "ticker": ticker,
                    "currency": row_currency,
                    "paid": _amount(cell("paid")),
                    "pending": _amount(cell("pending")),
                    "source_file": source_name,
                    "source_id": f"{source_name}:{name}:{row_number}",
                })
            except ValueError as error:
                raise ValueError(f"{source_name}, {name}, строка {row_number}: {error}") from error
        if nonzero_total and len(positions) == table_start:
            raise ValueError(f"{source_name}, {name}: ненулевой итог ВМ без строк инструментов")
    return positions if recognized else None


def parse_finam_margins(
    paths: list[Path], *, report_dates: Mapping[Path, date] | None = None
) -> pl.DataFrame:
    result = []
    maximum = Counter()
    identified = {}
    daily_positions = {}
    for path in paths:
        path = Path(path)
        source_name = str(path)
        tables = _tables(path.read_bytes(), source_name)
        positions = _positions(tables, source_name)
        if positions is not None:
            day = _report_day(tables, (report_dates or {}).get(path), source_name)
            if day is None:
                raise ValueError(f"{source_name}: не указана дата однодневного отчета ПФИ")
            snapshot = {}
            for position in positions:
                key = (position["ticker"], position["currency"])
                if key in snapshot:
                    raise ValueError(f"{source_name}: повторный инструмент в оценке ПФИ: {key[0]}")
                snapshot[key] = position
            if day in daily_positions:
                previous = {key: (value["paid"], value["pending"]) for key, value in daily_positions[day].items()}
                current = {key: (value["paid"], value["pending"]) for key, value in snapshot.items()}
                if previous != current:
                    raise ValueError(f"{source_name}: противоречивые однодневные отчеты ПФИ за {day}")
            else:
                daily_positions[day] = snapshot
            continue
        records = _parse_tables(tables, source_name)
        seen = Counter()
        for record, explicit_id in records:
            value = tuple(record[key] for key in ("date", "ticker", "currency", "gross_pnl"))
            key = ("id", record["source_id"]) if explicit_id else ("row", *value)
            if explicit_id:
                if key in identified and identified[key] != value:
                    raise ValueError(f"Противоречивые данные операции {record['source_id']!r}: {source_name}")
                identified[key] = value
            seen[key] += 1
            if seen[key] > maximum[key]:
                result.append(record)
        maximum |= seen
    pending = {}
    for day, snapshot in sorted(daily_positions.items()):
        missing = {key for key, value in pending.items() if value != 0} - snapshot.keys()
        if missing:
            raise ValueError(f"Отчет ПФИ за {day}: отсутствуют данные по ранее начисленной ВМ: " + ", ".join(key[0] for key in sorted(missing)))
        for key, position in snapshot.items():
            with localcontext() as context:
                context.prec = 80
                amount = position["paid"] + position["pending"] - pending.get(key, Decimal("0"))
            result.append({
                "date": day,
                "ticker": position["ticker"],
                "gross_pnl": amount,
                "currency": position["currency"],
                "source_id": position["source_id"],
                "source_file": position["source_file"],
            })
        pending = {key: position["pending"] for key, position in snapshot.items()}
    return pl.DataFrame(result, schema=MARGIN_SCHEMA).sort("date", maintain_order=True)
