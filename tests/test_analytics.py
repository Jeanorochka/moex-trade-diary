from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl
import pytest
import xlsxturbo

from trade_exporter.domain.models import ClosedTrade
from trade_exporter.services.analytics import (
    MONEY,
    analyze_file,
    analyze_frame,
    analyze_trades,
)


def diary(rows):
    return pl.DataFrame(
        rows,
        schema={
            "дата": pl.String,
            "тикер": pl.String,
            "цена входа": pl.String,
            "цена выхода": pl.String,
            "финрез": pl.String,
            "комиссия": pl.String,
        },
        orient="row",
    )


def trade(gross="10", commission="1", exit_at=None, ticker="TEST"):
    return ClosedTrade(
        exit_at=exit_at or datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        instrument_id="test",
        ticker=ticker,
        direction="long",
        quantity=1,
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        gross_pnl=Decimal(gross),
        commission=Decimal(commission),
    )


def test_known_results_and_trade_vs_daily_drawdown():
    report = analyze_frame(
        diary([
            ("01.01.26", "Z", "100", "99", "-100", "5"),
            ("01.01.26", "A", "100", "102", "205", "5"),
            ("02.01.26", "A", "100", "99", "-52", "3"),
            ("01.02.26", "Z", "100", "100", "0", "0"),
            ("01.02.26", "Z", "100", "101", "103", "3"),
        ]),
        Decimal("1000"),
    )
    result = report.summary
    assert (result["trades"], result["wins"], result["losses"], result["breakeven"]) == (5, 2, 2, 1)
    assert result["gross_pnl"] == Decimal("156")
    assert result["commission"] == Decimal("16")
    assert result["net_pnl"] == Decimal("140")
    assert result["win_rate"] == 0.4
    assert result["avg_trade"] == Decimal("28")
    assert result["avg_win"] == Decimal("150")
    assert result["avg_loss"] == Decimal("80")
    assert result["profit_factor"] == result["payoff_ratio"] == Decimal("1.875")
    assert result["best_trade"] == Decimal("200")
    assert result["worst_trade"] == Decimal("-105")
    assert result["max_drawdown"] == Decimal("105")
    assert result["max_daily_drawdown"] == Decimal("55")
    assert result["max_drawdown_pct"] == Decimal("0.105")
    assert result["return_on_capital"] == Decimal("0.14")
    assert result["trading_days"] == 3
    assert report.daily["net_pnl"].to_list() == [Decimal("95"), Decimal("-55"), Decimal("100")]
    assert report.daily["equity"].to_list() == [Decimal("1095"), Decimal("1040"), Decimal("1140")]
    assert report.daily["drawdown"].to_list() == [Decimal("0"), Decimal("55"), Decimal("0")]
    assert report.monthly["date"].to_list() == [date(2026, 1, 1), date(2026, 2, 1)]
    assert report.monthly["net_pnl"].to_list() == [Decimal("40"), Decimal("100")]
    assert report.by_ticker["ticker"].to_list() == ["A", "Z"]
    assert report.by_ticker["net_pnl"].to_list() == [Decimal("145"), Decimal("-5")]
    assert report.trades.schema["net_pnl"] == MONEY


@pytest.mark.parametrize("capital", [Decimal("0"), Decimal("100")])
def test_initial_losing_day_is_a_drawdown(capital):
    report = analyze_trades([trade("-10", "2")], timezone.utc, capital)
    assert report.summary["max_drawdown"] == Decimal("12")
    assert report.summary["max_daily_drawdown"] == Decimal("12")
    assert report.daily["drawdown"].to_list() == [Decimal("12")]
    assert report.summary["max_drawdown_pct"] == (Decimal("0.12") if capital else None)


def test_preserves_duplicates_and_intraday_order_for_streaks():
    duplicate = ("01.01.26", "A", "1", "1", "-1", "0")
    report = analyze_frame(diary([
        ("02.01.26", "LATER", "1", "1", "1", "0"),
        ("01.01.26", "Z", "1", "1", "1", "0"),
        duplicate,
        duplicate,
        ("01.01.26", "B", "1", "1", "1", "0"),
    ]))
    assert report.trades["ticker"].to_list() == ["Z", "A", "A", "B", "LATER"]
    assert report.summary["trades"] == 5
    assert report.summary["max_win_streak"] == 2
    assert report.summary["max_loss_streak"] == 2


