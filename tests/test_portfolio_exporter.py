from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

import orjson
import polars as pl

from trade_exporter.exporters.portfolio import export_portfolio


MONEY = pl.Decimal(precision=38, scale=18)
POSITION_SCHEMA = {
    "instrument_id": pl.String, "ticker": pl.String, "asset_type": pl.String,
    "quantity": MONEY, "average_price": MONEY, "current_price": MONEY,
    "price_currency": pl.String, "average_price_currency": pl.String,
    "market_value": MONEY, "cost_basis": MONEY, "unrealized_pnl": MONEY,
    "valuation_currency": pl.String, "unrealized_return": MONEY,
    "weight": MONEY, "equity_weight": MONEY, "long_exposure": MONEY,
    "short_exposure": MONEY, "price_as_of": pl.String,
    "valuation_source": pl.String, "unrealized_source": pl.String,
    "valuation_status": pl.String,
}
ALLOCATION_SCHEMA = {
    "asset_type": pl.String, "long_market_value": MONEY, "short_market_value": MONEY,
    "gross_exposure": MONEY, "long_weight": MONEY, "short_weight": MONEY, "equity_weight": MONEY,
}
CASH_SCHEMA = {"currency": pl.String, "amount": MONEY, "amount_base": MONEY}
FLOW_SCHEMA = {
    "timestamp": pl.Datetime("us", "UTC"), "amount": MONEY, "currency": pl.String,
    "kind": pl.String, "id": pl.String, "amount_base": MONEY,
}


def report():
    rows = []
    for ticker, quantity, average, current, cost, market, pnl in [
        ("SBER", "100", "200", "250", "20000", "25000", "5000"),
        ("VTBR", "-100", "10", "12", "-1000", "-1200", "-200"),
    ]:
        rows.append({
            "instrument_id": ticker + "@MISX", "ticker": ticker, "asset_type": "share",
            "quantity": Decimal(quantity), "average_price": Decimal(average), "current_price": Decimal(current),
            "price_currency": "RUB", "average_price_currency": "RUB", "market_value": Decimal(market),
            "cost_basis": Decimal(cost), "unrealized_pnl": Decimal(pnl), "valuation_currency": "RUB",
            "unrealized_return": Decimal(pnl) / abs(Decimal(cost)), "weight": None,
            "equity_weight": Decimal(market) / Decimal("33800"), "long_exposure": max(Decimal(market), Decimal("0")),
            "short_exposure": max(-Decimal(market), Decimal("0")), "price_as_of": "2026-10-17T12:00:00+00:00",
            "valuation_source": "broker snapshot", "unrealized_source": "broker", "valuation_status": "broker",
        })
    return SimpleNamespace(
        summary={
            "account_id": "test-account", "broker": "finam", "captured_at": "2026-10-17T12:00:00+00:00",
            "base_currency": "RUB", "equity": Decimal("33800"), "cash_base": Decimal("10000"),
            "known_cash_base": Decimal("10000"), "positions_market_value": Decimal("23800"),
            "known_positions_market_value": Decimal("23800"), "valuation_residual": Decimal("0"),
            "positions_count": 2, "priced_positions_count": 2, "unvalued_positions_count": 0,
            "long_market_value": Decimal("25000"), "short_market_value": Decimal("1200"),
            "gross_exposure": Decimal("26200"), "unrealized_pnl": Decimal("4800"),
            "known_unrealized_pnl": Decimal("4800"), "unrealized_cost_basis": Decimal("21000"),
            "holdings_return": Decimal("0.228571428571428571"), "total_external_inflows": Decimal("30000"),
            "total_external_outflows": Decimal("1000"), "net_external_funding": Decimal("29000"),
            "lifetime_profit": Decimal("4800"), "profit_to_net_funding_ratio": None, "xirr": None,
            "ledger_complete": True, "ledger_start": "2025-10-17T00:00:00+00:00",
            "income": Decimal("0"), "fees": Decimal("0"), "taxes": Decimal("0"),
            "source": "Finam account snapshot", "valuation_complete": True,
            "positions_include_pending_orders": False,
        },
        positions=pl.DataFrame(rows, schema=POSITION_SCHEMA),
        allocation=pl.DataFrame([{
            "asset_type": "share", "long_market_value": Decimal("25000"), "short_market_value": Decimal("1200"),
            "gross_exposure": Decimal("26200"), "long_weight": Decimal("1"), "short_weight": Decimal("1"),
            "equity_weight": Decimal("23800") / Decimal("33800"),
        }], schema=ALLOCATION_SCHEMA),
        cash=pl.DataFrame([{"currency": "RUB", "amount": Decimal("10000"), "amount_base": Decimal("10000")}], schema=CASH_SCHEMA),
        flows=pl.DataFrame([
            {"timestamp": datetime(2025, 10, 17, tzinfo=timezone.utc), "amount": Decimal("30000"), "currency": "RUB", "kind": "deposit", "id": "in-1", "amount_base": Decimal("30000")},
            {"timestamp": datetime(2026, 1, 17, tzinfo=timezone.utc), "amount": Decimal("-1000"), "currency": "RUB", "kind": "withdraw", "id": "out-1", "amount_base": Decimal("-1000")},
        ], schema=FLOW_SCHEMA),
    )


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, value):
        self.text.append(value)


