from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import os
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Thread
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
    raise ValueError("Укажи дату в формате дд.мм.гг или дд.мм.гггг.")


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
        raise ValueError("Выбери направление операции: пополнение или вывод.")
    amount = _positive_decimal(
        row.get("amount"), "Укажи положительную сумму операции: до 20 целых и 18 дробных цифр.",
    )
    return {"date": day.strftime("%d.%m.%Y"), "kind": kind, "amount": format(amount, "f")}


def build_export_args(
    broker, account_id, date_from, date_to, starting_capital,
    output_dir=None, config_path="config.toml", *, today: date | None = None,
    cash_flow_mode="auto", cash_flows=(),
) -> list[str]:
    broker = str(broker).strip().lower()
    if broker not in BROKERS:
        raise ValueError("Выбери брокера: Финам или Т-Банк.")
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("Сначала загрузи и выбери счёт.")
    first, last = _day(date_from), _day(date_to)
    if first > last:
        raise ValueError("Дата начала должна быть не позже даты окончания.")
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    if first > today or last > today:
        raise ValueError("Даты периода не могут быть позже сегодняшнего дня.")
    capital = _positive_decimal(
        starting_capital, "Укажи положительный начальный капитал: до 20 целых и 18 дробных цифр.",
    )
    cash_flow_mode = str(cash_flow_mode).strip().lower()
    if cash_flow_mode not in {"auto", "manual"}:
        raise ValueError("Выбери автоматический или ручной ввод пополнений и выводов.")
    flow_arguments = []
    if cash_flow_mode == "manual":
        if not isinstance(cash_flows, (list, tuple)):
            raise ValueError("Укажи список пополнений и выводов.")
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
        raise ValueError("Укажи корректную папку результатов.")
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
            raise ValueError("Выбери брокера.")
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


