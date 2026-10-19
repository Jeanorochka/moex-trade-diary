from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QLocale, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)

from trade_exporter import gui
from trade_exporter.services.file_analysis import analysis_worker
from trade_exporter.ui.charts import AssetChart, ResultChart
from trade_exporter.ui.tables import table
from trade_exporter.ui.theme import TITLE, button, card, label, number, tone


def _date_edit(day):
    widget = QDateEdit(QDate(day.year, day.month, day.day))
    widget.setCalendarPopup(True)
    widget.setDisplayFormat("dd.MM.yyyy")
    widget.setLocale(QLocale(QLocale.Language.Russian, QLocale.Country.Russia))
    return widget


def _field(text, widget):
    frame = QWidget()
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(label(text, "Muted"))
    layout.addWidget(widget)
    return frame


def _row(*widgets):
    frame = QWidget()
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for widget in widgets:
        layout.addWidget(widget)
    return frame


class _Task(QThread):
    event = Signal(str, object)

    def __init__(self, function, arguments, parent):
        super().__init__(parent)
        self.function, self.arguments = function, arguments

    def run(self):
        try:
            self.function(*self.arguments, self.event.emit)
        except (Exception, SystemExit) as error:
            self.event.emit("error", gui._redact(error))


class _JobWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.task = None
        self.busy = False
        self.succeeded = False

    def _job(self, function, arguments):
        if self.task is not None:
            return
        self.succeeded = False
        self.message.hide()
        self._set_busy(True)
        self.task = _Task(function, arguments, self)
        self.task.event.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self.task.finished.connect(self._finished)
        self.task.start()

    @Slot()
    def _finished(self):
        task = self.task
        self.task = None
        if task is not None:
            task.deleteLater()
        self._set_busy(False)

    def _set_busy(self, busy):
        self.busy = busy
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(1 if self.succeeded else 0)
        for widget in self.controls:
            widget.setEnabled(not busy)

    def _error(self, message):
        self.status.setText("Не удалось завершить операцию.")
        self.message.setText(str(message))
        self.message.show()

    def closeEvent(self, event):
        if self.busy:
            self.status.setText("Дождитесь завершения операции, затем закройте окно.")
            event.ignore()
        else:
            event.accept()