@pytest.mark.parametrize(
    ("gross", "wins", "losses", "breakeven", "factor"),
    [("0", 0, 0, 2, None), ("10", 2, 0, 0, None), ("-10", 0, 2, 0, Decimal("0"))],
)
def test_zero_all_wins_and_all_losses(gross, wins, losses, breakeven, factor):
    report = analyze_trades([trade(gross, "0"), trade(gross, "0")], timezone.utc)
    assert report.summary["wins"] == wins
    assert report.summary["losses"] == losses
    assert report.summary["breakeven"] == breakeven
    assert report.summary["profit_factor"] == factor
    assert report.summary["payoff_ratio"] is None
    assert report.summary["return_on_capital"] is None
    assert report.summary["max_drawdown_pct"] is None


def test_empty_trades_have_typed_empty_tables_and_no_undefined_ratios():
    report = analyze_trades([], timezone.utc)
    assert report.summary["trades"] == 0
    assert report.summary["net_pnl"] == Decimal("0")
    assert report.summary["max_drawdown"] == Decimal("0")
    assert report.summary["win_rate"] is None
    assert report.summary["avg_trade"] is None
    assert report.summary["profit_factor"] is None
    assert report.summary["best_trade"] is None
    assert report.daily.is_empty()
    assert report.monthly.is_empty()
    assert report.by_ticker.is_empty()
    assert report.trades.schema["net_pnl"] == MONEY
    assert report.daily.schema["date"] == pl.Date


def test_money_is_decimal_and_commission_is_absolute():
    report = analyze_trades([
        trade("0.300000000000000000", "-0.3"),
        trade("0.300000000000000001", "0.3"),
    ], timezone.utc)
    assert report.summary["net_pnl"] == Decimal("0.000000000000000001")
    assert report.summary["wins"] == 1
    assert report.summary["breakeven"] == 1
    assert report.summary["commission"] == Decimal("0.6")


def test_unrepresentable_imported_decimal_is_rejected_before_casting():
    with pytest.raises(ValueError, match="gross_pnl"):
        analyze_frame(diary([("01.01.26", "A", "1", "2", "0.0000000000000000001", "0")]))


def test_domain_fractional_commission_is_rounded_at_eighteen_places():
    commission = str(Decimal("1") / Decimal("3"))
    report = analyze_trades([trade("1", commission)], timezone.utc)
    assert report.summary["commission"] == Decimal("0.333333333333333333")
    assert report.summary["net_pnl"] == Decimal("0.666666666666666667")


def test_domain_rounding_uses_half_even_and_does_not_depend_on_decimal_context():
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 6
        report = analyze_trades([
            trade("0.0000000000000000005", "0"),
            trade("0.0000000000000000015", "0"),
        ], timezone.utc)
    assert report.trades["gross_pnl"].to_list() == [Decimal("0"), Decimal("0.000000000000000002")]
    assert report.summary["wins"] == 1
    assert report.summary["breakeven"] == 1


def test_domain_trades_are_sorted_by_full_timestamp_stably():
    report = analyze_trades([
        trade("1", "0", datetime(2026, 1, 1, 17, tzinfo=timezone.utc), "LATE"),
        trade("-1", "0", datetime(2026, 1, 1, 10, tzinfo=timezone.utc), "EARLY"),
        trade("-1", "0", datetime(2026, 1, 1, 10, tzinfo=timezone.utc), "SAME"),
    ], timezone.utc)
    assert report.trades["ticker"].to_list() == ["EARLY", "SAME", "LATE"]
    assert report.summary["max_drawdown"] == Decimal("2")
    assert report.summary["max_loss_streak"] == 2


def test_display_timezone_changes_trade_day_and_weekends_are_retained():
    report = analyze_trades([
        trade(exit_at=datetime(2026, 1, 2, 23, tzinfo=timezone.utc)),
    ], "Europe/Moscow")
    assert report.trades["date"].to_list() == [date(2026, 1, 3)]
    assert report.daily["net_pnl"].to_list() == [Decimal("9")]


def test_naive_exit_time_is_rejected():
    with pytest.raises(ValueError, match="часовой пояс"):
        analyze_trades([trade(exit_at=datetime(2026, 1, 1))], timezone.utc)


@pytest.mark.parametrize("raw", ["03.02.26", "03.02.2026", "2026-02-03", "2026-02-03T12:34:00"])
def test_date_formats_and_localized_money(raw):
    report = analyze_frame(diary([(raw, " TEST ", "1 234,50", "1235.5", "1,25", "-0,25")]))
    assert report.trades["date"].to_list() == [date(2026, 2, 3)]
    assert report.trades["ticker"].to_list() == ["TEST"]
    assert report.trades["entry_price"].to_list() == [Decimal("1234.50")]
    assert report.summary["net_pnl"] == Decimal("1")


