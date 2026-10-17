from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

import orjson
import polars as pl

from trade_exporter.exporters.excel import write_workbook

if TYPE_CHECKING:
    from trade_exporter.domain.portfolio import PortfolioReport


ASSETS = {
    "share": "Акции", "stock": "Акции", "stocks": "Акции", "shares": "Акции", "equity": "Акции", "equities": "Акции",
    "future": "Фьючерсы", "futures": "Фьючерсы", "bond": "Облигации",
    "etf": "Фонды", "fund": "Фонды", "funds": "Фонды", "currency": "Валюта", "cash": "Деньги",
    "option": "Опционы", "options": "Опционы", "other": "Другие активы", "unknown": "Другие активы",
}
STATUSES = {
    "broker": "Оценка брокера", "calculated": "По текущей котировке",
    "stale_quote": "Дата котировки требует уточнения", "missing_price": "Нет текущей цены",
    "unsupported_price_basis": "Денежная оценка не определена", "foreign_currency": "В исходной валюте",
    "pending_quantity": "Включены ожидающие исполнения заявки",
}
HEADERS = {
    "instrument_id": "инструмент", "ticker": "тикер", "asset_type": "тип актива",
    "quantity": "количество", "average_price": "средняя цена покупки", "current_price": "текущая цена",
    "price_currency": "валюта текущей цены", "average_price_currency": "валюта цены покупки",
    "market_value": "текущая стоимость", "cost_basis": "себестоимость открытой позиции",
    "unrealized_pnl": "нереализованный результат", "valuation_currency": "валюта оценки",
    "unrealized_return": "доходность открытой позиции", "weight": "доля вложений",
    "equity_weight": "доля капитала", "long_exposure": "длинная позиция",
    "short_exposure": "короткая позиция", "price_as_of": "дата котировки",
    "valuation_source": "источник оценки", "unrealized_source": "источник результата",
    "valuation_status": "основание оценки", "long_market_value": "стоимость длинных позиций",
    "short_market_value": "стоимость коротких позиций", "gross_exposure": "валовые вложения",
    "long_weight": "доля длинных позиций", "short_weight": "доля коротких позиций",
    "currency": "валюта", "amount": "сумма", "amount_base": "сумма в валюте отчета",
    "timestamp": "время", "kind": "вид операции", "id": "идентификатор",
    "price_status": "состояние котировки",
    "quantity_includes_pending_orders": "количество включает ожидающие заявки",
}
SUMMARY = {
    "account_id": "Счет", "broker": "Брокер", "captured_at": "Состояние на",
    "base_currency": "Валюта отчета", "equity": "Капитал сейчас", "cash_base": "Деньги",
    "known_cash_base": "Деньги с известной оценкой", "positions_market_value": "Стоимость позиций",
    "known_positions_market_value": "Известная стоимость позиций", "valuation_residual": "Разница со сводной оценкой брокера",
    "positions_count": "Открытых позиций", "priced_positions_count": "Позиций с денежной оценкой",
    "unvalued_positions_count": "Позиций без денежной оценки", "long_market_value": "Длинные позиции",
    "short_market_value": "Короткие позиции", "gross_exposure": "Валовые вложения",
    "unrealized_pnl": "Нереализованный результат", "known_unrealized_pnl": "Известный нереализованный результат",
    "unrealized_cost_basis": "Себестоимость сопоставимых позиций", "holdings_return": "Доходность открытых позиций",
    "total_external_inflows": "Внесено на счет", "total_external_outflows": "Выведено со счета",
    "net_external_funding": "Чистые внесения", "lifetime_profit": "Результат за всю историю",
    "profit_to_net_funding_ratio": "Отношение прибыли к чистым взносам", "xirr": "Среднегодовая доходность XIRR",
    "ledger_complete": "Полнота истории движений", "ledger_start": "Начало истории",
    "income": "Доходные выплаты", "fees": "Комиссии", "taxes": "Налоги", "source": "Источник",
    "valuation_complete": "Полнота оценки позиций", "valuation_residual_basis": "Состав разницы оценки",
    "positions_include_pending_orders": "Включены ожидающие исполнения заявки",
    "equity_authoritative": "Капитал предоставлен брокером", "unrealized_complete": "Полнота нереализованного результата",
    "returns_complete": "Полнота данных для доходности", "allocation_complete": "Полнота структуры вложений",
    "xirr_status": "Основание расчета XIRR",
    "flows_currency_complete": "Движения приведены к валюте отчета",
    "stale_quotes_count": "Позиций с устаревшей котировкой",
    "unknown_quote_times_count": "Позиций без даты котировки",
    "external_flows_currency_complete": "Внешние движения приведены к валюте отчета",
    "holdings_return_basis": "Основание доходности открытых позиций",
    "valuation_components_complete": "Полнота оценки состава портфеля",
    "valuation_reconciled": "Сверка состава со стоимостью капитала",
    "valuation_reconciliation_status": "Основание сверки капитала",
    "valuation_residual_threshold": "Точность сверки капитала",
    "funding_status": "Подтверждение исходного капитала",
    "cash_flow_error": "Причина недоступности истории движений",
}
PERCENTAGES = {"unrealized_return", "weight", "equity_weight", "long_weight", "short_weight", "holdings_return", "profit_to_net_funding_ratio", "xirr"}
VALUES = {
    "unique_bracketed_root": "Однозначно рассчитана", "multiple_roots": "Несколько возможных значений",
    "no_bracketed_root": "Значение в исследованном диапазоне не найдено", "same_day": "Недостаточный период наблюдений",
    "no_sign_change": "Нет денежных потоков обоих направлений", "no_inception_funding": "Исходный вклад не подтвержден",
    "incomplete_ledger_or_currency": "Неполная история или разные валюты внешних движений",
    "non_conventional_cash_flows": "Неоднозначность потоков пополнений и выводов",
    "equity_minus_known_base_currency_components": "Капитал минус известные остатки и позиции в валюте отчета",
    "non_derivative_unrealized_over_absolute_cost": "Нереализованный результат бумаг / абсолютная себестоимость",
    "reconciled": "Состав согласуется со стоимостью капитала", "unexplained_balance": "Есть разница с оценкой состава",
    "partial_components": "Денежная оценка состава доступна частично",
    "evidenced_external_funding": "Внешние внесения подтверждены", "empty_zero_equity": "Счет без капитала",
    "missing_inception_funding": "Исходные внесения не подтверждены",
}
CSS = """
:root{color-scheme:light;--ink:#14263b;--muted:#697b8f;--line:#e3eaf0;--green:#158875;--red:#bf5d6a;--paper:#f4f7fa;--white:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 "Segoe UI",Arial,sans-serif;font-variant-numeric:tabular-nums}a{color:inherit;text-decoration:none}button,input{font:inherit}button{cursor:pointer}main{max-width:1360px;margin:auto;padding:32px 36px 48px}.top{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:25px}.brand{display:flex;align-items:center;gap:11px;font-weight:650;letter-spacing:.04em}.brand-mark{background:var(--ink);color:white;display:grid;place-items:center;width:34px;height:34px;border-radius:10px;font-size:18px}.downloads{display:flex;gap:9px}.downloads a{background:white;border:1px solid var(--line);border-radius:9px;padding:8px 14px;font-size:12px;font-weight:600}.hero{background:#142b40;color:white;border-radius:20px;padding:30px 32px;display:flex;align-items:center;justify-content:space-between;gap:24px;position:relative;overflow:hidden}.hero:after{content:"";position:absolute;width:360px;height:360px;border:70px solid #ffffff05;border-radius:100%;right:80px;top:-120px;pointer-events:none}.eyebrow{text-transform:uppercase;letter-spacing:.13em;font-size:11px;font-weight:650;color:#91b8c6}.hero h1{font-size:16px;font-weight:500;margin:10px 0 5px;color:#d2e1e9}.equity{font-size:48px;line-height:1.15;font-weight:650;letter-spacing:-.04em}.hero-meta{color:#abc2cf;margin-top:14px;font-size:12px}.hero-side{text-align:right;position:relative;z-index:1}.hero-side strong{display:block;font-size:14px;font-weight:500}.hero-side span{color:#abc2cf;font-size:12px}.hero-tag{display:inline-block;background:#ffffff0d;border:1px solid #ffffff16;padding:5px 11px;border-radius:20px;font-size:11px;margin-bottom:13px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:18px 0 24px}.stat{background:white;border:1px solid var(--line);border-radius:13px;padding:18px 21px}.label{font-size:12px;color:var(--muted)}.stat strong{display:block;font-size:25px;letter-spacing:-.025em;margin:5px 0}.caption{font-size:11px;color:var(--muted)}.positive{color:var(--green)}.negative{color:var(--red)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{background:white;border:1px solid var(--line);border-radius:15px;padding:23px 25px;min-width:0}.panel h2{font-size:16px;font-weight:650;margin:0 0 3px;letter-spacing:-.015em}.sub{color:var(--muted);font-size:11px;margin:0 0 20px}.legend{display:flex;gap:17px;font-size:11px;color:var(--muted);margin:12px 0 0}.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}.green{background:#238f81}.red{background:#c66a78}.gray{background:#bdcbd7}.svg-chart{width:100%;height:auto;display:block}.empty{min-height:180px;display:grid;align-content:center;justify-items:center;text-align:center;color:var(--muted);font-size:13px}.empty span{font-size:11px;margin-top:7px}.table-panel{margin-top:20px;padding:0;overflow:hidden}.table-top{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:22px 25px 17px}.table-top .sub{margin:2px 0 0}.search{border:1px solid var(--line);border-radius:8px;background:#fafcfd;padding:9px 12px;width:230px;outline:none}.search:focus{border-color:#93b8c4}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:right;color:var(--muted);font-weight:500;font-size:10px;padding:12px 17px;background:#f8fafc;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}td{text-align:right;padding:15px 17px;border-bottom:1px solid #edf1f5;font-size:12px}th:first-child,td:first-child{text-align:left;padding-left:25px}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#fafcfe}.instrument{font-weight:650;font-size:13px}.instrument-sub{display:block;color:var(--muted);font-size:10px;font-weight:400;max-width:210px;overflow:hidden;text-overflow:ellipsis}.cell-note{display:block;color:var(--muted);font-size:10px}.table-foot{padding:12px 25px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.bottom{margin-top:20px;grid-template-columns:1.15fr .85fr}.funding{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;padding:15px 0 22px}.funding strong{font-size:20px;display:block;margin-top:5px}.profit{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line);padding-top:18px}.profit strong{font-size:25px}.meta{display:grid;grid-template-columns:1fr 1.2fr;gap:10px 18px;font-size:11px;margin-top:18px}.meta dt{color:var(--muted)}.meta dd{margin:0;text-align:right;overflow-wrap:anywhere}.footer{font-size:10px;color:#8594a4;display:flex;justify-content:space-between;gap:20px;margin-top:23px}.cash-list{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.cash-item{background:#f2f6f9;border-radius:5px;padding:3px 7px;font-size:10px;color:var(--muted)}[hidden]{display:none!important}@media(max-width:900px){main{padding:22px 18px}.grid,.bottom{grid-template-columns:1fr}.hero{padding:25px}.equity{font-size:39px}.stat{padding:15px}.stat strong{font-size:21px}}@media(max-width:560px){.top{align-items:flex-start}.downloads a{padding:7px 9px}.brand{font-size:11px}.hero-side{display:none}.hero{padding:24px}.equity{font-size:34px}.stats{grid-template-columns:1fr;gap:9px}.stat{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}.stat strong{grid-column:2;grid-row:1/3;text-align:right;margin:0;align-self:center}.caption{grid-column:1}.panel{padding:20px}.table-top{flex-direction:column;align-items:stretch}.search{width:100%}.funding{grid-template-columns:1fr;gap:12px}.funding>div{display:flex;justify-content:space-between;align-items:center}.funding strong{font-size:17px;margin:0}.footer{flex-direction:column;gap:3px}}@media print{body{background:white}main{padding:0}.downloads,.search{display:none}.panel,.stat,.hero{break-inside:avoid}.grid{grid-template-columns:1fr 1fr}.hero{-webkit-print-color-adjust:exact;print-color-adjust:exact}.table-wrap{overflow:visible}th,td{padding:8px;font-size:9px}.instrument{font-size:10px}.instrument-sub{font-size:8px}.footer{margin-top:10px}}
"""


