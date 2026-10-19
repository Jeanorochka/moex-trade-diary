from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from trade_exporter import cli
from trade_exporter.brokers.registry import create_broker
from trade_exporter.config import load_config


BROKERS = {"finam": "Финам", "tbank": "Т-Банк"}
TOKEN_ENVS = {"finam": "F.INVEST_TOKEN", "tbank": "T.INVEST_TOKEN"}
CASH_FLOW_KINDS = {"deposit": "Пополнение", "withdrawal": "Вывод"}


def _day(value: str) -> date:
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(str(value).strip(), pattern).date()
        except ValueError:
            pass
    raise ValueError("Укажите дату в формате дд.мм.гг или дд.мм.гггг.")


def _positive_decimal(value, message: str) -> Decimal:
    raw = "".join(str(value or "").split()).replace(",", ".")
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount <= 0:
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            represented = amount.quantize(Decimal("1e-18"))
        if represented != amount or amount >= Decimal("1e20"):
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError(message) from error
    return amount


def _manual_cash_flow(row, first: date, last: date) -> dict[str, str]:
    if not isinstance(row, dict):
        raise ValueError("Для операции нужны дата, направление и сумма.")
    day = _day(row.get("date", ""))
    if not first <= day <= last:
        raise ValueError("Дата пополнения или вывода должна входить в выбранный период.")
    kind = str(row.get("kind", "")).strip().lower()
    if kind not in CASH_FLOW_KINDS:
        raise ValueError("Выберите направление операции: пополнение или вывод.")
    amount = _positive_decimal(
        row.get("amount"), "Укажите положительную сумму операции: до 20 целых и 18 дробных цифр.",
    )
    return {"date": day.strftime("%d.%m.%Y"), "kind": kind, "amount": format(amount, "f")}


def build_export_args(
    broker, account_id, date_from, date_to, starting_capital,
    output_dir=None, config_path="config.toml", *, today: date | None = None,
    cash_flow_mode="auto", cash_flows=(),
) -> list[str]:
    broker = str(broker).strip().lower()
    if broker not in BROKERS:
        raise ValueError("Выберите брокера: Финам или Т-Банк.")
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("Сначала загрузите и выберите счёт.")
    first, last = _day(date_from), _day(date_to)
    if first > last:
        raise ValueError("Дата начала должна быть не позже даты окончания.")
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    if first > today or last > today:
        raise ValueError("Даты периода не могут быть позже сегодняшнего дня.")
    capital = _positive_decimal(
        starting_capital, "Укажите положительный начальный капитал: до 20 целых и 18 дробных цифр.",
    )
    cash_flow_mode = str(cash_flow_mode).strip().lower()
    if cash_flow_mode not in {"auto", "manual"}:
        raise ValueError("Выберите автоматический или ручной ввод пополнений и выводов.")
    flow_arguments = []
    if cash_flow_mode == "manual":
        if not isinstance(cash_flows, (list, tuple)):
            raise ValueError("Укажите список пополнений и выводов.")
        flow_arguments = ["--cash-flow-mode", "manual"]
        for index, row in enumerate(cash_flows, 1):
            try:
                flow = _manual_cash_flow(row, first, last)
            except ValueError as error:
                raise ValueError(f"Операция {index}: {error}") from error
            sign = "+" if flow["kind"] == "deposit" else "-"
            flow_arguments.extend(("--cash-flow", f"{flow['date']}:{sign}{flow['amount']}"))
    config_path = Path(config_path).resolve()
    raw_output = str(output_dir or "").strip()
    if "\x00" in raw_output:
        raise ValueError("Укажите корректную папку результатов.")
    output_path = Path(raw_output) if raw_output else Path("exports")
    if not output_path.is_absolute():
        output_path = config_path.parent / output_path
    output_path = output_path.resolve()
    for candidate in (output_path, *output_path.parents):
        if candidate.exists():
            if not candidate.is_dir():
                raise ValueError("Путь результатов должен указывать на папку.")
            break
    return [
        "--config", str(config_path), "--broker", broker, "--account-id", account_id,
        "--from", first.strftime("%d.%m.%Y"), "--to", last.strftime("%d.%m.%Y"),
        "--starting-capital", format(capital, "f"), "--output-dir", str(output_path),
        "--analytics",
    ] + flow_arguments


