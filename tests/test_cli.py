from datetime import datetime, timezone
from decimal import Decimal

import orjson
import pytest

from trade_exporter import cli
from trade_exporter.domain.models import Account, Side
from trade_exporter.exporters.excel import ExcelExporter
from conftest import make_fill
from test_excel_exporter import sample_trade


def test_analyze_runs_without_config_token_or_broker(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TBANK_INVEST_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "create_broker", lambda *args: pytest.fail("offline analysis touched broker"))
    source = ExcelExporter(tmp_path).export(
        [sample_trade()], "tbank", "123456789",
        datetime(2026, 9, 11, tzinfo=timezone.utc),
        datetime(2026, 9, 12, tzinfo=timezone.utc), timezone.utc,
    )
    source_bytes = source.read_bytes()
    paths = cli.main(["--analyze", str(source), "--starting-capital", "1000"])
    assert all(path.exists() for path in paths)
    payload = orjson.loads(paths[1].read_bytes())
    assert payload["summary"]["trades"] == 1
    assert Decimal(payload["summary"]["net_pnl"]) == Decimal("41.70")
    assert source.read_bytes() == source_bytes
    assert "Аналитика и графики:" in capsys.readouterr().out


def test_export_and_analyze_share_selected_closed_trades(tmp_path, monkeypatch):
    account = Account("1", "test", "fake", opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc))

    class Broker:
        name = "fake"

        def list_accounts(self):
            return [account]

        def get_fills(self, **kwargs):
            return [make_fill(9, Side.BUY, "100"), make_fill(10, Side.SELL, "110")]

    monkeypatch.setattr(cli, "create_broker", lambda *args: Broker())
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    path = cli.main([
        "--config", str(config), "--account-id", "1",
        "--from", "11.09.26", "--to", "11.09.26",
        "--output-dir", str(tmp_path), "--analytics",
    ])
    assert path.exists()
    payload = orjson.loads(next(tmp_path.glob("analytics_*.json")).read_bytes())
    assert payload["summary"]["trades"] == 1
    assert Decimal(payload["summary"]["net_pnl"]) == 10


def test_empty_diary_analysis_prints_undefined_ratios(tmp_path, capsys):
    source = ExcelExporter(tmp_path).export(
        [], "tbank", "123456789",
        datetime(2026, 9, 11, tzinfo=timezone.utc),
        datetime(2026, 9, 12, tzinfo=timezone.utc), timezone.utc,
    )
    paths = cli.main(["--analyze", str(source)])
    assert orjson.loads(paths[1].read_bytes())["summary"]["trades"] == 0
    assert "Win rate: n/a" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "oops"])
def test_invalid_capital_is_rejected_before_any_broker_call(value):
    with pytest.raises(SystemExit):
        cli.parse_args(["--starting-capital", value])


def test_offline_analysis_does_not_silently_ignore_date_filters():
    with pytest.raises(SystemExit):
        cli.parse_args(["--analyze", "trades.xlsx", "--from", "11.09.26"])


def test_console_entrypoint_does_not_return_paths_as_exit_status(monkeypatch, tmp_path):
    calls = []

    def main():
        calls.append(True)
        return tmp_path / "result.xlsx"

    monkeypatch.setattr(cli, "main", main)
    assert cli.run() is None
    assert calls == [True]
