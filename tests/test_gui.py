from pathlib import Path
from datetime import date
from types import SimpleNamespace
import sys

import pytest

from trade_exporter import gui
from trade_exporter.domain.models import Account


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "load_dotenv", lambda **options: None)
    monkeypatch.setenv("F.INVEST_TOKEN", "synthetic-finam-secret")
    monkeypatch.setenv("T.INVEST_TOKEN", "synthetic-tbank-secret")
    path = tmp_path / "config.toml"
    path.write_text(
        '[app]\nbroker="auto"\noutput_dir="exports"\n'
        '[finam]\ntoken_env="F.INVEST_TOKEN"\n'
        '[tbank]\ntoken_env="T.INVEST_TOKEN"\n', encoding="utf-8",
    )
    return path


def arguments(config, **changes):
    values = {
        "broker": "finam", "account_id": "account-1", "date_from": "01.01.26",
        "date_to": "17.10.26", "starting_capital": "100000", "config_path": config,
        "today": date(2026, 10, 17),
    }
    values.update(changes)
    return gui.build_export_args(**values)


def test_form_builds_complete_noninteractive_export_request(config):
    argv = arguments(config, starting_capital=" 100 000,50 ")
    assert argv == [
        "--config", str(config.resolve()), "--broker", "finam", "--account-id", "account-1",
        "--from", "01.01.2026", "--to", "17.10.2026", "--starting-capital", "100000.50",
        "--output-dir", str(config.parent / "exports"), "--analytics",
    ]


def test_manual_flows_replace_mode_and_keep_dated_deposit_withdrawal_signs(config):
    argv = arguments(config, cash_flow_mode="manual", cash_flows=[
        {"date": "01.01.26", "kind": "deposit", "amount": "1 000,50"},
        {"date": "17.10.2026", "kind": "withdrawal", "amount": "25.01"},
    ])
    assert argv[-6:] == [
        "--cash-flow-mode", "manual", "--cash-flow", "01.01.2026:+1000.50",
        "--cash-flow", "17.10.2026:-25.01",
    ]


def test_explicit_manual_empty_list_means_zero_external_flows(config):
    argv = arguments(config, cash_flow_mode="manual")
    assert argv[-2:] == ["--cash-flow-mode", "manual"]
    assert "--cash-flow" not in argv


def test_auto_mode_ignores_retained_manual_rows_without_validating_or_mutating_them(config):
    rows = [{"date": "outside-period", "kind": "withdrawal", "amount": "0"}]
    assert arguments(config, cash_flow_mode="auto", cash_flows=rows) == arguments(config)
    assert rows == [{"date": "outside-period", "kind": "withdrawal", "amount": "0"}]


def test_manual_identical_cash_flows_are_kept_as_two_distinct_instructions(config):
    row = {"date": "01.06.26", "kind": "deposit", "amount": "100"}
    argv = arguments(config, cash_flow_mode="manual", cash_flows=[row, dict(row)])
    assert argv.count("--cash-flow") == 2
    assert argv.count("01.06.2026:+100") == 2


@pytest.mark.parametrize("mode", ["", "both", None, "supplement"])
def test_form_rejects_ambiguous_cash_flow_mode(config, mode):
    with pytest.raises(ValueError, match="автоматический или ручной"):
        arguments(config, cash_flow_mode=mode)


@pytest.mark.parametrize("amount", ["", "0", "-1", "NaN", "Infinity", "oops", "0.0000000000000000001", "100000000000000000000"])
def test_manual_cash_flow_requires_positive_finite_representable_amount(config, amount):
    with pytest.raises(ValueError, match="Операция 1.*положительную сумму"):
        arguments(config, cash_flow_mode="manual", cash_flows=[
            {"date": "01.06.26", "kind": "deposit", "amount": amount},
        ])


@pytest.mark.parametrize("row", [
    {"date": "31.12.25", "kind": "deposit", "amount": "1"},
    {"date": "18.10.26", "kind": "deposit", "amount": "1"},
    {"date": "31.02.26", "kind": "deposit", "amount": "1"},
    {"date": "01.06.26", "kind": "transfer", "amount": "1"},
    {"date": "01.06.26", "amount": "1"},
    {}, None,
])
def test_manual_cash_flow_rejects_invalid_dates_directions_and_missing_fields(config, row):
    with pytest.raises(ValueError, match="Операция 1"):
        arguments(config, cash_flow_mode="manual", cash_flows=[row])


def test_manual_mode_requires_a_row_collection(config):
    with pytest.raises(ValueError, match="список"):
        arguments(config, cash_flow_mode="manual", cash_flows={"date": "01.06.26"})


@pytest.mark.parametrize("capital", ["", " ", "0", "-1", "NaN", "Infinity", "oops", "0.0000000000000000001", "100000000000000000000"])
def test_form_requires_positive_representable_starting_capital(config, capital):
    with pytest.raises(ValueError, match="капитал"):
        arguments(config, starting_capital=capital)


@pytest.mark.parametrize("changes", [
    {"date_from": "31.02.26"}, {"date_to": "invalid"},
    {"date_from": "18.10.26", "date_to": "17.10.26"},
    {"broker": "auto"}, {"account_id": " "},
])
def test_form_rejects_invalid_period_or_missing_explicit_selection(config, changes):
    with pytest.raises(ValueError):
        arguments(config, **changes)