class ExportWindow(_JobWindow):
    def __init__(self, root_dir):
        super().__init__()
        self.root_dir = Path(root_dir).resolve()
        self.config_path = self.root_dir / "config.toml"
        self.accounts, self.cash_flows = [], []
        self.analysis_window = None
        self.result_path = None
        self.setWindowTitle(TITLE)
        self.resize(1100, 880)
        self.setMinimumSize(960, 720)
        config_error = None
        try:
            config = gui._read_config(self.root_dir)
            self.timezone = ZoneInfo(str(config.get("app", {}).get("timezone", "Europe/Moscow")))
        except Exception as error:
            config, self.timezone = {}, ZoneInfo("Europe/Moscow")
            config_error = gui._redact(error, gui._secrets({}))
        today = datetime.now(self.timezone).date()
        selected = gui.initial_broker(config)
        destination = Path(str(config.get("app", {}).get("output_dir", "exports")))
        if not destination.is_absolute():
            destination = self.root_dir / destination
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        rail = QFrame()
        rail.setObjectName("Rail")
        rail.setFixedWidth(205)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(20, 30, 20, 24)
        navigation.setSpacing(10)
        mark = label("↗", "Heading")
        mark.setStyleSheet("color: #C5B8FF; font-size: 38px;")
        navigation.addWidget(mark)
        navigation.addWidget(label("Расширенный\nброкерский отчет", "Section", True))
        navigation.addSpacing(28)
        export_nav = button("Выгрузка", lambda: None)
        export_nav.setObjectName("Nav")
        export_nav.setCheckable(True)
        export_nav.setChecked(True)
        export_nav.clicked.connect(lambda: export_nav.setChecked(True))
        navigation.addWidget(export_nav)
        self.analysis_button = button("Аналитика файла", self.open_analysis)
        self.analysis_button.setObjectName("Nav")
        navigation.addWidget(self.analysis_button)
        navigation.addStretch()
        navigation.addWidget(label("Финам · Т-Банк", "Muted"))
        navigation.addWidget(label("Excel и JSON", "Muted"))
        shell.addWidget(rail)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("Content")
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(28, 25, 28, 24)
        body.setSpacing(18)
        body.addWidget(label("БРОКЕРСКИЙ ОТЧЁТ", "Eyebrow"))
        body.addWidget(label("Выгрузка и аналитика", "Heading"))
        body.addWidget(label("Выберите счёт и период, мы подготовим отчёт.", "Muted"))
        account_card, form = card("Параметры отчёта")
        self.broker_box = QComboBox()
        self.broker_box.addItem("Выберите брокера", None)
        for name, title in gui.BROKERS.items():
            self.broker_box.addItem(title, name)
        self.broker_box.setCurrentIndex(max(0, self.broker_box.findData(selected)))
        self.broker_box.currentIndexChanged.connect(self._broker_changed)
        self.load_button = button("Загрузить счета", self._load_accounts)
        self.account_box = QComboBox()
        self.account_box.setPlaceholderText("Сначала загрузите счета")
        connection = QGridLayout()
        connection.setHorizontalSpacing(12)
        connection.addWidget(_field("Брокер", self.broker_box), 0, 0)
        connection.addWidget(self.load_button, 0, 1, alignment=Qt.AlignmentFlag.AlignBottom)
        connection.addWidget(_field("Счёт", self.account_box), 1, 0, 1, 2)
        connection.setColumnStretch(0, 1)
        form.addLayout(connection)
        self.first = _date_edit(today.replace(month=1, day=1))
        self.last = _date_edit(today)
        self.capital = QLineEdit()
        self.capital.setPlaceholderText("Например, 130 000")
        dates = QGridLayout()
        dates.setHorizontalSpacing(12)
        dates.addWidget(_field("Дата начала", self.first), 0, 0)
        dates.addWidget(_field("Дата окончания", self.last), 0, 1)
        dates.addWidget(_field("Капитал на дату начала", self.capital), 0, 2)
        for column in range(3):
            dates.setColumnStretch(column, 1)
        form.addLayout(dates)
        self.output = QLineEdit(str(destination))
        self.folder_button = button("Обзор…", self._choose_folder)
        form.addWidget(_field("Папка результатов", _row(self.output, self.folder_button)))
        body.addWidget(account_card)
        flow_card, flows = card("Пополнения и выводы")
        self.cash_flow_mode = QComboBox()
        self.cash_flow_mode.addItem("Из данных брокера", "auto")
        self.cash_flow_mode.addItem("Ввести вручную", "manual")
        self.cash_flow_mode.currentIndexChanged.connect(self._cash_flow_mode_changed)
        flows.addWidget(self.cash_flow_mode)
        self.manual_panel = QWidget()
        manual = QVBoxLayout(self.manual_panel)
        manual.setContentsMargins(0, 0, 0, 0)
        manual.setSpacing(10)
        self.flow_date = _date_edit(today)
        self.flow_kind_box = QComboBox()
        for kind, text in gui.CASH_FLOW_KINDS.items():
            self.flow_kind_box.addItem(text, kind)
        self.flow_amount = QLineEdit()
        self.flow_amount.setPlaceholderText("Сумма")
        self.add_flow_button = button("Добавить", self._add_cash_flow)
        manual.addWidget(_row(self.flow_date, self.flow_kind_box, self.flow_amount, self.add_flow_button))
        self.flow_table, self.flow_model = table(("Дата", "Операция", "Сумма"))
        self.flow_table.setMaximumHeight(150)
        self.flow_table.setMinimumHeight(105)
        manual.addWidget(self.flow_table)
        self.remove_flow_button = button("Удалить выбранное", self._remove_cash_flow)
        manual.addWidget(self.remove_flow_button, alignment=Qt.AlignmentFlag.AlignLeft)
        manual.addWidget(label("Укажите все внешние движения за период. Ручной список заменяет данные брокера, пустой список означает отсутствие операций.", "Muted", True))
        flows.addWidget(self.manual_panel)
        body.addWidget(flow_card)
        footer, footer_layout = card()
        self.start_button = button("Выгрузить и рассчитать", self._start, True)
        footer_layout.addWidget(self.start_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        footer_layout.addWidget(self.progress)
        self.status = label("Выберите брокера и загрузите счета.", "Muted", True)
        footer_layout.addWidget(self.status)
        self.message = label("", "Error", True)
        self.message.hide()
        footer_layout.addWidget(self.message)
        self.result_buttons = _row(button("Открыть Excel", self._open_result), button("Посмотреть аналитику", self._analyze_result))
        self.result_buttons.hide()
        footer_layout.addWidget(self.result_buttons)
        self.details_button = button("Подробности", self._toggle_log)
        footer_layout.addWidget(self.details_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setFixedHeight(110)
        self.log.hide()
        footer_layout.addWidget(self.log)
        body.addWidget(footer)
        body.addStretch()
        shell.addWidget(scroll, 1)
        self.controls = [self.broker_box, self.account_box, self.load_button, self.first, self.last, self.capital, self.output, self.folder_button, self.cash_flow_mode, self.start_button]
        self._cash_flow_mode_changed()
        if config_error:
            self._error(config_error)

    def _broker_changed(self):
        self.accounts = []
        self.account_box.clear()

    def _cash_flow_mode_changed(self):
        manual = self.cash_flow_mode.currentData() == "manual"
        self.manual_panel.setVisible(manual)
        self.manual_panel.setEnabled(manual and not self.busy)

    def _set_busy(self, busy):
        super()._set_busy(busy)
        self._cash_flow_mode_changed()

    def _add_cash_flow(self):
        if self.busy or self.cash_flow_mode.currentData() != "manual":
            return
        try:
            flow = gui._manual_cash_flow({"date": self.flow_date.date().toString("dd.MM.yyyy"), "kind": self.flow_kind_box.currentData(), "amount": self.flow_amount.text()}, self.first.date().toPython(), self.last.date().toPython())
        except ValueError as error:
            self._error(error)
            return
        self.cash_flows.append(flow)
        self._refresh_flows()
        self.flow_amount.clear()
        self.message.hide()

    def _refresh_flows(self):
        self.flow_model.replace((gui._day(row["date"]), gui.CASH_FLOW_KINDS[row["kind"]], Decimal(row["amount"])) for row in self.cash_flows)

    def _remove_cash_flow(self):
        if self.busy or self.cash_flow_mode.currentData() != "manual":
            return
        selected = [self.flow_model.rows[index.row()] for index in self.flow_table.selectionModel().selectedRows()]
        for day, kind, amount in selected:
            match = next((index for index, row in enumerate(self.cash_flows) if gui._day(row["date"]) == day and gui.CASH_FLOW_KINDS[row["kind"]] == kind and Decimal(row["amount"]) == amount), None)
            if match is not None:
                del self.cash_flows[match]
        self._refresh_flows()

    def _load_accounts(self):
        name = self.broker_box.currentData()
        if not name:
            self._error("Выберите Финам или Т-Банк.")
            return
        self._broker_changed()
        self._job(gui.load_accounts_worker, (self.root_dir, name))

    def _choose_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, "Папка результатов", self.output.text())
        if chosen:
            self.output.setText(chosen)

    def _start(self):
        index = self.account_box.currentIndex()
        account_id = self.accounts[index].id if 0 <= index < len(self.accounts) else ""
        try:
            argv = gui.build_export_args(self.broker_box.currentData(), account_id, self.first.date().toString("dd.MM.yyyy"), self.last.date().toString("dd.MM.yyyy"), self.capital.text(), self.output.text(), self.config_path, today=datetime.now(self.timezone).date(), cash_flow_mode=self.cash_flow_mode.currentData(), cash_flows=self.cash_flows)
        except ValueError as error:
            self._error(error)
            return
        self.result_buttons.hide()
        self._job(gui.export_worker, (self.root_dir, argv))

    @Slot(str, object)
    def _receive(self, kind, value):
        if kind == "accounts":
            name, accounts = value
            if name == self.broker_box.currentData():
                self.accounts = accounts
                for account in accounts:
                    self.account_box.addItem(f"{account.name} · …{account.id[-6:]}")
                if accounts:
                    self.account_box.setCurrentIndex(0)
                self.succeeded = True
        elif kind in {"progress", "log"}:
            self.log.appendPlainText(str(value))
            if kind == "progress":
                self.status.setText(str(value))
        elif kind == "error":
            self.log.appendPlainText(str(value))
            self._error(value)
        elif kind == "result":
            self.result_path = Path(value[0] if isinstance(value, tuple) else value)
            self.succeeded = True
            self.status.setText("Готово. Отчёт сохранён в выбранной папке.")
            self.result_buttons.show()

    def _toggle_log(self):
        self.log.setVisible(self.log.isHidden())

    def _open_result(self):
        if self.result_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result_path)))

    def _analyze_result(self):
        self.open_analysis(self.result_path)

    def open_analysis(self, path=None):
        if self.analysis_window is None:
            self.analysis_window = AnalysisWindow(self.root_dir)
        self.analysis_window.show()
        self.analysis_window.raise_()
        self.analysis_window.activateWindow()
        if isinstance(path, (str, Path)) and not self.analysis_window.busy:
            self.analysis_window.file_path.setText(str(path))
            self.analysis_window.analyze()

    def closeEvent(self, event):
        if self.analysis_window is not None and self.analysis_window.busy:
            self.status.setText("Дождитесь завершения анализа файла.")
            event.ignore()
            return
        if not self.busy and self.analysis_window is not None:
            self.analysis_window.close()
        super().closeEvent(event)


