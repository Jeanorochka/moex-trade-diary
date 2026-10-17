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
    assert "Excel:" in capsys.readouterr().out


def test_export_and_analyze_share_selected_closed_trades(tmp_path, monkeypatch):
    account = Account("1", "test", "fake", opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc))

    class Broker:
        name = "fake"

        def list_accounts(self):
            return [account]

        def get_fills(self, **kwargs):
            return [make_fill(9, Side.BUY, "100"), make_fill(10, Side.SELL, "110")]

    monkeypatch.setattr(cli, "create_broker", lambda *args: Broker())
    monkeypatch.setattr(cli, "print_portfolio", lambda *args, **kwargs: ())
    monkeypatch.setattr(cli, "prepare_portfolio_report", lambda *args, **kwargs: None)
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    path = cli.main([
        "--config", str(config), "--broker", "tbank", "--account-id", "1",
        "--from", "11.09.26", "--to", "11.09.26",
        "--output-dir", str(tmp_path), "--analytics",
    ])
    assert path.exists()
    payload = orjson.loads(path.with_suffix(".json").read_bytes())
    assert payload["summary"]["trades"] == 1
    assert Decimal(payload["summary"]["net_pnl"]) == 10
    assert list(tmp_path.glob("*.xlsx")) == [path]
    assert list(tmp_path.glob("*.json")) == [path.with_suffix(".json")]
    assert not list(tmp_path.rglob("*.html"))


def test_empty_diary_analysis_only_prints_saved_paths(tmp_path, capsys):
    source = ExcelExporter(tmp_path).export(
        [], "tbank", "123456789",
        datetime(2026, 9, 11, tzinfo=timezone.utc),
        datetime(2026, 9, 12, tzinfo=timezone.utc), timezone.utc,
    )
    paths = cli.main(["--analyze", str(source)])
    assert orjson.loads(paths[1].read_bytes())["summary"]["trades"] == 0
    output = capsys.readouterr().out
    assert output.splitlines() == [f"Excel: {paths[0].resolve()}", f"JSON: {paths[1].resolve()}"]


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


@pytest.mark.parametrize("broker_name", ["finam", "tbank"])
def test_current_portfolio_does_not_prompt_dates_or_request_closed_trades(broker_name, tmp_path, monkeypatch):
    account = Account("1", "test", broker_name, opened_at=datetime(2025, 1, 1, tzinfo=timezone.utc))

    class Broker:
        name = broker_name

        def list_accounts(self):
            return [account]

        def get_fills(self, **kwargs):
            pytest.fail("current portfolio requested closed trades")

    requests = []
    expected = (tmp_path / "portfolio.xlsx", tmp_path / "portfolio.json")
    monkeypatch.setattr(cli, "create_broker", lambda *args: Broker())
    monkeypatch.setattr(cli, "print_portfolio", lambda *args, **kwargs: requests.append(args) or expected)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("standalone portfolio prompted date"))
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    result = cli.main(["--config", str(config), "--broker", broker_name, "--account-id", "1", "--portfolio", "--output-dir", str(tmp_path)])
    assert result == expected
    assert len(requests) == 1
    assert requests[0][1] == account


def test_current_portfolio_rejects_historical_date_label():
    with pytest.raises(SystemExit):
        cli.parse_args(["--portfolio", "--to", "11.09.26"])


@pytest.mark.parametrize("broker_name", ["finam", "tbank"])
def test_trade_history_error_still_saves_prepared_nav_report(broker_name, tmp_path, monkeypatch):
    from types import SimpleNamespace

    account = Account("1", "test", broker_name, opened_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    prepared = SimpleNamespace(summary={"equity": Decimal(120)})
    saved = []

    def unavailable(*args, **kwargs):
        raise RuntimeError("trade history unavailable")

    broker = SimpleNamespace(name=broker_name, list_accounts=lambda: [account], get_fills=unavailable)
    monkeypatch.setattr(cli, "create_broker", lambda *args: broker)
    monkeypatch.setattr(cli, "prepare_portfolio_report", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(cli, "execute_finam", unavailable)
    monkeypatch.setattr(cli, "print_portfolio", lambda *args, **kwargs: saved.append(kwargs["report"]))
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="trade history unavailable"):
        cli.main([
            "--config", str(config), "--broker", broker_name, "--account-id", "1",
            "--from", "11.09.26", "--to", "11.09.26", "--output-dir", str(tmp_path),
        ])
    assert saved == [prepared]


def test_source_broker_report_is_embedded_without_extra_output_files(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from trade_exporter.services.analytics import analyze_trades

    account = Account("1", "test", "finam", opened_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    broker = SimpleNamespace(name="finam", list_accounts=lambda: [account])
    source_directories = []

    def prepare(*args, **kwargs):
        assert kwargs["write_output"] is False
        directory = Path(kwargs["source_report_dir"])
        source_directories.append(directory)
        source = directory / "broker_source.html"
        source.write_text("<html>source variation margin</html>", encoding="utf-8")
        report = analyze_trades([sample_trade()], timezone.utc)
        report.details["source_reports"] = [source]
        return None, report, [source]

    monkeypatch.setattr(cli, "create_broker", lambda *args: broker)
    monkeypatch.setattr(cli, "prepare_portfolio_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "execute_finam", prepare)
    config = tmp_path / "config.toml"
    config.write_text('[app]\ntimezone = "UTC"\n', encoding="utf-8")
    path = cli.main([
        "--config", str(config), "--broker", "finam", "--account-id", "1",
        "--from", "11.09.26", "--to", "11.09.26", "--output-dir", str(tmp_path),
    ])
    assert list(tmp_path.glob("*.xlsx")) == [path]
    assert list(tmp_path.glob("*.json")) == [path.with_suffix(".json")]
    assert not list(tmp_path.rglob("*.html"))
    assert not source_directories[0].exists()
    payload = orjson.loads(path.with_suffix(".json").read_bytes())
    assert payload["details"]["source_reports"] == [{
        "name": "broker_source.html", "encoding": "utf-8",
        "content": "<html>source variation margin</html>",
    }]