def _fmt(value, currency=None, *, percent=False, places=2):
    if value is None:
        return "н/д"
    number = Decimal(str(value)) * (100 if percent else 1)
    text = f"{number:,.{places}f}".replace(",", " ").replace(".", ",")
    if percent:
        return text + "%"
    units = {"RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥"}
    return text + (" " + units.get(currency, currency) if currency else "")


def _quantity(value):
    if value is None:
        return "н/д"
    text = format(Decimal(str(value)), "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text).replace(".", ",")


def _tone(value):
    return "positive" if value is not None and value > 0 else "negative" if value is not None and value < 0 else ""


def _asset(value):
    return ASSETS.get(str(value).lower(), str(value or "Другие активы"))


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _summary_value(key, value):
    if value is None:
        return "н/д"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (Decimal, float)):
        return _fmt(value, percent=key in PERCENTAGES)
    return VALUES.get(str(value), str(value))


def _display(frame):
    result = frame.with_columns(
        pl.col(name).cast(pl.Float64)
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Decimal)
    )
    for name, dtype in result.schema.items():
        if dtype == pl.Date:
            result = result.with_columns(pl.col(name).dt.strftime("%d.%m.%Y"))
        elif isinstance(dtype, pl.Datetime):
            result = result.with_columns(pl.col(name).dt.strftime("%d.%m.%Y %H:%M:%S %Z"))
    if "asset_type" in result.columns:
        result = result.with_columns(pl.col("asset_type").replace(ASSETS))
    if "valuation_status" in result.columns:
        result = result.with_columns(pl.col("valuation_status").replace(STATUSES))
    if "price_status" in result.columns:
        result = result.with_columns(pl.col("price_status").replace({
            "stale": "Устаревшая котировка", "timestamp_unknown": "Дата не указана", "as_of_quote": "На указанную дату",
        }))
    if "kind" in result.columns:
        result = result.with_columns(pl.col("kind").replace({
            "external": "Внешнее движение", "income": "Доходная выплата", "fee": "Комиссия", "tax": "Налог", "other": "Прочее",
        }))
    return result.rename({name: HEADERS.get(name, name) for name in result.columns})