class _Metric(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        self.title = label("", "Muted")
        self.value = label("—", "MetricValue")
        self.unit = label("", "Muted")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.unit)

    def display(self, title, value, mode, currency, colored=True):
        self.title.setText(title)
        self.value.setText(number(Decimal(str(value)) if value is not None else None, percent=mode == "percent"))
        self.value.setStyleSheet(f"color: {tone(value).name() if colored else '#263149'};")
        self.unit.setText(currency if mode == "money" else "Доходность")


class AnalysisWindow(_JobWindow):
    def __init__(self, root_dir=None):
        super().__init__()
        self.root_dir = Path(root_dir or Path.cwd())
        self.analysis = None
        self.setWindowTitle("Аналитика файла · " + TITLE)
        self.resize(1160, 900)
        self.setMinimumSize(940, 740)
        central = QWidget()
        self.setCentralWidget(central)
        body = QVBoxLayout(central)
        body.setContentsMargins(28, 24, 28, 24)
        body.setSpacing(16)
        body.addWidget(label("АНАЛИТИКА ФАЙЛА", "Eyebrow"))
        body.addWidget(label("Портфель в цифрах", "Heading"))
        source, source_layout = card()
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("Путь к Excel, CSV или TSV")
        self.file_path.returnPressed.connect(self.analyze)
        self.browse_button = button("Выбрать файл…", self._browse)
        source_layout.addWidget(_row(self.file_path, self.browse_button))
        self.capital = QLineEdit()
        self.capital.setPlaceholderText("Из отчёта, если указан")
        self.capital.setMaximumWidth(260)
        self.start_button = button("Рассчитать аналитику", self.analyze, True)
        source_layout.addWidget(_row(_field("Начальный капитал, необязательно", self.capital), self.start_button))
        body.addWidget(source)
        self.status = label("Выберите файл. Подключение к брокеру не требуется.", "Muted", True)
        body.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        body.addWidget(self.progress)
        self.message = label("", "Error", True)
        self.message.hide()
        body.addWidget(self.message)
        self.empty = label("◎\n\nЗдесь появятся показатели, графики и сделки\nВыберите файл отчёта выше", "Muted", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("font-size: 17px; color: #8C92A9; line-height: 1.8;")
        body.addWidget(self.empty, 1)
        self.dashboard = QWidget()
        dashboard = QVBoxLayout(self.dashboard)
        dashboard.setContentsMargins(0, 0, 0, 0)
        dashboard.setSpacing(14)
        self.cards = [_Metric() for _ in range(4)]
        dashboard.addWidget(_row(*self.cards))
        self.quality = label("", "Notice", True)
        dashboard.addWidget(self.quality)
        self.tabs = QTabWidget()
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.asset_chart = AssetChart()
        asset_scroll = QScrollArea()
        asset_scroll.setWidgetResizable(True)
        asset_scroll.setWidget(self.asset_chart)
        self.tabs.addTab(asset_scroll, "По активам")
        curve = QWidget()
        curve_layout = QVBoxLayout(curve)
        curve_layout.setContentsMargins(18, 16, 18, 12)
        curve_layout.addWidget(label("Накопленный результат сделок, без переоценки открытых позиций", "Muted", True))
        self.result_chart = ResultChart()
        curve_layout.addWidget(self.result_chart, 1)
        self.tabs.addTab(curve, "Динамика сделок")
        trade_page = QWidget()
        trade_layout = QVBoxLayout(trade_page)
        trade_layout.setContentsMargins(16, 16, 16, 16)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти тикер или сделку…")
        trade_layout.addWidget(self.search)
        self.trade_table, self.trade_model = table(("Дата", "Тикер", "Статус", "Вход", "Выход / Сейчас", "Финрез"), profit_columns=(5,), price_columns=(3, 4))
        self.search.textChanged.connect(self.trade_model.filter)
        trade_layout.addWidget(self.trade_table)
        self.tabs.addTab(trade_page, "Сделки и позиции")
        leaders = QWidget()
        leaders_layout = QHBoxLayout(leaders)
        self.leader_models = []
        for heading in ("Лучшие 5", "Худшие 5"):
            frame, layout = card(heading)
            view, model = table(("Тикер", "Статус", "Финрез"), profit_columns=(2,), stretch_column=0)
            view.setSortingEnabled(False)
            layout.addWidget(view)
            leaders_layout.addWidget(frame)
            self.leader_models.append(model)
        self.tabs.addTab(leaders, "Лидеры")
        self.metric_table, self.metric_model = table(("Показатель", "Значение", "Единица"), stretch_column=0)
        self.tabs.addTab(self.metric_table, "Показатели")
        dashboard.addWidget(self.tabs, 1)
        body.addWidget(self.dashboard, 1)
        self.dashboard.hide()
        self.controls = [self.file_path, self.browse_button, self.capital, self.start_button]

    def _browse(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Выберите отчёт", self.file_path.text() or str(self.root_dir / "exports"), "Отчёты (*.xlsx *.csv *.tsv)")
        if chosen:
            self.file_path.setText(chosen)

    def analyze(self):
        if self.busy:
            return
        self.analysis = None
        self.dashboard.hide()
        self.empty.show()
        self.empty.setText("Выберите файл отчёта для анализа.")
        try:
            raw = self.capital.text().strip()
            capital = gui._positive_decimal(raw, "Укажите положительный начальный капитал или оставьте поле пустым.") if raw else Decimal(0)
            path = Path(self.file_path.text().strip().strip('"')).expanduser()
            if not path.is_absolute():
                path = self.root_dir / path
            if not path.is_file():
                raise ValueError("Выберите существующий файл для анализа.")
        except ValueError as error:
            self._error(error)
            return
        self.empty.setText("Рассчитываем аналитику…")
        self._job(analysis_worker, (path, capital))

    @Slot(str, object)
    def _receive(self, kind, value):
        if kind == "progress":
            self.status.setText(str(value))
        elif kind == "error":
            self.empty.setText("Выберите другой файл или проверьте данные отчёта.")
            self._error(value)
        elif kind == "analysis":
            self.show_analysis(value)

    def show_analysis(self, result):
        self.analysis = result
        self.succeeded = True
        self.status.setText(f"{result.path.name} · {len(result.trades)} строк сделок и позиций · {result.currency}")
        self.file_path.setText(str(result.path))
        for index, (widget, values) in enumerate(zip(self.cards, result.cards)):
            widget.display(*values, result.currency, colored=not (result.portfolio_period and index == 0))
        self.quality.setText(" ".join(result.quality))
        self.quality.setVisible(bool(result.quality))
        self.asset_chart.set_data(result.assets, result.currency)
        self.result_chart.set_data(result.daily)
        self.search.clear()
        self.trade_model.replace(result.trades)
        for model, values in zip(self.leader_models, (result.best, result.worst)):
            model.replace((row.ticker, "Открыта" if row.opened else "Закрыта", row.result) for row in values)
        metrics = []
        for text, value, fmt in result.metrics:
            amount = Decimal(str(value)) if value is not None else None
            if "%" in fmt and amount is not None:
                amount *= 100
            unit = "%" if "%" in fmt else result.currency if "#,##0" in fmt else "Коэффициент"
            metrics.append((text[:1].upper() + text[1:], amount, unit))
        self.metric_model.replace(metrics)
        self.empty.hide()
        self.dashboard.show()