class _Window:
    def __init__(self, root_dir: Path):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.root_dir = root_dir
        self.config_path = root_dir / "config.toml"
        self.events = Queue()
        self.accounts = []
        self.cash_flows = []
        self.busy = False
        self.window = tk.Tk()
        self.window.title("Расширенный брокерский отчет")
        self.window.geometry("700x760")
        self.window.minsize(660, 710)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        try:
            config = _read_config(root_dir)
            self.timezone = ZoneInfo(str(config.get("app", {}).get("timezone", "Europe/Moscow")))
            today = datetime.now(self.timezone).date()
        except Exception as error:
            messagebox.showerror("Не удалось открыть программу", _redact(error, _secrets({})), parent=self.window)
            self.window.destroy()
            raise
        selected = initial_broker(config)
        output = Path(str(config.get("app", {}).get("output_dir", "exports")))
        if not output.is_absolute():
            output = root_dir / output
        self.broker = tk.StringVar(value=BROKERS.get(selected, ""))
        self.account = tk.StringVar()
        self.first = tk.StringVar(value=today.replace(month=1, day=1).strftime("%d.%m.%y"))
        self.last = tk.StringVar(value=today.strftime("%d.%m.%y"))
        self.capital = tk.StringVar()
        self.output = tk.StringVar(value=str(output))
        self.cash_flow_mode = tk.StringVar(value="auto")
        self.flow_date = tk.StringVar(value=today.strftime("%d.%m.%y"))
        self.flow_kind = tk.StringVar(value=CASH_FLOW_KINDS["deposit"])
        self.flow_amount = tk.StringVar()
        self.status = tk.StringVar(value="Выбери брокера и загрузи счета.")
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(11, weight=1)
        ttk.Label(body, text="Выгрузка сделок и аналитика", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 15))
        ttk.Label(body, text="Брокер").grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.broker_box = ttk.Combobox(body, textvariable=self.broker, values=list(BROKERS.values()), state="readonly")
        self.broker_box.grid(row=1, column=1, sticky="ew", pady=4)
        self.broker_box.bind("<<ComboboxSelected>>", self._broker_changed)
        self.load_button = ttk.Button(body, text="Загрузить счета", command=self._load_accounts)
        self.load_button.grid(row=1, column=2, padx=(8, 0))
        ttk.Label(body, text="Счёт").grid(row=2, column=0, sticky="w")
        self.account_box = ttk.Combobox(body, textvariable=self.account, state="readonly")
        self.account_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        dates = ttk.Frame(body)
        dates.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(body, text="Период").grid(row=3, column=0, sticky="w")
        ttk.Label(dates, text="с").pack(side="left")
        first_entry = ttk.Entry(dates, textvariable=self.first, width=13)
        first_entry.pack(side="left", padx=(6, 16))
        ttk.Label(dates, text="по").pack(side="left")
        last_entry = ttk.Entry(dates, textvariable=self.last, width=13)
        last_entry.pack(side="left", padx=6)
        ttk.Label(dates, text="дд.мм.гг").pack(side="left", padx=8)
        ttk.Label(body, text="Капитал на дату «с» *").grid(row=4, column=0, sticky="w")
        capital_entry = ttk.Entry(body, textvariable=self.capital)
        capital_entry.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(body, text="Стоимость портфеля на начало периода, в валюте счёта.", foreground="#5c6570", wraplength=420).grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(body, text="Папка результатов").grid(row=6, column=0, sticky="w")
        output_entry = ttk.Entry(body, textvariable=self.output)
        output_entry.grid(row=6, column=1, sticky="ew", pady=4)
        self.folder_button = ttk.Button(body, text="Обзор…", command=self._choose_folder)
        self.folder_button.grid(row=6, column=2, padx=(8, 0))
        flow_panel = ttk.LabelFrame(body, text="Пополнения и выводы", padding=(10, 6))
        flow_panel.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        flow_panel.columnconfigure(0, weight=1)
        modes = ttk.Frame(flow_panel)
        modes.grid(row=0, column=0, columnspan=2, sticky="w")
        self.flow_mode_buttons = []
        for label, mode in (("Из данных брокера", "auto"), ("Ввести вручную", "manual")):
            button = ttk.Radiobutton(modes, text=label, variable=self.cash_flow_mode, value=mode, command=self._cash_flow_mode_changed)
            button.pack(side="left", padx=(0, 18))
            self.flow_mode_buttons.append(button)
        ttk.Label(flow_panel, text="Укажи все пополнения и выводы за период.").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 4))
        flow_inputs = ttk.Frame(flow_panel)
        flow_inputs.grid(row=2, column=0, columnspan=2, sticky="ew")
        for column, label in enumerate(("Дата, дд.мм.гг", "Направление", "Сумма")):
            ttk.Label(flow_inputs, text=label).grid(row=0, column=column, sticky="w")
        self.flow_date_entry = ttk.Entry(flow_inputs, textvariable=self.flow_date, width=12)
        self.flow_date_entry.grid(row=1, column=0, padx=(0, 6))
        self.flow_kind_box = ttk.Combobox(flow_inputs, textvariable=self.flow_kind, values=list(CASH_FLOW_KINDS.values()), state="readonly", width=14)
        self.flow_kind_box.grid(row=1, column=1, padx=(0, 6))
        self.flow_amount_entry = ttk.Entry(flow_inputs, textvariable=self.flow_amount, width=17)
        self.flow_amount_entry.grid(row=1, column=2, padx=(0, 6))
        self.add_flow_button = ttk.Button(flow_inputs, text="Добавить", command=self._add_cash_flow)
        self.add_flow_button.grid(row=1, column=3)
        self.cash_flow_list = tk.Listbox(flow_panel, height=4, exportselection=False, font=("Segoe UI", 9))
        self.cash_flow_list.grid(row=3, column=0, sticky="ew", pady=5)
        self.remove_flow_button = ttk.Button(flow_panel, text="Удалить", command=self._remove_cash_flow)
        self.remove_flow_button.grid(row=3, column=1, sticky="n", padx=(8, 0), pady=5)
        ttk.Label(
            flow_panel, text="Ручной список заменяет внешние потоки брокера. Пустой список — операций не было.\nСуммы в валюте счёта; даты учитываются на конец дня.",
            foreground="#5c6570", wraplength=630,
        ).grid(row=4, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(body, text="Выгрузить и рассчитать", command=self._start)
        self.start_button.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew")
        ttk.Label(body, textvariable=self.status, wraplength=630).grid(row=10, column=0, columnspan=3, sticky="w", pady=8)
        log_frame = ttk.Frame(body)
        log_frame.grid(row=11, column=0, columnspan=3, sticky="nsew")
        self.log = tk.Text(log_frame, height=5, state="disabled", wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.entries = [first_entry, last_entry, capital_entry, output_entry]
        self._cash_flow_mode_changed()
        self.window.after(80, self._poll)

    def _broker_name(self):
        return next((name for name, label in BROKERS.items() if label == self.broker.get()), "")

    def _broker_changed(self, event=None):
        self.accounts = []
        self.account.set("")
        self.account_box.configure(values=[])

    def _emit(self, kind, payload):
        self.events.put((kind, payload))

    def _set_busy(self, value):
        self.busy = value
        for widget in [self.load_button, self.start_button, self.folder_button, *self.entries]:
            widget.configure(state="disabled" if value else "normal")
        for widget in (self.broker_box, self.account_box):
            widget.configure(state="disabled" if value else "readonly")
        self._cash_flow_mode_changed()
        if value:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _cash_flow_mode_changed(self):
        enabled = not self.busy and self.cash_flow_mode.get() == "manual"
        for widget in (self.flow_date_entry, self.flow_amount_entry, self.add_flow_button, self.remove_flow_button, self.cash_flow_list):
            widget.configure(state="normal" if enabled else "disabled")
        self.flow_kind_box.configure(state="readonly" if enabled else "disabled")
        for button in self.flow_mode_buttons:
            button.configure(state="disabled" if self.busy else "normal")

    def _add_cash_flow(self):
        if self.busy or self.cash_flow_mode.get() != "manual":
            return
        try:
            flow = _manual_cash_flow({
                "date": self.flow_date.get(),
                "kind": next((kind for kind, label in CASH_FLOW_KINDS.items() if label == self.flow_kind.get()), ""),
                "amount": self.flow_amount.get(),
            }, _day(self.first.get()), _day(self.last.get()))
        except ValueError as error:
            self.messagebox.showerror("Проверь операцию", str(error), parent=self.window)
            return
        self.cash_flows.append(flow)
        self.cash_flow_list.insert("end", f"{flow['date']}   {CASH_FLOW_KINDS[flow['kind']]}   {flow['amount']}")
        self.flow_amount.set("")

    def _remove_cash_flow(self):
        if self.busy or self.cash_flow_mode.get() != "manual":
            return
        for index in reversed(self.cash_flow_list.curselection()):
            del self.cash_flows[index]
            self.cash_flow_list.delete(index)

    def _load_accounts(self):
        if not self._broker_name():
            self.messagebox.showerror("Нужен брокер", "Выбери Финам или Т-Банк.", parent=self.window)
            return
        self._broker_changed()
        self._set_busy(True)
        Thread(target=load_accounts_worker, args=(self.root_dir, self._broker_name(), self._emit), daemon=True).start()

    def _choose_folder(self):
        current = Path(self.output.get())
        chosen = self.filedialog.askdirectory(parent=self.window, title="Папка результатов", initialdir=str(current if current.is_dir() else self.root_dir))
        if chosen:
            self.output.set(chosen)

    def _start(self):
        try:
            index = self.account_box.current()
            account_id = self.accounts[index].id if 0 <= index < len(self.accounts) else ""
            argv = build_export_args(
                self._broker_name(), account_id, self.first.get(), self.last.get(),
                self.capital.get(), self.output.get(), self.config_path,
                today=datetime.now(self.timezone).date(),
                cash_flow_mode=self.cash_flow_mode.get(), cash_flows=self.cash_flows,
            )
        except ValueError as error:
            self.messagebox.showerror("Проверь поля", str(error), parent=self.window)
            return
        self._set_busy(True)
        Thread(target=export_worker, args=(self.root_dir, argv, self._emit), daemon=True).start()

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "accounts":
                    name, self.accounts = payload
                    if name == self._broker_name():
                        labels = [f"{account.name} · …{account.id[-6:]}" for account in self.accounts]
                        self.account_box.configure(values=labels)
                        self.account_box.current(0)
                elif kind == "progress":
                    self.status.set(payload)
                    self._append_log(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "error":
                    self.status.set("Не удалось завершить операцию.")
                    self._append_log(payload)
                    self.messagebox.showerror("Ошибка", str(payload), parent=self.window)
                elif kind == "result":
                    self.status.set("Готово. Результаты сохранены в выбранной папке.")
                    self._append_log("Готово.")
                elif kind == "idle":
                    self._set_busy(False)
        except Empty:
            pass
        self.window.after(80, self._poll)

    def _close(self):
        if self.busy:
            self.messagebox.showinfo("Операция выполняется", "Дождись завершения операции, затем закрой окно.", parent=self.window)
        else:
            self.window.destroy()


def run_gui(root_dir: Path | None = None) -> None:
    root_dir = Path(root_dir).resolve() if root_dir is not None else Path(__file__).resolve().parents[2]
    application = _Window(root_dir)
    application.window.mainloop()