def _comparison(positions, currency):
    rows = [
        row for row in positions.to_dicts()
        if row.get("cost_basis") is not None and row.get("market_value") is not None
        and row.get("quantity", 0) > 0 and row["cost_basis"] >= 0 and row["market_value"] >= 0
        and "future" not in str(row.get("asset_type", "")).lower()
        and str(row.get("asset_type", "")).lower() != "cash"
        and row.get("valuation_currency") == currency
    ]
    rows.sort(key=lambda row: row["market_value"], reverse=True)
    return pl.DataFrame({
        "инструмент": [row.get("ticker") or row["instrument_id"] for row in rows],
        "себестоимость": [float(row["cost_basis"]) for row in rows],
        "стоимость сейчас": [float(row["market_value"]) for row in rows],
    }, schema={"инструмент": pl.String, "себестоимость": pl.Float64, "стоимость сейчас": pl.Float64})


def _allocation_svg(frame, currency):
    rows = frame.to_dicts()
    rows = [row for row in rows if row.get("long_market_value") is not None or row.get("short_market_value") is not None]
    if not rows:
        return '<div class="empty">Структура вложений пока не определена<span>Нужна денежная оценка позиций.</span></div>'
    maximum = max((max(abs(row.get("long_market_value") or 0), abs(row.get("short_market_value") or 0)) for row in rows), default=1) or 1
    height = 30 + len(rows) * 48
    parts = [f'<svg class="svg-chart" viewBox="0 0 620 {height}" role="img" aria-label="Длинные и короткие позиции по типам активов">', f'<line x1="350" y1="8" x2="350" y2="{height - 8}" stroke="#dce6ee"/>']
    for index, row in enumerate(rows):
        y = 28 + index * 48
        long = abs(row.get("long_market_value") or 0)
        short = abs(row.get("short_market_value") or 0)
        long_width, short_width = float(long / maximum) * 155, float(short / maximum) * 155
        parts += [
            f'<text x="0" y="{y + 5}" fill="#243c51" font-size="13">{escape(_asset(row.get("asset_type")))}</text>',
            f'<rect x="{350 - short_width:.2f}" y="{y - 8}" width="{short_width:.2f}" height="17" rx="3" fill="#c66a78"/>',
            f'<rect x="350" y="{y - 8}" width="{long_width:.2f}" height="17" rx="3" fill="#238f81"/>',
            f'<text x="615" y="{y + 5}" text-anchor="end" fill="#697b8f" font-size="11">{escape(_fmt(long - short, currency, places=0))}</text>',
        ]
    parts.append("</svg>")
    return "".join(parts)


