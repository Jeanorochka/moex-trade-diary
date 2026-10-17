from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
import pytest

from trade_exporter import cli
from trade_exporter.brokers import registry
from trade_exporter.brokers.finam import FinamBroker
from trade_exporter.brokers.tbank import TBankBroker
from trade_exporter.domain.models import Account


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    for key in ("F.INVEST_TOKEN", "T.INVEST_TOKEN", "INVEST_TOKEN", "FINAM_API_TOKEN", "FINAM_TOKEN", "TBANK_INVEST_TOKEN"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("key, expected", [("F.INVEST_TOKEN", "finam"), ("T.INVEST_TOKEN", "tbank")])
def test_only_populated_prefix_selects_its_broker_without_prompt(key, expected, monkeypatch):
    monkeypatch.setenv(key, " synthetic ")
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("single broker prompted"))
    assert cli.choose_broker(cli.parse_args([]), {}) == expected


@pytest.mark.parametrize("answer, expected", [("1", "finam"), ("2", "tbank"), ("tbank", "tbank")])
def test_both_prefixes_offer_selection_before_authentication(answer, expected, monkeypatch, capsys):
    monkeypatch.setenv("F.INVEST_TOKEN", "finam-secret")
    monkeypatch.setenv("T.INVEST_TOKEN", "tbank-secret")
    answers = iter(("wrong", answer))
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.choose_broker(cli.parse_args([]), {}) == expected
    output = capsys.readouterr().out
    assert "Финам" in output and "Т-Банк" in output
    assert "finam-secret" not in output and "tbank-secret" not in output


@pytest.mark.parametrize("broker", ["finam", "tbank"])
def test_explicit_broker_skips_choice_with_both_prefixes(broker, monkeypatch):
    monkeypatch.setenv("F.INVEST_TOKEN", "finam-secret")
    monkeypatch.setenv("T.INVEST_TOKEN", "tbank-secret")
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("explicit broker prompted"))
    assert cli.choose_broker(cli.parse_args(["--broker", broker]), {}) == broker


def test_no_prefixed_token_gives_configuration_error_without_guessing(monkeypatch):
    monkeypatch.setenv("INVEST_TOKEN", "ambiguous-secret")
    monkeypatch.setenv("F.INVEST_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="F.INVEST_TOKEN") as error:
        cli.choose_broker(cli.parse_args([]), {})
    assert "ambiguous-secret" not in str(error.value)


def test_both_tokens_without_input_require_explicit_broker(monkeypatch):
    monkeypatch.setenv("F.INVEST_TOKEN", "finam-secret")
    monkeypatch.setenv("T.INVEST_TOKEN", "tbank-secret")

    def closed_input(*args):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    with pytest.raises(RuntimeError, match="--broker finam"):
        cli.choose_broker(cli.parse_args([]), {})


def test_direct_finam_token_selects_finam_without_scanning_other_credentials(monkeypatch):
    monkeypatch.setenv("T.INVEST_TOKEN", "tbank-secret")
    assert cli.choose_broker(cli.parse_args(["--token", "finam-direct"]), {}) == "finam"


def test_configured_broker_and_custom_token_variable_remain_explicit(monkeypatch):
    monkeypatch.setenv("T.INVEST_TOKEN", "tbank-secret")
    assert cli.choose_broker(cli.parse_args([]), {"app": {"broker": "FINAM"}}) == "finam"
    monkeypatch.delenv("T.INVEST_TOKEN")
    monkeypatch.setenv("CUSTOM_F", "finam-secret")
    assert cli.choose_broker(cli.parse_args([]), {"finam": {"token_env": "CUSTOM_F"}}) == "finam"


@pytest.mark.parametrize("selected, both", [("finam", False), ("tbank", False), ("finam", True), ("tbank", True)])
def test_root_dotenv_routes_each_token_only_to_its_adapter(selected, both, tmp_path, monkeypatch, capsys):
    config = Path(__file__).resolve().parents[1] / "config.toml"
    config_copy = tmp_path / "config.toml"
    config_copy.write_bytes(config.read_bytes())
    env_path = tmp_path / ".env"
    f_value = "finam-secret" if both or selected == "finam" else ""
    t_value = "tbank-secret" if both or selected == "tbank" else ""
    env_path.write_text(f"F.INVEST_TOKEN={f_value}\nT.INVEST_TOKEN = {t_value}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "load_dotenv", lambda: load_dotenv(env_path))
    monkeypatch.setattr("primp.Client", lambda **kwargs: SimpleNamespace())
    created = []

    def create(name, settings):
        adapter = registry.create_broker(name, settings)
        created.append((name, adapter.token))
        return adapter

    account = Account("1", "test", selected)
    monkeypatch.setattr(cli, "create_broker", create)
    monkeypatch.setattr(FinamBroker, "list_accounts", lambda *a: [account])
    monkeypatch.setattr(TBankBroker, "list_accounts", lambda *a: [account])
    monkeypatch.setattr(cli, "print_portfolio", lambda *a: ())
    monkeypatch.setattr("builtins.input", lambda *a: "1" if selected == "finam" else "2")
    assert cli.main(["--config", str(config_copy), "--portfolio", "--account-id", "1"]) == ()
    assert created == [(selected, "finam-secret" if selected == "finam" else "tbank-secret")]
    output = capsys.readouterr().out
    assert "finam-secret" not in output and "tbank-secret" not in output