def initial_broker(config: dict, environment=None) -> str | None:
    environment = os.environ if environment is None else environment
    candidates = []
    for name in BROKERS:
        settings = config.get(name, {})
        key = str(settings.get("token_env", TOKEN_ENVS[name]))
        token = settings.get("token") if name == "finam" else None
        if str(token or environment.get(key, "")).strip():
            candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    configured = str(config.get("app", {}).get("broker", "auto")).strip().lower()
    return configured if configured in BROKERS else None


def _read_config(root_dir: Path) -> dict:
    load_dotenv(dotenv_path=root_dir / ".env")
    return load_config(root_dir / "config.toml")


def _secrets(config: dict, broker=None) -> tuple[str, ...]:
    values = []
    for name in BROKERS:
        settings = config.get(name, {})
        key = str(settings.get("token_env", TOKEN_ENVS[name]))
        values.extend((os.getenv(key, ""), settings.get("token", "")))
    if broker is not None:
        values.extend(getattr(broker, name, "") for name in ("token", "_jwt", "_secret"))
    return tuple(str(value).strip() for value in values if value and str(value).strip())


def _redact(value, secrets=()) -> str:
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", r"\1***", text)
    return re.sub(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "***", text)


class _LogWriter:
    encoding = "utf-8"

    def __init__(self, emit, secrets):
        self.emit = emit
        self.secrets = secrets
        self.buffer = ""

    def write(self, value):
        self.buffer += str(value)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.emit("log", _redact(line, self.secrets))
        return len(str(value))

    def flush(self):
        if self.buffer.strip():
            self.emit("log", _redact(self.buffer, self.secrets))
        self.buffer = ""


def load_accounts_worker(root_dir: Path, broker_name: str, emit) -> None:
    config, broker = {}, None
    try:
        if broker_name not in BROKERS:
            raise ValueError("Выберите брокера.")
        config = _read_config(Path(root_dir))
        emit("progress", f"Загружаю счета: {BROKERS[broker_name]}…")
        broker = create_broker(broker_name, dict(config.get(broker_name, {})))
        accounts = broker.list_accounts()
        if not accounts:
            raise RuntimeError("У выбранного брокера нет доступных счетов.")
        emit("accounts", (broker_name, accounts))
        emit("progress", f"Счета загружены: {len(accounts)}.")
    except (Exception, SystemExit) as error:
        emit("error", _redact(error, _secrets(config, broker)))
    finally:
        emit("idle", None)


def export_worker(root_dir: Path, argv: list[str], emit, runner=None) -> None:
    config = {}
    writer = None
    try:
        config = _read_config(Path(root_dir))
        secrets = _secrets(config)
        writer = _LogWriter(emit, secrets)
        progress = lambda message: emit("progress", _redact(message, secrets))
        progress("Начинаю выгрузку и расчёт аналитики…")
        with redirect_stdout(writer), redirect_stderr(writer):
            result = (runner or cli.main)(argv, progress=progress)
        writer.flush()
        emit("result", result)
    except (Exception, SystemExit) as error:
        if writer is not None:
            writer.flush()
        emit("error", _redact(error, _secrets(config)))
    finally:
        emit("idle", None)


def run_gui(root_dir: Path | None = None) -> None:
    try:
        from PySide6.QtCore import QLocale
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise RuntimeError("Установите зависимости интерфейса: uv sync --locked") from error
    from trade_exporter.ui.theme import configure_app
    from trade_exporter.ui.windows import ExportWindow

    root_dir = Path(root_dir).resolve() if root_dir is not None else Path(__file__).resolve().parents[2]
    QLocale.setDefault(QLocale(QLocale.Language.Russian, QLocale.Country.Russia))
    existing = QApplication.instance()
    application = existing or QApplication([])
    configure_app(application)
    application.report_window = ExportWindow(root_dir)
    application.report_window.show()
    if existing is None:
        application.exec()