def test_portfolio_exports_exact_snapshot_values_and_readable_sheets(tmp_path):
    value = report()
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    assert all(path.exists() for path in (workbook, json_path, html_path))
    data = orjson.loads(json_path.read_bytes())
    assert data["kind"] == "portfolio_snapshot"
    assert data["version"] == 1
    assert Decimal(data["summary"]["equity"]) == Decimal("33800")
    assert Decimal(data["positions"][0]["quantity"]) == 100
    assert Decimal(data["positions"][1]["market_value"]) == -1200
    assert Decimal(data["summary"]["holdings_return"]) == Decimal("0.228571428571428571")
    assert data["flows"][0]["timestamp"].startswith("2025-10-17T00:00:00")
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert list(sheets) == ["Портфель", "Позиции", "Структура", "Деньги", "Движения", "Покупка и сейчас"]
    assert sheets["Позиции"]["количество"].to_list() == [100, -100]
    assert sheets["Позиции"]["текущая стоимость"].to_list() == [25000, -1200]
    assert sheets["Покупка и сейчас"]["инструмент"].to_list() == ["SBER"]
    assert sheets["Движения"]["сумма"].to_list() == [30000, -1000]
    text = html_path.read_text(encoding="utf-8")
    assert "33 800,00 ₽" in text
    assert "4 800,00 ₽" in text
    assert "Средняя покупка" in text
    assert "Нереализованный результат" in text
    assert "Результат за всю историю" in text


def test_excel_uses_native_bar_charts_without_fabricated_equity_history(tmp_path):
    workbook, _, _ = export_portfolio(report(), tmp_path)
    namespace = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
    with ZipFile(workbook) as archive:
        charts = [name for name in archive.namelist() if name.startswith("xl/charts/chart") and name.endswith(".xml")]
        assert len(charts) == 2
        roots = [ElementTree.fromstring(archive.read(name)) for name in charts]
        assert all(root.find(".//c:barChart", namespace) is not None for root in roots)
        formulas = [ref.text for root in roots for ref in root.findall(".//c:f", namespace)]
        assert any("Покупка и сейчас" in formula for formula in formulas)
        assert any("Структура" in formula for formula in formulas)
        assert not any("Дни" in formula or "История капитала" in formula for formula in formulas)


