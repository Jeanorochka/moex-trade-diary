from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from trade_exporter.brokers.registry import available_brokers, create_broker
from trade_exporter.config import load_config
from trade_exporter.domain.models import Account
from trade_exporter.exporters.report import export_report
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services.analytics import analyze_file, load_report_portfolio
from trade_exporter.services.export_service import ExportTradeHistory
from trade_exporter.services.finam_service import execute_finam
from trade_exporter.services.matcher import FifoMatcher
from trade_exporter.services.portfolio_service import execute_portfolio, prepare_portfolio_report, save_portfolio_report
from trade_exporter.services.manual_flows import parse_cash_flows


TOKEN_ENVS = {"finam": "F.INVEST_TOKEN", "tbank": "T.INVEST_TOKEN"}
BROKER_NAMES = {"finam": "Финам", "tbank": "Т-Банк"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Выгрузка сделок и аналитика дневника")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--broker", choices=available_brokers())
    parser.add_argument("--token", help="API-токен Финама; по умолчанию из F.INVEST_TOKEN")
    parser.add_argument("--account-id")
    parser.add_argument("--from", dest="date_from", help="dd.mm.yy")
    parser.add_argument("--to", dest="date_to", help="dd.mm.yy")
    analysis = parser.add_mutually_exclusive_group()
    analysis.add_argument("--analyze", type=Path, metavar="DIARY", help="Анализ готового Excel без API брокера")
    analysis.add_argument("--analytics", action="store_true", help="Аналитика и графики после выгрузки")
    analysis.add_argument("--portfolio", action="store_true", help="Текущий портфель, переоценка позиций и доходность с учётом денежных потоков")
    parser.add_argument("--output-dir", type=Path, help="Каталог результатов")
    parser.add_argument("--starting-capital", default="0", help="Начальный капитал для относительных метрик")
    parser.add_argument("--capital-from", help="Дата стартового капитала для текущего портфеля, dd.mm.yy")
    parser.add_argument("--cash-flow-mode", choices=("auto", "manual"), default="auto", help="Внешние движения: из брокера или полный ручной журнал")
    parser.add_argument("--cash-flow", action="append", default=[], metavar="DATE:AMOUNT", help="Ручное движение dd.mm.yyyy:+1000 или dd.mm.yyyy:-500; повторяется для каждой операции")
    args = parser.parse_args(argv)
    try:
        args.starting_capital = Decimal(args.starting_capital)
    except ArithmeticError:
        parser.error("Начальный капитал должен быть числом.")
    if not args.starting_capital.is_finite() or args.starting_capital < 0:
        parser.error("Начальный капитал должен быть конечным неотрицательным числом.")
    try:
        args.manual_cash_flows = parse_cash_flows(args.cash_flow)
    except ValueError as error:
        parser.error(str(error))
    if args.cash_flow and args.cash_flow_mode != "manual":
        parser.error("Для ручных движений выбери --cash-flow-mode manual.")
    if args.cash_flow_mode == "manual" and args.starting_capital <= 0:
        parser.error("Для ручных движений нужен положительный стартовый капитал.")
    if args.analyze and (args.cash_flow_mode != "auto" or args.cash_flow):
        parser.error("Ручные движения задаются при выгрузке портфеля; --analyze сохраняет журнал из исходного файла.")
    if args.analyze and any((args.broker, args.token, args.account_id, args.date_from, args.date_to, args.capital_from)):
        parser.error("--analyze анализирует файл целиком; параметры брокера и дат к нему не применяются.")
    if args.portfolio and (args.date_from or args.date_to):
        parser.error("--portfolio получает текущую оценку; исторические даты относятся к выгрузке сделок.")
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


