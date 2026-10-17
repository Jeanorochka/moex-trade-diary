from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from trade_exporter.brokers.registry import available_brokers, create_broker
from trade_exporter.config import load_config
from trade_exporter.domain.models import Account
from trade_exporter.exporters.analytics import export_analysis
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services.analytics import analyze_file, analyze_trades
from trade_exporter.services.export_service import ExportTradeHistory
from trade_exporter.services.finam_service import execute_finam
from trade_exporter.services.matcher import FifoMatcher


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Выгрузка сделок и аналитика дневника")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--broker", choices=available_brokers())
    parser.add_argument("--token", help="API-токен Финама; по умолчанию из FINAM_API_TOKEN")
    parser.add_argument("--account-id")
    parser.add_argument("--from", dest="date_from", help="dd.mm.yy")
    parser.add_argument("--to", dest="date_to", help="dd.mm.yy")
    analysis = parser.add_mutually_exclusive_group()
    analysis.add_argument("--analyze", type=Path, metavar="DIARY", help="Анализ готового Excel без API брокера")
    analysis.add_argument("--analytics", action="store_true", help="Аналитика и графики после выгрузки")
    parser.add_argument("--output-dir", type=Path, help="Каталог результатов")
    parser.add_argument("--starting-capital", default="0", help="Начальный капитал для относительных метрик")
    args = parser.parse_args(argv)
    try:
        args.starting_capital = Decimal(args.starting_capital)
    except ArithmeticError:
        parser.error("Начальный капитал должен быть числом.")
    if not args.starting_capital.is_finite() or args.starting_capital < 0:
        parser.error("Начальный капитал должен быть конечным неотрицательным числом.")
    if args.analyze and any((args.broker, args.token, args.account_id, args.date_from, args.date_to)):
        parser.error("--analyze анализирует файл целиком; параметры брокера и дат к нему не применяются.")
    return args


def parse_day(value: str, tz):
    value = value.strip()

    formats = (
        "%d.%m.%y",
        "%d.%m.%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            pass

    raise ValueError(
        f"Неверный формат даты: {value}. "
        "Используй dd.mm.yy или dd.mm.yyyy"
    )


def choose_account(accounts: list[Account]) -> Account:
    if not accounts:
        raise RuntimeError("Нет доступных счетов.")
    print("\nДоступные счета:\n")
    for i, account in enumerate(accounts, 1):
        tail = account.id[-6:] if account.id else "------"
        print(f"{i}. {account.name} | {account.account_type} | ...{tail}")
    while True:
        raw = input("\nВыбери номер счета: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(accounts):
            return accounts[int(raw) - 1]
        print("Нужен номер счета из списка.")


def print_analysis(report, output_dir):
    workbook, json_path = export_analysis(report, output_dir)
    win_rate = report.summary["win_rate"]
    ratio_label = "Доля положительных строк результата" if "event_type" in report.trades.columns else "Win rate"
    print(f"{ratio_label}: {win_rate:.2%}" if win_rate is not None else f"{ratio_label}: n/a")
    profit_factor = report.summary["profit_factor"]
    print(f"Profit factor: {profit_factor:.3f}" if profit_factor is not None else "Profit factor: n/a")
    print(f"Максимальная просадка: {report.summary['max_drawdown']:.2f}")
    print(f"Аналитика и графики: {workbook.resolve()}")
    print(f"JSON: {json_path.resolve()}")
    return workbook, json_path


def main(argv=None):
    args = parse_args(argv)
    if args.analyze:
        report = analyze_file(args.analyze, starting_capital=args.starting_capital)
        print(f"Закрытых строк FIFO: {report.summary['trades']}")
        print(f"Net P&L: {report.summary['net_pnl']:.2f}")
        return print_analysis(report, args.output_dir or args.analyze.parent / "analytics")

    load_dotenv()
    cfg = load_config(args.config)
    app_cfg = cfg.get("app", {})
    excel_cfg = cfg.get("excel", {})

    broker_name = (args.broker or app_cfg.get("broker", "tbank")).lower()
    broker_settings = dict(cfg.get(broker_name, {}))
    if args.token is not None:
        if broker_name != "finam":
            raise ValueError("--token используется с --broker finam.")
        broker_settings["token"] = args.token
    broker = create_broker(broker_name, broker_settings)
    tz = ZoneInfo(str(app_cfg.get("timezone", "Europe/Moscow")))

    accounts = broker.list_accounts()
    requested_id = args.account_id or str(app_cfg.get("default_account_id", "")).strip()
    if requested_id:
        account = next((a for a in accounts if a.id == requested_id), None)
        if account is None:
            raise ValueError("Указанный account_id не найден среди доступных счетов.")
    else:
        account = choose_account(accounts)

    date_from = args.date_from or input("С даты (dd.mm.yy): ").strip()
    date_to = args.date_to or input("По дату (dd.mm.yy): ").strip()
    period_from = parse_day(date_from, tz)
    last_day = parse_day(date_to, tz)
    if last_day < period_from:
        raise ValueError("Конечная дата раньше начальной.")
    period_to_exclusive = last_day + timedelta(days=1)

    exporter = ExcelExporter(
        output_dir=args.output_dir or Path(str(app_cfg.get("output_dir", "exports"))),
        filename_prefix=str(app_cfg.get("filename_prefix", "trades")),
        extra_columns=bool(excel_cfg.get("extra_columns", False)),
        include_summary_sheet=bool(excel_cfg.get("include_summary_sheet", True)),
    )
    if broker_name == "finam":
        path, report, source_reports = execute_finam(
            broker=broker,
            exporter=exporter,
            account=account,
            period_from=period_from,
            period_to_exclusive=period_to_exclusive,
            display_timezone=tz,
            starting_capital=args.starting_capital,
            reconstruction_mode=str(app_cfg.get("reconstruction_mode", "full")),
            lookback_days=int(app_cfg.get("lookback_days", 30)),
        )
        print("\nГотово.")
        print("Результат: закрытия акций FIFO и начисленная ВМ фьючерсов, включая открытые позиции.")
        print(f"Gross P&L: {report.summary['gross_pnl']:.2f}")
        print(f"Комиссии: {report.summary['commission']:.2f}")
        print(f"Net P&L: {report.summary['net_pnl']:.2f}")
        print(f"Excel: {path.resolve()}")
        print_analysis(report, path.parent)
        if source_reports:
            print(f"Исходные отчёты ({len(source_reports)}): {source_reports[0].parent.resolve()}")
        return path
    use_case = ExportTradeHistory(
        broker=broker,
        matcher=FifoMatcher(),
        exporter=exporter,
        reconstruction_mode=str(app_cfg.get("reconstruction_mode", "full")),
        lookback_days=int(app_cfg.get("lookback_days", 30)),
        display_timezone=tz,
    )
    path, trades = use_case.execute(account, period_from, period_to_exclusive)

    gross = sum((t.gross_pnl for t in trades), start=Decimal("0"))
    commission = sum((t.commission for t in trades), start=Decimal("0"))
    print("\nГотово.")
    print(f"Закрытых строк: {len(trades)}")
    print(f"Gross P&L: {gross:.2f}")
    print(f"Комиссии: {commission:.2f}")
    print(f"Net P&L: {(gross - commission):.2f}")
    print(f"Excel: {path.resolve()}")
    if args.analytics:
        report = analyze_trades(trades, tz, starting_capital=args.starting_capital)
        print_analysis(report, path.parent)
    return path


def run():
    main()