def test_html_is_standalone_accessible_and_escapes_untrusted_labels(tmp_path):
    value = report()
    value.summary["account_id"] = '<script>alert("account")</script>'
    value.summary["source"] = '<img src="https://example.invalid/tracker">'
    value.positions = value.positions.with_columns(pl.lit('<svg onload="alert(1)">').alias("ticker"))
    _, _, path = export_portfolio(value, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert '<script>alert("account")</script>' not in text
    assert '<svg onload="alert(1)">' not in text
    assert '&lt;script&gt;alert(&quot;account&quot;)&lt;/script&gt;' in text
    document = Document()
    document.feed(text)
    assert sum(tag == "script" for tag, attrs in document.tags) == 1
    assert not any(tag in {"img", "iframe", "link"} for tag, attrs in document.tags)
    assert not any("src" in attrs for tag, attrs in document.tags)
    assert any(tag == "html" and attrs.get("lang") == "ru" for tag, attrs in document.tags)
    assert any(tag == "svg" and attrs.get("role") == "img" for tag, attrs in document.tags)
    assert any(tag == "input" and attrs.get("aria-label") == "Найти инструмент" for tag, attrs in document.tags)


def test_partial_ledger_never_displays_defined_lifetime_profit(tmp_path):
    value = report()
    value.summary.update(ledger_complete=False, lifetime_profit=Decimal("999999"))
    _, _, path = export_portfolio(value, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "999 999,00" not in text
    assert "Неполная" in text
    assert '<strong class="">н/д</strong>' in text


def test_unknown_derivative_and_foreign_currency_are_not_treated_as_ruble_value(tmp_path):
    value = report()
    future = {name: None for name in POSITION_SCHEMA}
    future.update(instrument_id="RIU6@FORTS", ticker="RIU6", asset_type="futures", quantity=Decimal("1"), current_price=Decimal("100000"), price_currency="RUB", valuation_status="unsupported_price_basis")
    foreign = {name: None for name in POSITION_SCHEMA}
    foreign.update(instrument_id="AAPL@XNAS", ticker="AAPL", asset_type="share", quantity=Decimal("2"), average_price=Decimal("100"), current_price=Decimal("120"), cost_basis=Decimal("200"), market_value=Decimal("240"), price_currency="USD", average_price_currency="USD", valuation_currency="USD", valuation_status="foreign_currency")
    value.positions = pl.concat([value.positions, pl.DataFrame([future, foreign], schema=POSITION_SCHEMA)])
    value.summary.update(positions_count=4, unvalued_positions_count=2, valuation_complete=False, positions_market_value=None, unrealized_pnl=None)
    workbook, json_path, path = export_portfolio(value, tmp_path)
    comparison = pl.read_excel(workbook, sheet_name="Покупка и сейчас")
    assert comparison["инструмент"].to_list() == ["SBER"]
    text = path.read_text(encoding="utf-8")
    assert "Денежная оценка не определена" in text
    assert "В исходной валюте" in text
    assert "120,0000 $" in text
    assert orjson.loads(json_path.read_bytes())["summary"]["positions_market_value"] is None


def test_empty_portfolio_preserves_schemas_and_contains_no_invalid_charts(tmp_path):
    value = report()
    value.positions = pl.DataFrame(schema=POSITION_SCHEMA)
    value.allocation = pl.DataFrame(schema=ALLOCATION_SCHEMA)
    value.cash = pl.DataFrame(schema=CASH_SCHEMA)
    value.flows = pl.DataFrame(schema=FLOW_SCHEMA)
    value.summary.update(equity=Decimal("0"), positions_count=0, priced_positions_count=0, positions_market_value=Decimal("0"), cash_base=Decimal("0"), unrealized_pnl=None, ledger_complete=False, lifetime_profit=None)
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    with ZipFile(workbook) as archive:
        assert not any(name.startswith("xl/charts/chart") for name in archive.namelist())
    assert orjson.loads(json_path.read_bytes())["positions"] == []
    assert "Открытых позиций нет" in html_path.read_text(encoding="utf-8")
    assert pl.read_excel(workbook, sheet_name="Позиции").height == 0


def test_export_names_never_overwrite_existing_portfolio(tmp_path):
    value = report()
    first = export_portfolio(value, tmp_path)
    second = export_portfolio(value, tmp_path)
    assert set(first).isdisjoint(second)
    assert all(path.exists() for path in (*first, *second))


def test_nav_graph_uses_only_actual_observations_and_shows_adjusted_period(tmp_path):
    value = report()
    value.summary["valuation_history"] = [
        {"captured_at": "2026-10-01T12:00:00+00:00", "equity": Decimal("20000"), "currency": "RUB"},
        {"captured_at": "2026-10-17T12:00:00+00:00", "equity": Decimal("33800"), "currency": "RUB"},
    ]
    value.summary["observed_period"] = {
        "start": "2026-10-01T12:00:00+00:00", "end": "2026-10-17T12:00:00+00:00",
        "opening_equity": Decimal("20000"), "closing_equity": Decimal("33800"),
        "net_external_flows": Decimal("10000"), "profit": Decimal("3800"),
        "modified_dietz_return": Decimal("0.15"), "days": 16, "basis": "observed_snapshots",
    }
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    text = html_path.read_text(encoding="utf-8")
    assert "Наблюдения стоимости" in text
    assert "пополнения и выводы не исключены" in text
    assert "3 800,00 ₽" in text
    assert "15,00%" in text
    document = Document()
    document.feed(text)
    assert sum(tag == "circle" for tag, attrs in document.tags) == 2
    history = pl.read_excel(workbook, sheet_name="История стоимости")
    assert history["капитал"].to_list() == [20000, 33800]
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["observed_period"]["profit"]) == 3800
    with ZipFile(workbook) as archive:
        charts = [name for name in archive.namelist() if name.startswith("xl/charts/chart") and name.endswith(".xml")]
        assert len(charts) == 3


def test_first_observation_does_not_create_equity_history_curve(tmp_path):
    value = report()
    value.summary["valuation_history"] = [{"captured_at": value.summary["captured_at"], "equity": value.summary["equity"], "currency": "RUB"}]
    value.summary["observed_period"] = None
    workbook, _, html_path = export_portfolio(value, tmp_path)
    text = html_path.read_text(encoding="utf-8")
    assert "<polyline" not in text
    assert "Результат между снимками" not in text
    assert pl.read_excel(workbook, sheet_name="История стоимости").height == 1


def test_typed_portfolio_core_report_exports_without_snapshot_assumptions(tmp_path):
    from trade_exporter.domain.portfolio import CashFlow, PortfolioPosition, PortfolioSnapshot
    from trade_exporter.services.portfolio import analyze_portfolio

    opened = datetime(2025, 10, 17, tzinfo=timezone.utc)
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    value = analyze_portfolio(
        PortfolioSnapshot(
            "account", "finam", now, "RUB", Decimal("120"),
            positions=(PortfolioPosition(
                "SBER@MISX", "SBER", "equities", Decimal("1"),
                average_price=Decimal("100"), current_price=Decimal("120"), price_as_of=now,
            ),),
        ),
        [CashFlow(opened, Decimal("100"), "RUB")], ledger_complete=True, ledger_start=opened,
    )
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    assert all(path.exists() for path in (workbook, json_path, html_path))
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["unrealized_pnl"]) == 20
    assert Decimal(payload["summary"]["equity"]) == 120
    assert "Акции" in html_path.read_text(encoding="utf-8")
    assert pl.read_excel(workbook, sheet_name="Позиции")["состояние котировки"].to_list() == ["На указанную дату"]