def choose_broker(args, cfg):
    configured = str(cfg.get("app", {}).get("broker", "auto")).strip().lower()
    selected = args.broker or configured
    if selected != "auto":
        if selected not in available_brokers():
            raise ValueError("В app.broker укажи auto, finam или tbank.")
        return selected
    if args.token is not None:
        return "finam"
    candidates = []
    for name in available_brokers():
        settings = cfg.get(name, {})
        token_env = str(settings.get("token_env", TOKEN_ENVS[name]))
        token = settings.get("token") if name == "finam" else None
        if str(token or os.getenv(token_env, "")).strip():
            candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("Не найден токен. Заполни F.INVEST_TOKEN для Финама или T.INVEST_TOKEN для Т-Банка в корневом .env.")
    print("\nНайдены токены двух брокеров:")
    for index, name in enumerate(candidates, 1):
        print(f"{index}. {BROKER_NAMES[name]}")
    while True:
        try:
            answer = input("Выбери брокера: ").strip().lower()
        except (EOFError, OSError) as error:
            raise RuntimeError("Найдены оба токена. Укажи --broker finam или --broker tbank.") from error
        if answer in candidates:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("Нужен номер брокера из списка.")


def print_analysis(report, output_dir, portfolio_report=None):
    workbook, json_path = export_report(report, output_dir, portfolio_report=portfolio_report)
    print(f"Excel: {workbook.resolve()}")
    print(f"JSON: {json_path.resolve()}")
    return workbook, json_path


def print_portfolio(broker, account, output_dir, *, report=None, **options):
    if report is None:
        report, paths = execute_portfolio(broker, account, output_dir, **options)
    else:
        paths = save_portfolio_report(
            report, output_dir, progress=options.get("progress"),
            source_report_dir=options.get("source_report_dir"),
        )
    print(f"Excel: {paths[0].resolve()}")
    print(f"JSON: {paths[1].resolve()}")
    return paths


def main(argv=None, *, progress=None):
    status = progress or (lambda message: None)
    args = parse_args(argv)
    if args.analyze:
        status("Анализ готового файла")
        report = analyze_file(args.analyze, starting_capital=args.starting_capital)
        return print_analysis(report, args.output_dir or args.analyze.parent / "analytics", load_report_portfolio(args.analyze))

    load_dotenv()
    cfg = load_config(args.config)
    app_cfg = cfg.get("app", {})
    excel_cfg = cfg.get("excel", {})

    broker_name = choose_broker(args, cfg)
    broker_settings = dict(cfg.get(broker_name, {}))
    if args.token is not None:
        if broker_name != "finam":
            raise ValueError("--token используется с --broker finam.")
        broker_settings["token"] = args.token
    print(f"Брокер: {BROKER_NAMES[broker_name]}")
    status(f"Подключение: {BROKER_NAMES[broker_name]}")
    broker = create_broker(broker_name, broker_settings)
    tz = ZoneInfo(str(app_cfg.get("timezone", "Europe/Moscow")))

    status("Получение списка счетов")
    accounts = broker.list_accounts()
    requested_id = args.account_id or str(app_cfg.get("default_account_id", "")).strip()
    if requested_id:
        account = next((a for a in accounts if a.id == requested_id), None)
        if account is None:
            raise ValueError("Указанный account_id не найден среди доступных счетов.")
    else:
        account = choose_account(accounts)

    output_dir = args.output_dir or Path(str(app_cfg.get("output_dir", "exports")))
    if args.portfolio:
        if args.starting_capital > 0 and not args.capital_from:
            raise ValueError("Для стартового капитала укажи его дату: --capital-from dd.mm.yy.")
        baseline = parse_day(args.capital_from, tz) if args.capital_from else None
        if baseline is not None and baseline > datetime.now(tz):
            raise ValueError("Дата стартового капитала не может быть в будущем.")
        return print_portfolio(
            broker, account, output_dir, starting_capital=args.starting_capital,
            capital_started_at=baseline,
            cash_flow_mode=args.cash_flow_mode, manual_cash_flows=args.manual_cash_flows,
            progress=progress,
        )

    date_from = args.date_from or input("С даты (dd.mm.yy): ").strip()
    date_to = args.date_to or input("По дату (dd.mm.yy): ").strip()
    period_from = parse_day(date_from, tz)
    last_day = parse_day(date_to, tz)
    if last_day < period_from:
        raise ValueError("Конечная дата раньше начальной.")
    if last_day.date() > datetime.now(tz).date():
        raise ValueError("Конечная дата не может быть в будущем.")
    period_to_exclusive = last_day + timedelta(days=1)
    capital_started_at = parse_day(args.capital_from, tz) if args.capital_from else period_from
    if capital_started_at != period_from:
        raise ValueError("Для выгрузки сделок стартовый капитал должен относиться к дате --from.")
    with TemporaryDirectory(prefix="trade_exporter_sources_") as source_dir:
        return export_selected_period(
            broker, broker_name, account, args, app_cfg, excel_cfg, tz,
            output_dir, period_from, period_to_exclusive, capital_started_at,
            Path(source_dir), progress=progress,
        )