@pytest.mark.parametrize(
    ("column", "bad_value", "error_column"),
    [
        ("дата", "31.02.26", "date"),
        ("дата", None, "date"),
        ("тикер", "  ", "ticker"),
        ("тикер", None, "ticker"),
        ("финрез", "oops", "gross_pnl"),
        ("финрез", "NaN", "gross_pnl"),
        ("финрез", "Infinity", "gross_pnl"),
        ("комиссия", None, "commission"),
    ],
)
def test_invalid_rows_fail_with_row_and_column(column, bad_value, error_column):
    frame = diary([("01.01.26", "A", "1", "2", "1", "0")])
    frame = frame.with_columns(pl.lit(bad_value, dtype=pl.String).alias(column))
    with pytest.raises(ValueError, match=f"Строка 2, {error_column}"):
        analyze_frame(frame)


def test_missing_or_ambiguous_columns_fail():
    frame = diary([])
    with pytest.raises(ValueError, match="gross_pnl"):
        analyze_frame(frame.drop("финрез"))
    with pytest.raises(ValueError, match="Повторяющаяся колонка"):
        analyze_frame(frame.with_columns(pl.lit("0").alias("gross_pnl")))


@pytest.mark.parametrize("capital", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_starting_capital_is_rejected(capital):
    with pytest.raises(ValueError):
        analyze_trades([], timezone.utc, capital)


def test_xlsx_uses_trade_sheet_even_when_it_is_not_first(tmp_path):
    path = tmp_path / "diary.xlsx"
    xlsxturbo.dfs_to_xlsx([
        (pl.DataFrame({"metric": ["wrong"]}), "Итоги"),
        (diary([("01.01.26", "A", "1", "2", "10", "1")]), "Сделки"),
    ], str(path))
    assert analyze_file(path).summary["net_pnl"] == Decimal("9")


def test_xlsx_falls_back_to_first_sheet_only_if_trade_sheet_is_absent(tmp_path):
    path = tmp_path / "diary.xlsx"
    xlsxturbo.df_to_xlsx(
        diary([("01.01.26", "A", "1", "2", "10", "1")]),
        str(path),
        sheet_name="history",
    )
    assert analyze_file(path).summary["trades"] == 1


def test_malformed_trade_sheet_does_not_fall_back_to_valid_first_sheet(tmp_path):
    path = tmp_path / "diary.xlsx"
    xlsxturbo.dfs_to_xlsx([
        (diary([("01.01.26", "A", "1", "2", "10", "1")]), "history"),
        (pl.DataFrame({"bad_column": [1]}), "Сделки"),
    ], str(path))
    with pytest.raises(ValueError, match="Не хватает колонок"):
        analyze_file(path)


def test_malformed_xlsx_row_is_not_silently_skipped(tmp_path):
    path = tmp_path / "diary.xlsx"
    xlsxturbo.df_to_xlsx(diary([
        ("01.01.26", "A", "1", "2", "10", "1"),
        ("02.01.26", "A", "1", "2", "bad", "1"),
    ]), str(path), sheet_name="Сделки")
    with pytest.raises(ValueError, match="Строка 3, gross_pnl"):
        analyze_file(path)


def test_corrupt_xlsx_fails_clearly(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Не удалось прочитать broken.xlsx"):
        analyze_file(path)


def test_xlsx_native_dates_are_supported(tmp_path):
    path = tmp_path / "dates.xlsx"
    frame = diary([("01.01.26", "A", "1", "2", "10", "1")]).with_columns(
        pl.lit(date(2026, 2, 3)).alias("дата")
    )
    xlsxturbo.df_to_xlsx(frame, str(path), sheet_name="Сделки")
    assert analyze_file(path).trades["date"].to_list() == [date(2026, 2, 3)]


def test_header_only_xlsx_is_a_valid_empty_diary(tmp_path):
    path = tmp_path / "empty.xlsx"
    xlsxturbo.df_to_xlsx(diary([]), str(path), sheet_name="Сделки")
    assert analyze_file(path).summary["trades"] == 0


def test_csv_keeps_precise_numeric_text(tmp_path):
    path = tmp_path / "diary.csv"
    diary([("01.01.26", "A", "1", "2", "0.300000000000000001", "0.3")]).write_csv(path)
    assert analyze_file(path).summary["net_pnl"] == Decimal("0.000000000000000001")