def _comparison_svg(frame, currency):
    rows = frame.head(5).to_dicts()
    if not rows:
        return '<div class="empty">Себестоимость позиций пока не определена<span>Сравнение появится вместе с ценой покупки.</span></div>'
    maximum = max(max(row["себестоимость"], row["стоимость сейчас"]) for row in rows) or 1
    height = 15 + len(rows) * 51
    parts = [f'<svg class="svg-chart" viewBox="0 0 620 {height}" role="img" aria-label="Себестоимость и текущая стоимость открытых длинных позиций">']
    for index, row in enumerate(rows):
        y = 18 + index * 51
        parts.append(f'<text x="0" y="{y + 8}" fill="#243c51" font-size="12">{escape(row["инструмент"])}</text>')
        for offset, key, color in ((-6, "себестоимость", "#bdcbd7"), (8, "стоимость сейчас", "#238f81")):
            width = row[key] / maximum * 340
            parts.append(f'<rect x="140" y="{y + offset}" width="{width:.2f}" height="10" rx="3" fill="{color}"/>')
        parts.append(f'<text x="615" y="{y + 12}" text-anchor="end" fill="#697b8f" font-size="11">{escape(_fmt(row["стоимость сейчас"], currency, places=0))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _history(summary):
    rows = [
        row for row in summary.get("valuation_history", [])
        if row.get("equity") is not None and row.get("currency") == summary.get("base_currency")
    ]
    rows.sort(key=lambda row: datetime.fromisoformat(row["captured_at"].replace("Z", "+00:00")))
    return pl.DataFrame({
        "дата снимка": [row["captured_at"] for row in rows],
        "капитал": [float(row["equity"]) for row in rows],
        "валюта": [row["currency"] for row in rows],
    }, schema={"дата снимка": pl.String, "капитал": pl.Float64, "валюта": pl.String})


def _history_panel(summary, history):
    if history.height < 2:
        return ""
    rows = history.to_dicts()
    currency = summary.get("base_currency")
    times = [datetime.fromisoformat(row["дата снимка"].replace("Z", "+00:00")) for row in rows]
    values = [row["капитал"] for row in rows]
    low, high = min(values), max(values)
    padding = (high - low) * 0.15 if high != low else max(abs(high) * 0.05, 1)
    low, high = low - padding, high + padding
    elapsed = (times[-1] - times[0]).total_seconds() or 1
    coordinates = [
        (90 + (time - times[0]).total_seconds() / elapsed * 1050, 160 - (value - low) / (high - low) * 130)
        for time, value in zip(times, values)
    ]
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
    svg = '<svg class="svg-chart" viewBox="0 0 1200 200" role="img" aria-label="Фактические наблюдения стоимости портфеля; пополнения не исключены">'
    for ratio in (0, 0.5, 1):
        y = 160 - ratio * 130
        svg += f'<line x1="90" y1="{y}" x2="1140" y2="{y}" stroke="#e6edf2"/><text x="77" y="{y + 4}" text-anchor="end" fill="#697b8f" font-size="10">{escape(_fmt(low + ratio * (high - low), places=0))}</text>'
    svg += f'<polyline points="{points}" fill="none" stroke="#238f81" stroke-width="3" stroke-linejoin="round"/>'
    for index, (x, y) in enumerate(coordinates):
        svg += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="#238f81"><title>{escape(times[index].isoformat() + " · " + _fmt(values[index], currency))}</title></circle>'
    svg += f'<text x="90" y="190" fill="#697b8f" font-size="11">{times[0].strftime("%d.%m.%Y %H:%M")}</text><text x="1140" y="190" text-anchor="end" fill="#697b8f" font-size="11">{times[-1].strftime("%d.%m.%Y %H:%M")}</text></svg>'
    period = summary.get("observed_period")
    metrics = ""
    if period:
        metrics = '<div class="funding" style="border-top:1px solid var(--line);margin-top:12px">' + "".join(
            f'<div><span class="label">{label}</span><strong class="{_tone(period.get(key)) if key != "net_external_flows" else ""}">{escape(_fmt(period.get(key), None if percentage else currency, percent=percentage))}</strong></div>'
            for key, label, percentage in (
                ("profit", "Результат между снимками", False),
                ("modified_dietz_return", "Доходность с учетом движений", True),
                ("net_external_flows", "Чистые пополнения за период", False),
            )
        ) + '</div><div class="caption">' + (
            'Доходность рассчитана методом Модифицированного Дитца с учетом дат внешних движений.'
            if period.get("modified_dietz_return") is not None else 'Результат между снимками учитывает внешние движения.'
        ) + '</div>'
    quality = " · ".join(str(value) for value in summary.get("period_quality", []))
    return '<section class="panel" style="margin-top:20px"><h2>Наблюдения стоимости</h2><p class="sub">Сводная оценка брокера на даты снимков · пополнения и выводы не исключены</p>' + svg + metrics + (f'<p class="caption">{escape(quality)}</p>' if quality else "") + '</section>'


def _html(report, workbook_name, json_name, comparison):
    summary = report.summary
    currency = str(summary.get("base_currency") or "")
    money = lambda key: escape(_fmt(summary.get(key), currency))
    captured = str(summary.get("captured_at") or "")
    try:
        captured_time = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        captured = captured_time.strftime("%d.%m.%Y · %H:%M %Z")
    except ValueError:
        pass
    positions = report.positions.to_dicts()
    rows = []
    for position in positions:
        price_currency = position.get("price_currency")
        average_currency = position.get("average_price_currency")
        valuation_currency = position.get("valuation_currency")
        status = STATUSES.get(position.get("valuation_status"), position.get("valuation_status") or "")
        quote_date = position.get("price_as_of")
        if isinstance(quote_date, datetime):
            quote_date = quote_date.strftime("%d.%m.%Y %H:%M %Z")
        elif quote_date:
            quote_date = str(quote_date)
        rows.append(
            '<tr><td><span class="instrument">' + escape(str(position.get("ticker") or position.get("instrument_id") or ""))
            + '</span><span class="instrument-sub">' + escape(_asset(position.get("asset_type"))) + ' · ' + escape(str(position.get("instrument_id") or ""))
            + '</span></td><td>' + escape(_quantity(position.get("quantity")))
            + ('<span class="cell-note">С учетом ожидающих заявок</span>' if position.get("quantity_includes_pending_orders") else '')
            + '</td><td>' + escape(_fmt(position.get("average_price"), average_currency, places=4))
            + '</td><td>' + escape(_fmt(position.get("current_price"), price_currency, places=4))
            + ('<span class="cell-note">' + escape(str(quote_date)) + '</span>' if quote_date else '')
            + '</td><td>' + escape(_fmt(position.get("cost_basis"), valuation_currency))
            + '</td><td>' + escape(_fmt(position.get("market_value"), valuation_currency))
            + '<span class="cell-note">' + escape(status) + '</span></td><td class="' + _tone(position.get("unrealized_pnl")) + '">'
            + escape(_fmt(position.get("unrealized_pnl"), valuation_currency)) + '<span class="cell-note">'
            + escape(_fmt(position.get("unrealized_return"), percent=True)) + '</span></td><td>'
            + escape(_fmt(position.get("equity_weight"), percent=True)) + '</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="8" style="text-align:center;padding:38px;color:#697b8f">Открытых позиций нет</td></tr>')
    complete_ledger = summary.get("ledger_complete") is True
    profit = summary.get("lifetime_profit") if complete_ledger and summary.get("returns_complete", True) else None
    ratios = '<dl class="meta" style="border-top:1px solid var(--line);padding-top:16px">' + ''.join(
        f'<dt>{label}</dt><dd>{escape(_fmt(summary.get(key) if profit is not None else None, percent=True))}</dd>'
        for key, label in (("xirr", "Среднегодовая доходность (XIRR)"), ("profit_to_net_funding_ratio", "Прибыль / чистые взносы"))
    ) + '</dl>'
    funding = "".join(
        f'<div><span class="label">{label}</span><strong>{money(key)}</strong></div>'
        for key, label in (("total_external_inflows", "Внесено"), ("total_external_outflows", "Выведено"), ("net_external_funding", "Чистые внесения"))
    )
    cash = "".join(
        f'<span class="cash-item">{escape(_fmt(row.get("amount"), row.get("currency")))}</span>'
        for row in report.cash.to_dicts()
    )
    unrealized = summary.get("unrealized_pnl")
    unrealized_caption = "По открытым позициям" if unrealized is not None else "Известная часть: " + _fmt(summary.get("known_unrealized_pnl"), currency)
    metadata = [
        ("Брокер / счет", f"{summary.get('broker', '')} / {summary.get('account_id', '')}"),
        ("Источник", summary.get("source") or "н/д"),
        ("Денежная оценка", f"{summary.get('priced_positions_count', 0)} из {summary.get('positions_count', len(positions))} позиций"),
        ("История движения средств", "Полная" if complete_ledger else "Неполная"),
        ("Начало истории", summary.get("ledger_start") or "н/д"),
    ]
    if summary.get("valuation_residual") is not None:
        metadata.append(("Разница со сводной оценкой", _fmt(summary["valuation_residual"], currency)))
    if summary.get("positions_include_pending_orders"):
        metadata.append(("Состав позиций", "Включает ожидающие исполнения заявки"))
    if summary.get("valuation_reconciliation_status"):
        metadata.append(("Сверка капитала", VALUES.get(summary["valuation_reconciliation_status"], summary["valuation_reconciliation_status"])))
    if summary.get("valuation_residual_threshold") is not None:
        metadata.append(("Точность сверки", _fmt(summary["valuation_residual_threshold"], currency)))
    if summary.get("funding_status"):
        metadata.append(("Исходные внесения", VALUES.get(summary["funding_status"], summary["funding_status"])))
    meta = "".join(f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>" for key, value in metadata)
    quality = list(dict.fromkeys(str(value) for key in ("cash_flow_quality", "period_quality") for value in summary.get(key, [])))
    quality_html = '<div style="border-top:1px solid var(--line);margin-top:17px;padding-top:12px">' + ''.join(
        f'<p class="caption" style="margin:5px 0">{escape(value)}</p>' for value in quality
    ) + '</div>' if quality else ''
    cost_caption = "Открытые длинные позиции с известной себестоимостью"
    allocation_caption = "Длинные и короткие позиции" if summary.get("allocation_complete", True) else "Известная часть длинных и коротких позиций"
    if comparison.height > 5:
        cost_caption += " · 5 крупнейших по текущей стоимости"
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Портфель · {escape(str(summary.get("account_id") or ""))}</title><style>{CSS}</style></head>
<body><main><div class="top"><div class="brand"><span class="brand-mark">↗</span> МОЙ ПОРТФЕЛЬ</div><div class="downloads"><a href="{escape(workbook_name, quote=True)}">Скачать Excel</a><a href="{escape(json_name, quote=True)}">Данные JSON</a></div></div>
<section class="hero"><div><div class="eyebrow">Состояние на {escape(captured)}</div><h1>Капитал сейчас</h1><div class="equity">{money("equity")}</div><div class="hero-meta">Сводная оценка брокера · {escape(currency)}</div></div><div class="hero-side"><div class="hero-tag">Снимок портфеля</div><strong>{escape(str(summary.get("broker") or ""))}</strong><span>Счет {escape(str(summary.get("account_id") or ""))}</span></div></section>
<div class="stats"><div class="stat"><span class="label">Деньги на счете</span><strong>{money("cash_base")}</strong><span class="caption">В валюте отчета</span></div><div class="stat"><span class="label">Стоимость позиций</span><strong>{money("positions_market_value")}</strong><span class="caption">{summary.get("priced_positions_count", 0)} позиций с денежной оценкой</span></div><div class="stat"><span class="label">Нереализованный результат</span><strong class="{_tone(unrealized)}">{escape(_fmt(unrealized, currency))}</strong><span class="caption">{escape(unrealized_caption)}</span></div></div>
<div class="grid"><section class="panel"><h2>Структура вложений</h2><p class="sub">{escape(allocation_caption)} · денежная оценка в {escape(currency)}</p>{_allocation_svg(report.allocation, currency)}<div class="legend"><span><i class="dot green"></i>Длинные позиции</span><span><i class="dot red"></i>Короткие позиции</span></div></section><section class="panel"><h2>Покупка и стоимость сейчас</h2><p class="sub">{escape(cost_caption)}</p>{_comparison_svg(comparison, currency)}<div class="legend"><span><i class="dot gray"></i>Себестоимость</span><span><i class="dot green"></i>Текущая стоимость</span></div></section></div>{_history_panel(summary, _history(summary))}
<section class="panel table-panel"><div class="table-top"><div><h2>Открытые позиции</h2><p class="sub">Цена покупки, текущая оценка и результат по каждому инструменту</p></div><input class="search" type="search" id="position-search" aria-label="Найти инструмент" placeholder="Найти инструмент…"></div><div class="table-wrap"><table id="holdings"><thead><tr><th>Инструмент</th><th>Количество</th><th>Средняя покупка</th><th>Цена сейчас</th><th>Себестоимость</th><th>Стоимость сейчас</th><th>Нереализованный результат</th><th>Доля капитала</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><div class="table-foot">Нереализованный результат отражен отдельно. Капитал включает оценку брокера на дату снимка.</div></section>
<div class="grid bottom"><section class="panel"><h2>Движение капитала</h2><p class="sub">Пополнения и выводы за доступную историю счета</p><div class="funding">{funding}</div><div class="profit"><div><span class="label">Результат за всю историю</span><div class="caption">По капиталу и полной истории внешних движений</div></div><strong class="{_tone(profit)}">{escape(_fmt(profit, currency))}</strong></div>{ratios}<div class="cash-list">{cash}</div></section><section class="panel"><h2>Источники и оценка</h2><p class="sub">Основание цифр в этом снимке</p><dl class="meta">{meta}</dl>{quality_html}</section></div><div class="footer"><span>Снимок на {escape(captured)}. Цены и остатки относятся к указанной дате.</span><span>Портфель · {escape(currency)}</span></div></main>
<script>const search=document.getElementById('position-search');search.addEventListener('input',()=>{{const value=search.value.toLocaleLowerCase('ru');document.querySelectorAll('#holdings tbody tr').forEach(row=>{{row.hidden=!row.textContent.toLocaleLowerCase('ru').includes(value)}})}});</script></body></html>'''


def export_portfolio(report: PortfolioReport, output_dir: str | Path, prefix="portfolio") -> tuple[Path, Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = output_dir / f"{prefix}_{stamp}"
    workbook_path, json_path, html_path = (stem.with_suffix(extension) for extension in (".xlsx", ".json", ".html"))
    comparison = _comparison(report.positions, report.summary.get("base_currency"))
    simple_summary = {key: value for key, value in report.summary.items() if not isinstance(value, (list, dict))}
    summary_frame = pl.DataFrame({
        "показатель": [SUMMARY.get(key, key) for key in simple_summary],
        "значение": [
            _summary_value(key, value)
            for key, value in simple_summary.items()
        ],
    })
    sheets = {
        "Портфель": summary_frame,
        "Позиции": _display(report.positions),
        "Структура": _display(report.allocation),
        "Деньги": _display(report.cash),
        "Движения": _display(report.flows),
        "Покупка и сейчас": comparison,
    }
    history = _history(report.summary)
    if not history.is_empty():
        sheets["История стоимости"] = history
    period = report.summary.get("observed_period")
    if period:
        period_names = {
            "start": "Первый снимок", "end": "Последний снимок", "opening_equity": "Капитал в начале",
            "closing_equity": "Капитал в конце", "net_external_flows": "Чистые внешние движения",
            "profit": "Результат между снимками", "modified_dietz_return": "Доходность с учетом дат движений",
            "days": "Дней между снимками", "basis": "Основание",
        }
        sheets["Период наблюдений"] = pl.DataFrame({
            "показатель": [period_names.get(key, key) for key in period],
            "значение": [
                "Фактические снимки портфеля" if key == "basis" and value == "observed_snapshots"
                else "н/д" if value is None else _fmt(value, percent=key == "modified_dietz_return")
                if isinstance(value, (Decimal, float)) else str(value)
                for key, value in period.items()
            ],
        })
    quality_rows = [
        (label, str(value))
        for key, label in (("cash_flow_quality", "История денежных движений"), ("period_quality", "Период наблюдений"))
        for value in report.summary.get(key, [])
    ]
    if quality_rows:
        sheets["Основания"] = pl.DataFrame(quality_rows, schema={"раздел": pl.String, "основание": pl.String}, orient="row")
    options = {}
    for sheet_name, frame in sheets.items():
        formats = {}
        source = {name: key for key, name in HEADERS.items()}
        for column in frame.columns:
            key = source.get(column, column)
            if key in PERCENTAGES:
                formats[column] = {"num_format": "0.00%"}
            elif frame.schema[column].is_numeric():
                formats[column] = {"num_format": "#,##0.00"}
        options[sheet_name] = {"column_formats": formats, "column_widths": {"_all": 42}}
    options["Портфель"].update({"column_widths": {0: 52, 1: 42}})
    if history.height >= 2:
        end = history.height + 1
        options["История стоимости"]["charts"] = {"E2": {
            "type": "line", "title": "Наблюдения стоимости; пополнения не исключены",
            "categories_range": f"'История стоимости'!$A$2:$A${end}",
            "values_range": f"'История стоимости'!$B$2:$B${end}",
            "width": 850, "height": 420, "show_legend": False,
        }}
    if not comparison.is_empty():
        end = comparison.height + 1
        options["Покупка и сейчас"]["charts"] = {"E2": {
            "type": "bar", "title": "Себестоимость и стоимость сейчас",
            "categories_range": f"'Покупка и сейчас'!$A$2:$A${end}",
            "series": [
                {"name": "Себестоимость", "values_range": f"'Покупка и сейчас'!$B$2:$B${end}"},
                {"name": "Стоимость сейчас", "values_range": f"'Покупка и сейчас'!$C$2:$C${end}"},
            ], "width": 850, "height": 420, "legend_position": "bottom",
        }}
    if not report.allocation.is_empty():
        end = report.allocation.height + 1
        columns = report.allocation.columns
        if {"long_market_value", "short_market_value"} <= set(columns):
            long_col = chr(65 + columns.index("long_market_value"))
            short_col = chr(65 + columns.index("short_market_value"))
            options["Структура"]["charts"] = {"J2": {
                "type": "bar", "title": "Структура вложений: длинные и короткие позиции",
                "categories_range": f"'Структура'!$A$2:$A${end}",
                "series": [
                    {"name": "Длинные позиции", "values_range": f"'Структура'!${long_col}$2:${long_col}${end}"},
                    {"name": "Короткие позиции (абсолютная стоимость)", "values_range": f"'Структура'!${short_col}$2:${short_col}${end}"},
                ], "width": 850, "height": 420, "legend_position": "bottom",
            }}
    write_workbook(workbook_path, sheets, sheet_options=options)
    payload = {
        "kind": "portfolio_snapshot", "version": 1, "summary": report.summary,
        "positions": report.positions.to_dicts(), "allocation": report.allocation.to_dicts(),
        "cash": report.cash.to_dicts(), "flows": report.flows.to_dicts(),
    }
    json_path.write_bytes(orjson.dumps(payload, default=_json_default, option=orjson.OPT_INDENT_2))
    html_path.write_text(_html(report, workbook_path.name, json_path.name, comparison), encoding="utf-8")
    return workbook_path, json_path, html_path