def test_quality_reasons_and_pending_quantities_are_visible_to_reader(tmp_path):
    value = report()
    value.summary.update({
        "cash_flow_quality": ["Перевод ценных бумаг требует оценки внешнего потока."],
        "period_quality": ["Доходность периода требует полной истории внешних потоков."],
        "valuation_reconciliation_status": "partial_components",
        "valuation_residual_threshold": Decimal("0.01"), "funding_status": "missing_inception_funding",
    })
    value.positions = value.positions.with_columns(pl.lit(True).alias("quantity_includes_pending_orders"))
    workbook, _, path = export_portfolio(value, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "С учетом ожидающих заявок" in text
    assert "Перевод ценных бумаг требует оценки внешнего потока." in text
    assert "Доходность периода требует полной истории внешних потоков." in text
    assert "Денежная оценка состава доступна частично" in text
    assert "Исходные внесения не подтверждены" in text
    assert "partial_components" not in text
    reasons = pl.read_excel(workbook, sheet_name="Основания")
    assert reasons.height == 2
    assert reasons["основание"][0] == "Перевод ценных бумаг требует оценки внешнего потока."


def test_profit_to_net_funding_ratio_is_not_labelled_investment_return(tmp_path):
    value = report()
    value.summary["profit_to_net_funding_ratio"] = Decimal("0.2")
    workbook, _, path = export_portfolio(value, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Прибыль / чистые взносы" in text
    assert "Доходность за всю историю" not in text
    summary = pl.read_excel(workbook, sheet_name="Портфель")
    ratio = summary.filter(pl.col("показатель") == "Отношение прибыли к чистым взносам")
    assert ratio["значение"].to_list() == ["20,00%"]


def test_risk_sheet_has_numeric_values_units_methods_and_missing_var_reason(tmp_path):
    value = report()
    value.summary["risk"] = {
        "basis": "portfolio_snapshot_and_observed_nav", "as_of": "2026-10-17T12:00:00+00:00",
        "metrics": [
            {"metric": "max_drawdown_observed", "label": "Максимальная просадка наблюдаемого портфеля", "value": Decimal("0.04"), "unit": "fraction", "method": "Индекс стоимости с учетом внешних движений", "observations": 8, "basis": "actual_nav", "quality_reason": "Между снимками просадка не наблюдается."},
            {"metric": "historical_var95_1d_money", "label": "Исторический VaR 95% за день", "value": None, "unit": "RUB", "method": "Исторические однодневные изменения стоимости", "observations": 7, "basis": "actual_nav", "quality_reason": "Нужно не менее 20 дневных наблюдений доходности."},
            {"metric": "adverse_position_shock_5pct", "label": "Потеря при неблагоприятном изменении позиций на 5%", "value": Decimal("1310"), "unit": "RUB", "method": "Сценарий изменения известных вложений", "observations": 2, "basis": "scenario", "quality_reason": "Сценарная оценка; не VaR."},
            {"metric": "capital_period_annualized", "label": "Текущий результат в годовом темпе", "value": Decimal("0.2"), "unit": "fraction", "method": "Геометрическая экстраполяция результата периода", "observations": 1, "basis": "annualized_extrapolation", "quality_reason": "Экстраполяция; не прогноз."},
        ], "quality": ["Параметры рассчитаны только по доступным наблюдениям."],
    }
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    frame = pl.read_excel(workbook, sheet_name="Риск и доходность")
    assert frame["значение"].to_list() == [0.2, 0.04, None, 1310]
    assert frame.schema["значение"].is_numeric()
    assert frame["единица"].to_list() == ["%", "%", "RUB", "RUB"]
    assert frame["наблюдений"].to_list() == [1, 8, 7, 2]
    assert "20 дневных" in frame["качество данных"][2]
    assert frame["основание"][3] == "Сценарий изменения позиций; не VaR"
    assert "не прогноз" in frame["основание"][0]
    payload = orjson.loads(json_path.read_bytes())
    assert Decimal(payload["summary"]["risk"]["metrics"][0]["value"]) == Decimal("0.04")
    text = html_path.read_text(encoding="utf-8")
    assert "Риск и доходность" in text
    assert "4,00%" in text
    assert "20 дневных наблюдений" in text
    assert "Сценарная оценка; не VaR." in text
    with ZipFile(workbook) as archive:
        namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet_id = next(int(sheet.get("sheetId")) for sheet in book.find("s:sheets", namespace) if sheet.get("name") == "Риск и доходность")
        sheet = ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{sheet_id}.xml"))
        cell = sheet.find(".//s:c[@r='B2']", namespace)
        assert cell.get("t") not in {"s", "inlineStr"}
        assert Decimal(cell.find("s:v", namespace).text) == Decimal("0.2")
        assert sheet.find(".//s:c[@r='B4']/s:v", namespace) is None


def test_initial_capital_risk_basis_and_labels_are_preserved(tmp_path):
    value = report()
    value.summary["risk"] = {
        "as_of": value.summary["captured_at"], "metrics": [{
            "metric": "capital_period_return", "label": "Доходность от введенного начального капитала",
            "value": Decimal("0.1"), "unit": "fraction", "method": "С учетом движений за период",
            "observations": 1, "basis": "starting_capital_assumption", "quality_reason": "Начальная стоимость задана пользователем.",
        }], "quality": [],
    }
    workbook, _, html_path = export_portfolio(value, tmp_path)
    frame = pl.read_excel(workbook, sheet_name="Риск и доходность")
    assert frame["основание"].to_list() == ["От введенного начального капитала"]
    assert frame["значение"].to_list() == [0.1]
    text = html_path.read_text(encoding="utf-8")
    assert "Доходность от введенного начального капитала" in text
    assert "10,00%" in text


def test_full_period_is_primary_and_unknown_lifetime_funding_is_not_zero(tmp_path):
    value = report()
    value.summary.update({
        "ledger_complete": False, "returns_complete": False, "funding_status": "missing_inception_funding",
        "total_external_inflows": Decimal("0"), "total_external_outflows": Decimal("0"),
        "net_external_funding": Decimal("0"), "lifetime_profit": None,
        "portfolio_period": {
            "status": "complete", "base_currency": "RUB", "started_at": "2025-09-25T00:00:00+03:00",
            "ended_at": "2026-10-17T12:00:00+03:00", "starting_equity": Decimal("30000"),
            "ending_equity": Decimal("33800"), "external_inflows": Decimal("1000"),
            "external_outflows": Decimal("0"), "net_external_flows": Decimal("1000"),
            "total_profit": Decimal("2800"), "period_return": 0.092, "annualized_return": 0.086,
            "xirr": None, "days": 387.5, "valuation_basis": "current_broker_nav",
            "quality": [], "source": "Начальный NAV, текущая стоимость и подтверждённые потоки периода.",
        },
    })
    value.period_flows = pl.DataFrame([{
        "timestamp": datetime(2026, 6, 17, tzinfo=timezone.utc), "amount": Decimal("1000"),
        "currency": "RUB", "kind": "external", "id": "period-deposit", "amount_base": Decimal("1000"),
    }], schema=FLOW_SCHEMA)
    workbook, json_path, html_path = export_portfolio(value, tmp_path)
    sheets = pl.read_excel(workbook, sheet_id=0)
    assert list(sheets)[0] == "Портфель за период"
    assert sheets["Потоки периода"]["сумма"].to_list() == [1000]
    assert sheets["Портфель за период"].filter(pl.col("показатель") == "Полный результат портфеля")["значение"].to_list() == [2800]
    assert sheets["Портфель"].filter(pl.col("показатель") == "Внесено на счет")["значение"].to_list() == ["н/д"]
    text = html_path.read_text(encoding="utf-8")
    assert text.index('id="portfolio-period"') < text.index("Открытые позиции")
    assert "2 800,00 ₽" in text
    assert '<span class="label">Внесено</span><strong>н/д</strong>' in text
    assert '<span class="label">Выведено</span><strong>н/д</strong>' in text
    assert "Полная история внешних движений с открытия не подтверждена" in text
    payload = orjson.loads(json_path.read_bytes())
    assert payload["period_flows"][0]["id"] == "period-deposit"
    assert Decimal(payload["summary"]["portfolio_period"]["total_profit"]) == 2800


def test_unavailable_full_period_does_not_display_proxy_profit_as_nav_return(tmp_path):
    value = report()
    value.summary["portfolio_period"] = {
        "status": "unavailable", "base_currency": "RUB", "starting_equity": Decimal("30000"),
        "ending_equity": Decimal("33800"), "total_profit": Decimal("999"),
        "period_return": 0.99, "annualized_return": 9.99, "xirr": 8.88,
        "quality": ["Не подтверждены внешние движения выбранного периода."], "source": "Broker NAV",
    }
    workbook, _, html_path = export_portfolio(value, tmp_path)
    frame = pl.read_excel(workbook, sheet_name="Портфель за период")
    for label in ("Полный результат портфеля", "Доходность портфеля за период", "Годовой пересчёт доходности портфеля", "Годовая денежно-взвешенная доходность (XIRR)"):
        assert frame.filter(pl.col("показатель") == label)["значение"].to_list() == [None]
    assert frame.filter(pl.col("показатель") == "Капитал в начале")["значение"].to_list() == [30000]
    text = html_path.read_text(encoding="utf-8")
    assert "Полный результат недоступен" in text
    assert "999,00 ₽" not in text
    assert "99,00%" not in text


def test_actual_period_risk_precedes_trading_proxy_with_matching_percent_formats(tmp_path):
    value = report()
    value.summary["risk"] = {
        "as_of": "2026-10-17", "metrics": [
            {"metric": "proxy_max_drawdown", "label": "Торговая просадка", "value": 0.4, "unit": "fraction", "basis": "closed_pnl_proxy"},
            {"metric": "max_drawdown_observed", "label": "Фактическая просадка NAV", "value": None, "unit": "fraction", "basis": "actual_nav"},
            {"metric": "capital_period_return", "label": "Доходность портфеля за период", "value": 0.0248, "unit": "fraction", "basis": "starting_capital_assumption"},
            {"metric": "capital_period_profit", "label": "Полный результат периода", "value": Decimal("3234.22"), "unit": "RUB", "basis": "starting_capital_assumption"},
        ], "quality": [],
    }
    workbook, _, html_path = export_portfolio(value, tmp_path)
    frame = pl.read_excel(workbook, sheet_name="Риск и доходность")
    assert frame["показатель"].to_list() == ["Полный результат периода", "Доходность портфеля за период", "Фактическая просадка NAV", "Торговая просадка"]
    assert frame["значение"].to_list() == [3234.22, 0.0248, None, 0.4]
    assert "без полной переоценки портфеля" in frame["основание"][-1]
    text = html_path.read_text(encoding="utf-8")
    assert "2,48%" in text
    assert "40,00%" in text
