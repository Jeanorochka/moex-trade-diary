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
from trade_exporter.exporters.excel import ExcelExporter
from trade_exporter.services.export_service import ExportTradeHistory
from trade_exporter.services.matcher import FifoMatcher


def parse_args():
    parser = argparse.ArgumentParser(description="Выгрузка сделок в Excel")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--broker", choices=available_brokers())
    parser.add_argument("--account-id")
    parser.add_argument("--from", dest="date_from", help="dd.mm.yy")
    parser.add_argument("--to", dest="date_to", help="dd.mm.yy")
    return parser.parse_args()


def parse_day(value: str, tz):
    value = value.strip()

    formats = (
        "%d.%m.%y",   # 10.09.26
        "%d.%m.%Y",   # 10.09.2026
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


def main():
    load_dotenv()
    args = parse_args()
    cfg = load_config(args.config)
    app_cfg = cfg.get("app", {})
    excel_cfg = cfg.get("excel", {})

    broker_name = (args.broker or app_cfg.get("broker", "tbank")).lower()
    broker = create_broker(broker_name, cfg.get(broker_name, {}))
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
        output_dir=Path(str(app_cfg.get("output_dir", "exports"))),
        filename_prefix=str(app_cfg.get("filename_prefix", "trades")),
        extra_columns=bool(excel_cfg.get("extra_columns", False)),
        include_summary_sheet=bool(excel_cfg.get("include_summary_sheet", True)),
    )
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
