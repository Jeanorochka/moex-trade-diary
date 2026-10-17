from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
from xlsxturbo import dfs_to_xlsx
from xlsxturbo.types import SheetOptions

from trade_exporter.domain.models import ClosedTrade


def write_workbook(
    path: str | Path,
    sheets: Mapping[str, pl.DataFrame],
    *,
    sheet_options: Mapping[str, SheetOptions] | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    defaults: SheetOptions = {
        "autofit": True,
        "header_format": {"bold": True},
        "freeze_panes": True,
        "autofilter": True,
    }
    options = sheet_options or {}
    dfs_to_xlsx(
        [
            (frame, name, defaults | options.get(name, {}))
            for name, frame in sheets.items()
        ],
        path,
    )
    return path


class ExcelExporter:
    BASE_HEADERS = [
        "дата",
        "тикер",
        "цена входа",
        "цена выхода",
        "финрез",
        "комиссия",
    ]
    EXTRA_HEADERS = ["направление", "количество", "финрез net"]

    def __init__(
        self,
        output_dir: str | Path,
        filename_prefix: str = "trades",
        extra_columns: bool = False,
        include_summary_sheet: bool = True,
    ):
        self.output_dir = Path(output_dir)
        self.filename_prefix = filename_prefix
        self.extra_columns = extra_columns
        self.include_summary_sheet = include_summary_sheet

    def export(
        self,
        trades: list[ClosedTrade],
        broker_name: str,
        account_id: str,
        period_from: datetime,
        period_to_inclusive: datetime,
        display_timezone,
        *,
        risk: dict | None = None,
    ) -> Path:
        path = self._new_file_path(
            broker_name,
            account_id,
            period_from,
            period_to_inclusive,
        )
        schema = dict(
            zip(
                self.BASE_HEADERS,
                [pl.String, pl.String, pl.Float64, pl.Float64, pl.Float64, pl.Float64],
            )
        )
        if self.extra_columns:
            schema.update(zip(self.EXTRA_HEADERS, [pl.String, pl.Int64, pl.Float64]))
        rows = []
        for trade in trades:
            row = [
                trade.exit_at.astimezone(display_timezone).strftime("%d.%m.%y"),
                trade.ticker,
                float(trade.entry_price),
                float(trade.exit_price),
                float(trade.gross_pnl),
                float(trade.commission),
            ]
            if self.extra_columns:
                row += [trade.direction, trade.quantity, float(trade.net_pnl)]
            rows.append(row)
        sheets = {"Сделки": pl.DataFrame(rows, schema=schema, orient="row")}
        options: dict[str, SheetOptions] = {
            "Сделки": {
                "autofit": False,
                "column_widths": {
                    0: 12, 1: 16, 2: 16, 3: 16, 4: 14, 5: 14,
                    6: 14, 7: 12, 8: 14,
                },
                "column_formats": {
                    "цена входа": {"num_format": "0.######"},
                    "цена выхода": {"num_format": "0.######"},
                    "финрез": {"num_format": "0.00"},
                    "комиссия": {"num_format": "0.00"},
                    **({"финрез net": {"num_format": "0.00"}} if self.extra_columns else {}),
                },
            }
        }
        if self.include_summary_sheet:
            sheets["Итоги"] = self._summary_frame(trades)
            options["Итоги"] = {
                "header": False,
                "autofit": False,
                "freeze_panes": False,
                "autofilter": False,
                "column_widths": {0: 24, 1: 18},
                "cells": {
                    "B4": {
                        "value": sheets["Итоги"]["value"][3],
                        "num_format": "0.00%",
                    },
                    **{
                        f"B{row}": {
                            "value": sheets["Итоги"]["value"][row - 1],
                            "num_format": "0.00",
                        }
                        for row in (5, 6, 7)
                    },
                },
            }
        if risk is not None:
            from trade_exporter.exporters.portfolio import risk_sheet, risk_sheet_options

            sheets["Риск и доходность"] = risk_sheet(risk)
            options["Риск и доходность"] = risk_sheet_options(risk)
        return write_workbook(path, sheets, sheet_options=options)

    def _new_file_path(
        self,
        broker_name: str,
        account_id: str,
        period_from: datetime,
        period_to_inclusive: datetime,
    ) -> Path:
        tail = account_id[-6:] if account_id else "account"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = (
            f"{self.filename_prefix}_{broker_name}_{tail}_"
            f"{period_from.strftime('%Y%m%d')}_"
            f"{period_to_inclusive.strftime('%Y%m%d')}_{stamp}.xlsx"
        )
        return self.output_dir / filename

    @staticmethod
    def _summary_frame(trades: list[ClosedTrade]) -> pl.DataFrame:
        gross = sum((t.gross_pnl for t in trades), start=Decimal("0"))
        commission = sum((t.commission for t in trades), start=Decimal("0"))
        winners = sum(1 for t in trades if t.net_pnl > 0)
        losers = sum(1 for t in trades if t.net_pnl < 0)
        total = len(trades)
        rows = [
            ("Закрытых сделок", total),
            ("Прибыльных", winners),
            ("Убыточных", losers),
            ("Win rate", winners / total if total else 0),
            ("Gross P&L", float(gross)),
            ("Комиссия", float(commission)),
            ("Net P&L", float(gross - commission)),
        ]
        return pl.DataFrame(
            rows, schema={"metric": pl.String, "value": pl.Float64}, orient="row"
        )