@pytest.mark.parametrize("changes", [
    {"date_to": "18.10.26"},
    {"date_from": "18.10.26", "date_to": "19.10.26"},
])
def test_form_rejects_future_period_without_requesting_broker(config, changes):
    with pytest.raises(ValueError, match="сегодняшнего дня"):
        arguments(config, **changes)


def test_output_folder_is_resolved_beside_config_without_creation(config):
    argv = arguments(config, output_dir="results/new")
    destination = Path(argv[argv.index("--output-dir") + 1])
    assert destination == config.parent / "results" / "new"
    assert not destination.exists()
    config.parent.joinpath("not-a-folder").write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="папку"):
        arguments(config, output_dir=config.parent / "not-a-folder" / "nested")


def test_single_prefixed_token_selects_broker_without_exposing_token():
    assert gui.initial_broker({}, {"F.INVEST_TOKEN": " secret "}) == "finam"
    assert gui.initial_broker({}, {"T.INVEST_TOKEN": "secret"}) == "tbank"
    assert gui.initial_broker({}, {"F.INVEST_TOKEN": "f", "T.INVEST_TOKEN": "t"}) is None
    assert gui.initial_broker({}, {"INVEST_TOKEN": "ambiguous"}) is None


def test_initial_broker_honors_custom_environment_and_explicit_finam_token():
    assert gui.initial_broker({"finam": {"token_env": "CUSTOM"}}, {"CUSTOM": "f"}) == "finam"
    assert gui.initial_broker({"finam": {"token": "direct"}}, {}) == "finam"
    assert gui.initial_broker({"app": {"broker": "tbank"}}, {}) == "tbank"


def test_dotenv_is_loaded_from_application_root(config, monkeypatch):
    calls = []
    monkeypatch.setattr(gui, "load_dotenv", lambda **options: calls.append(options))
    result = gui._read_config(config.parent)
    assert calls == [{"dotenv_path": config.parent / ".env"}]
    assert result["finam"]["token_env"] == "F.INVEST_TOKEN"


def test_account_worker_uses_selected_broker_and_returns_accounts_without_trade_calls(config, monkeypatch):
    accounts = [Account("123", "test", "finam")]
    calls, events = [], []

    def create(name, settings):
        calls.append((name, settings))
        return SimpleNamespace(list_accounts=lambda: accounts)

    monkeypatch.setattr(gui, "create_broker", create)
    gui.load_accounts_worker(config.parent, "finam", lambda kind, value: events.append((kind, value)))
    assert calls == [("finam", {"token_env": "F.INVEST_TOKEN"})]
    assert ("accounts", ("finam", accounts)) in events
    assert events[-1] == ("idle", None)
    assert not any(kind == "error" for kind, _ in events)


def test_account_worker_redacts_initial_and_refreshed_tokens_on_failure(config, monkeypatch):
    events = []

    def fail():
        raise RuntimeError("synthetic-finam-secret refreshed-secret denied")

    broker = SimpleNamespace(list_accounts=fail, token="synthetic-finam-secret", _jwt="refreshed-secret")
    monkeypatch.setattr(gui, "create_broker", lambda *args: broker)
    gui.load_accounts_worker(config.parent, "finam", lambda kind, value: events.append((kind, value)))
    error = next(value for kind, value in events if kind == "error")
    assert error == "*** *** denied"
    assert events[-1] == ("idle", None)


def test_export_worker_sends_real_progress_and_stdout_through_events(config, monkeypatch):
    events, calls = [], []
    argv = arguments(config)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("GUI must not prompt in the console"))

    def run(values, *, progress):
        calls.append(values)
        progress("Загружаю сделки…")
        print("Сохранён отчёт.")
        return config.parent / "report.xlsx"

    gui.export_worker(config.parent, argv, lambda kind, value: events.append((kind, value)), runner=run)
    assert calls == [argv]
    assert ("progress", "Загружаю сделки…") in events
    assert ("log", "Сохранён отчёт.") in events
    assert ("result", config.parent / "report.xlsx") in events
    assert events[-1] == ("idle", None)


def test_export_worker_redacts_split_output_progress_and_error_without_traceback(config):
    events = []

    def run(values, *, progress):
        sys.stdout.write("synthetic-finam-")
        sys.stdout.write("secret\n")
        progress("token=synthetic-tbank-secret")
        raise RuntimeError("synthetic-finam-secret Bearer runtime-jwt eyJpayload.body.signature")

    gui.export_worker(config.parent, arguments(config), lambda kind, value: events.append((kind, value)), runner=run)
    visible = "\n".join(str(value) for kind, value in events if kind in {"progress", "log", "error"})
    for secret in ("synthetic-finam-secret", "synthetic-tbank-secret", "runtime-jwt", "eyJpayload.body.signature"):
        assert secret not in visible
    assert "Traceback" not in visible
    assert any(kind == "error" for kind, _ in events)
    assert not any(kind == "result" for kind, _ in events)
    assert events[-1] == ("idle", None)


def test_export_worker_reports_parser_exit_and_restores_streams(config):
    events = []
    stdout, stderr = sys.stdout, sys.stderr

    def run(values, *, progress):
        raise SystemExit(2)

    gui.export_worker(config.parent, arguments(config), lambda kind, value: events.append((kind, value)), runner=run)
    assert any(kind == "error" for kind, _ in events)
    assert sys.stdout is stdout
    assert sys.stderr is stderr