def export_selected_period(
    broker, broker_name, account, args, app_cfg, excel_cfg, tz, output_dir,
    period_from, period_to_exclusive, capital_started_at, source_dir, *, progress=None,
):
    status = progress or (lambda message: None)
    exporter = ExcelExporter(
        output_dir=output_dir,
        filename_prefix=str(app_cfg.get("filename_prefix", "trades")),
        extra_columns=bool(excel_cfg.get("extra_columns", False)),
        include_summary_sheet=bool(excel_cfg.get("include_summary_sheet", True)),
    )
    portfolio_report = prepare_portfolio_report(
        broker, account, output_dir, starting_capital=args.starting_capital,
        capital_started_at=capital_started_at, result_period_start=period_from,
        result_period_end=period_to_exclusive, progress=progress,
        cash_flow_mode=args.cash_flow_mode, manual_cash_flows=args.manual_cash_flows,
        source_report_dir=source_dir,
    )
    if broker_name == "finam":
        status("Загрузка исполнений и брокерских отчётов Финама")
        try:
            _, report, _ = execute_finam(
                broker=broker,
                exporter=exporter,
                account=account,
                period_from=period_from,
                period_to_exclusive=period_to_exclusive,
                display_timezone=tz,
                starting_capital=args.starting_capital,
                reconstruction_mode=str(app_cfg.get("reconstruction_mode", "full")),
                lookback_days=int(app_cfg.get("lookback_days", 30)),
                portfolio_report=portfolio_report,
                write_output=False, source_report_dir=source_dir,
            )
        except Exception:
            if portfolio_report is not None:
                print("Торговую детализацию выгрузить не удалось. Сохраняю отдельно подготовленную оценку портфеля.")
                print_portfolio(broker, account, output_dir, report=portfolio_report, progress=progress, source_report_dir=source_dir)
            raise
    else:
        status("Загрузка и сопоставление сделок Т-Банка")
        use_case = ExportTradeHistory(
            broker=broker,
            matcher=FifoMatcher(),
            exporter=exporter,
            reconstruction_mode=str(app_cfg.get("reconstruction_mode", "full")),
            lookback_days=int(app_cfg.get("lookback_days", 30)),
            display_timezone=tz,
            starting_capital=args.starting_capital,
            portfolio_report=portfolio_report,
        )
        try:
            use_case.execute(account, period_from, period_to_exclusive, write_output=False)
        except Exception:
            if portfolio_report is not None:
                print("Торговую детализацию выгрузить не удалось. Сохраняю подготовленную оценку портфеля.")
                print_portfolio(broker, account, output_dir, report=portfolio_report, progress=progress, source_report_dir=source_dir)
            raise
        report = use_case.analysis_report
    status("Сохранение Excel с аналитикой и лидерами сделок")
    details = dict(report.details)
    details["source_reports"] = sorted(set(details.get("source_reports", ())) | {
        path for path in source_dir.rglob("*") if path.is_file()
    })
    path, json_path = export_report(
        report, output_dir, portfolio_report=portfolio_report,
        prefix=f"report_{broker_name}", details=details,
        period_from=period_from, period_to=period_to_exclusive - timedelta(microseconds=1),
    )
    print(f"Excel: {path.resolve()}")
    print(f"JSON: {json_path.resolve()}")
    status("Выгрузка завершена")
    return path


def run():
    main()
