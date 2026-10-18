from datetime import datetime, timezone
from decimal import Decimal
import os
from types import SimpleNamespace

import orjson
import polars as pl
import pytest

from trade_exporter import cli
from trade_exporter.brokers import finam, registry
from trade_exporter.domain.models import Account, Fill, Side
from trade_exporter.domain.portfolio import CashBalance, PortfolioSnapshot
from trade_exporter.services.portfolio import analyze_portfolio
from trade_exporter.exporters.portfolio import export_portfolio
from trade_exporter.exporters.excel import ExcelExporter
from test_excel_exporter import sample_trade

UTC = timezone.utc


@pytest.fixture
def integration(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    def current_portfolio(broker, account, output_dir, **options):
        snapshot = PortfolioSnapshot(account.id, broker.name, datetime.now(UTC), "RUB", Decimal(100), (CashBalance("RUB", Decimal(100)),), source="synthetic broker")
        report = analyze_portfolio(snapshot)
        return report, export_portfolio(report, output_dir)

    monkeypatch.setattr(cli, "execute_portfolio", current_portfolio)
    monkeypatch.setattr(cli, "prepare_portfolio_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(finam.primp, "Client", lambda **kwargs: SimpleNamespace())
    config = tmp_path / "config.toml"
    config.write_text(
        '[app]\ntimezone = "UTC"\nreconstruction_mode = "full"\n'
        '[excel]\nextra_columns = true\n',
        encoding="utf-8",
    )
    accounts = [
        Account("skip-account", "other", "finam"),
        Account(
            "selected-account", "finam test", "finam",
            opened_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    ]
    fills = [
        Fill(
            executed_at=datetime(2026, 9, 10, 9, tzinfo=UTC),
            instrument_id="SBER@MISX", ticker="SBER", instrument_type="share",
            side=Side.BUY, quantity=2, pnl_price=Decimal("100"),
            display_price=Decimal("100"), commission=Decimal("2"), currency="RUB",
        ),
        Fill(
            executed_at=datetime(2026, 9, 11, 10, tzinfo=UTC),
            instrument_id="SBER@MISX", ticker="SBER", instrument_type="share",
            side=Side.SELL, quantity=1, pnl_price=Decimal("110"),
            display_price=Decimal("110"), commission=Decimal("1"), currency="RUB",
        ),
    ]
    state = SimpleNamespace(created=[], fill_requests=[], accounts=accounts, fills=fills)

    def create_broker(name, settings):
        broker = registry.create_broker(name, settings)
        state.created.append(broker)
        return broker

    def load_trades(self, account_id, date_from, date_to):
        state.fill_requests.append((account_id, date_from, date_to))
        return [
            {
                "trade_id": str(index),
                "account_id": account_id,
                "symbol": fill.instrument_id,
                "timestamp": fill.executed_at.isoformat(),
                "side": f"SIDE_{fill.side.value}",
                "price": {"value": str(fill.display_price)},
                "size": {"value": str(fill.quantity)},
                "currency": fill.currency,
                "commission": {"value": {"value": str(fill.commission)}, "currency": fill.currency},
            }
            for index, fill in enumerate(fills, 1)
        ]

    monkeypatch.setattr(cli, "create_broker", create_broker)
    monkeypatch.setattr(finam.FinamBroker, "list_accounts", lambda self: accounts)
    monkeypatch.setattr(finam.FinamBroker, "load_trades", load_trades)
    monkeypatch.setattr(finam.FinamBroker, "_asset", lambda *args: {
        "type": "SHARES", "ticker": "SBER", "mic": "MISX",
    })
    state.arguments = [
        "--config", str(config), "--broker", "finam",
        "--from", "11.09.26", "--to", "11.09.26",
        "--output-dir", str(tmp_path),
    ]
    return state


def test_finam_env_token_selects_account_and_exports_analytics_by_default(
    integration, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("F.INVEST_TOKEN", "test-env-secret")
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    path = cli.main(integration.arguments)
    assert path.exists()
    assert integration.created[0].token == "test-env-secret"
    assert path.name == "11.09.2026_11.09.2026.xlsx"
    assert integration.fill_requests == [(
        "selected-account", datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 12, tzinfo=UTC),
    )]
    payload = orjson.loads(path.with_suffix(".json").read_bytes())
    assert payload["summary"]["trades"] == 1
    assert Decimal(payload["summary"]["net_pnl"]) == 8
    assert list(tmp_path.glob("*.xlsx")) == [path]
    assert list(tmp_path.glob("*.json")) == [path.with_suffix(".json")]
    assert not list(tmp_path.rglob("*.html"))
    assert list(pl.read_excel(path, sheet_id=0)) == ["Отчёт"]
    assert "finam test" in capsys.readouterr().out


def test_finam_cli_token_overrides_environment_without_mutating_it(
    integration, monkeypatch
):
    monkeypatch.setenv("F.INVEST_TOKEN", "old-env-secret")
    path = cli.main(integration.arguments + [
        "--account-id", "selected-account", "--token", "test-cli-secret",
    ])
    assert path.exists()
    assert integration.created[0].token == "test-cli-secret"
    assert os.environ["F.INVEST_TOKEN"] == "old-env-secret"


def test_finam_cli_token_works_without_environment(integration, monkeypatch):
    monkeypatch.delenv("F.INVEST_TOKEN", raising=False)
    monkeypatch.delenv("FINAM_TOKEN", raising=False)
    path = cli.main(integration.arguments + [
        "--account-id", "selected-account", "--token", "test-cli-secret",
    ])
    assert path.exists()
    assert integration.created[0].token == "test-cli-secret"


def test_tbank_exports_one_workbook_with_analytics(integration, monkeypatch, tmp_path):
    broker = SimpleNamespace(
        name="tbank", list_accounts=lambda: integration.accounts,
        get_fills=lambda **kwargs: integration.fills,
    )
    monkeypatch.setattr(cli, "create_broker", lambda *args: broker)
    arguments = list(integration.arguments)
    arguments[arguments.index("finam")] = "tbank"
    path = cli.main(arguments + ["--account-id", "selected-account"])
    assert path.exists()
    assert not list(tmp_path.glob("analytics_*"))
    assert list(tmp_path.glob("*.xlsx")) == [path]
    assert path.with_suffix(".json").is_file()
    assert not list(tmp_path.rglob("*.html"))
    assert list(pl.read_excel(path, sheet_id=0)) == ["Отчёт"]


def test_offline_analysis_remains_independent_of_finam(tmp_path, monkeypatch):
    monkeypatch.delenv("F.INVEST_TOKEN", raising=False)
    monkeypatch.delenv("FINAM_TOKEN", raising=False)
    monkeypatch.setattr(cli, "create_broker", lambda *args: pytest.fail("broker was called"))
    monkeypatch.setattr(cli, "load_config", lambda *args: pytest.fail("config was called"))
    source = ExcelExporter(tmp_path).export(
        [sample_trade()], "finam", "selected-account",
        datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC), UTC,
    )
    paths = cli.main(["--analyze", str(source)])
    assert all(path.exists() for path in paths)
    summary = orjson.loads(paths[1].read_bytes())["summary"]
    assert summary["trades"] == 1
    assert Decimal(summary["net_pnl"]) == Decimal("41.70")


def test_offline_analysis_rejects_unused_token():
    with pytest.raises(SystemExit):
        cli.parse_args(["--analyze", "diary.xlsx", "--token", "unused"])
