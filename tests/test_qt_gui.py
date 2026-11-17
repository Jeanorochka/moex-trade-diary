from datetime import date
from decimal import Decimal
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QPushButton
import pytest

from trade_exporter import gui
from trade_exporter.domain.models import Account
from trade_exporter.services.file_analysis import analyze_local_file
from trade_exporter.ui.tables import TableModel
from trade_exporter.ui.theme import configure_app
from trade_exporter.ui.windows import AnalysisWindow, ExportWindow
from test_file_analysis import source_report


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    configure_app(application)
    return application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_read_config", lambda root: {"finam": {"token": "test-token"}})
    widget = ExportWindow(tmp_path, token_path=tmp_path / "credentials" / "tokens.dat")
    yield widget
    assert not widget.busy
    widget.close()
    app.processEvents()


def finish(window, app):
    deadline = monotonic() + 5
    while window.task is not None and monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert window.task is None and not window.busy


def visit_token_dialog(window, open_dialog, button_text, edits=None):
    open_dialog()
    dialog = window.token_dialog
    assert dialog is not None and dialog.isVisible()
    fields = dialog.findChildren(QLineEdit)
    received = {
        "title": dialog.windowTitle(),
        "fields": [(field.text(), field.echoMode()) for field in fields],
    }
    for index, value in (edits or {}).items():
        fields[index].setText(value)
    next(widget for widget in dialog.findChildren(QPushButton) if widget.text() == button_text).click()
    assert window.token_dialog is None
    return received


def test_first_launch_and_gear_save_replace_and_remove_tokens(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_read_config", lambda root: {})
    monkeypatch.setattr(gui, "configured_tokens", lambda config: {})
    path = tmp_path / "credentials" / "tokens.dat"
    window = ExportWindow(tmp_path, token_path=path)
    window.show()
    try:
        initial = visit_token_dialog(window, window.prompt_for_tokens, "Сохранить", {1: " tbank-old "})
        assert initial == {"title": "Токены брокеров", "fields": [("", QLineEdit.EchoMode.Password)] * 3}
        assert window.broker_box.currentData() == "tbank"
        assert not (tmp_path / ".env").exists()
        assert not window.settings_button.icon().isNull()
        assert not window.settings_button.icon().pixmap(17, 17).isNull()
        assert window.settings_button.x() > window.width() / 2
        restored = ExportWindow(tmp_path, token_path=path)
        restored.show()
        try:
            assert restored.tokens == {"tbank": "tbank-old"}
            restored.accounts = [Account("account-1", "Счёт", "tbank")]
            visit_token_dialog(restored, restored.settings_button.click, "Отмена", {1: "tbank-discarded"})
            assert restored.accounts and restored.tokens == {"tbank": "tbank-old"}
            visit_token_dialog(restored, restored.settings_button.click, "Сохранить", {1: "tbank-new"})
            assert restored.tokens == {"tbank": "tbank-new"} and not restored.accounts
            visit_token_dialog(restored, restored.settings_button.click, "Сохранить", {1: ""})
            assert restored.tokens == {}
        finally:
            restored.close()
        reopened = ExportWindow(tmp_path, token_path=path)
        try:
            assert reopened.tokens == {} and reopened.tokens_saved
        finally:
            reopened.close()
    finally:
        window.close()


def test_first_launch_can_be_skipped_and_file_analysis_stays_available(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_read_config", lambda root: {})
    monkeypatch.setattr(gui, "configured_tokens", lambda config: {})
    path = tmp_path / "credentials" / "tokens.dat"
    window = ExportWindow(tmp_path, token_path=path)
    window.show()
    try:
        visit_token_dialog(window, window.prompt_for_tokens, "Пропустить")
        window.open_analysis()
        assert window.analysis_window.isVisible()
    finally:
        window.close()
    reopened = ExportWindow(tmp_path, token_path=path)
    reopened.show()
    try:
        monkeypatch.setattr(reopened, "_token_settings", lambda **kwargs: pytest.fail("Prompt repeated after skip"))
        reopened.prompt_for_tokens()
    finally:
        reopened.close()


def test_manual_flows_preserve_draft_through_mode_switches_and_busy_state(window):
    window.first.setDate(QDate(2026, 1, 1))
    window.last.setDate(QDate(2026, 12, 31))
    window.cash_flow_mode.setCurrentIndex(window.cash_flow_mode.findData("manual"))
    window.flow_date.setDate(QDate(2026, 6, 1))
    window.flow_amount.setText("1 000,25")
    window._add_cash_flow()
    expected = [{"date": "01.06.2026", "kind": "deposit", "amount": "1000.25"}]
    assert window.cash_flows == expected
    assert window.flow_model.rowCount() == 1
    window.cash_flow_mode.setCurrentIndex(0)
    window._remove_cash_flow()
    assert window.cash_flows == expected
    window.cash_flow_mode.setCurrentIndex(1)
    window._set_busy(True)
    assert not window.flow_kind_box.isEnabled()
    window._set_busy(False)
    window.flow_table.selectRow(0)
    window._remove_cash_flow()
    assert not window.cash_flows


def test_account_and_export_jobs_run_without_freezing_or_console_prompts(window, app, monkeypatch):
    accounts = [Account("secret-account-123", "Счёт", "finam")]
    calls = []

    def accounts_worker(root, name, emit):
        emit("accounts", (name, accounts))
        emit("progress", "Счета загружены")
        emit("idle", None)

    def export_worker(root, arguments, emit):
        calls.append(arguments)
        emit("progress", "Загружаем сделки")
        emit("result", root / "готовый отчёт.xlsx")
        emit("idle", None)

    monkeypatch.setattr(gui, "load_accounts_worker", accounts_worker)
    monkeypatch.setattr(gui, "export_worker", export_worker)
    window.broker_box.setCurrentIndex(window.broker_box.findData("finam"))
    window._load_accounts()
    assert window.busy and not window.start_button.isEnabled()
    finish(window, app)
    assert window.accounts == accounts
    assert "secret-account" not in window.account_box.currentText()
    window.first.setDate(QDate(2026, 1, 1))
    window.last.setDate(QDate(2026, 1, 2))
    window.capital.setText("100 000")
    window._start()
    finish(window, app)
    assert calls and "--account-id" in calls[0]
    assert "secret-account-123" in calls[0]
    assert window.result_path.name == "готовый отчёт.xlsx"
    assert window.succeeded and window.start_button.isEnabled()


def test_worker_errors_restore_controls_and_show_message(window, app, monkeypatch):
    def fail(root, name, emit):
        emit("error", "Нет доступа к счёту")
        emit("idle", None)

    monkeypatch.setattr(gui, "load_accounts_worker", fail)
    window.broker_box.setCurrentIndex(1)
    window._load_accounts()
    finish(window, app)
    assert window.message.text() == "Нет доступа к счёту"
    assert window.load_button.isEnabled()
    assert not window.succeeded


def test_analysis_window_reads_selected_file_in_background_without_config(app, source_report, monkeypatch):
    monkeypatch.setattr(gui, "_read_config", lambda root: pytest.fail("Analysis must not read broker config"))
    widget = AnalysisWindow(source_report.parent)
    original = {path: path.read_bytes() for path in source_report.parent.iterdir()}
    try:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source_report), ""))
        widget._browse()
        assert widget.file_path.text() == str(source_report)
        widget.analyze()
        assert widget.busy and not widget.file_path.isEnabled()
        finish(widget, app)
        assert widget.analysis is not None
        assert widget.cards[1].value.text() == "10,00"
        assert widget.trade_model.rowCount() == 4
        assert widget.file_path.isEnabled()
        assert all(path.read_bytes() == content for path, content in original.items())
    finally:
        widget.close()


def test_invalid_new_file_clears_previous_results(app, source_report):
    widget = AnalysisWindow(source_report.parent)
    try:
        widget.show_analysis(analyze_local_file(source_report))
        widget.file_path.setText(str(source_report.parent / "missing.csv"))
        widget.analyze()
        assert widget.analysis is None
        assert widget.dashboard.isHidden()
        assert "существующий файл" in widget.message.text()
    finally:
        widget.close()


def test_second_window_is_reused_and_export_window_remains_available(window, monkeypatch):
    monkeypatch.setattr(AnalysisWindow, "show", lambda self: None)
    window.open_analysis()
    first = window.analysis_window
    window.open_analysis()
    assert first is window.analysis_window
    assert first is not window
    assert not window.busy


def test_typed_sorting_and_filtering_do_not_lose_duplicate_trades(app):
    model = TableModel(("Тикер", "Финрез"), [("AAA", Decimal(2)), ("AAA", Decimal(100)), ("BBB", Decimal(20)), ("CCC", None), ("AAA", Decimal(2))])
    model.sort(1, Qt.SortOrder.DescendingOrder)
    assert [row[1] for row in model.rows] == [100, 20, 2, 2, None]
    model.filter("aaa")
    assert len(model.rows) == 3
    assert [row[1] for row in model.rows] == [100, 2, 2]
    model.filter("")
    assert len(model.rows) == 5


def test_manual_flow_dates_sort_chronologically(window):
    window.cash_flows = [
        {"date": "01.03.2026", "kind": "deposit", "amount": "10"},
        {"date": "20.02.2026", "kind": "withdrawal", "amount": "5"},
    ]
    window._refresh_flows()
    assert window.flow_model.rows[0][0] == date(2026, 2, 20)


def test_export_sidebar_and_bcs_choice(window):
    from PySide6.QtWidgets import QLabel

    labels = {widget.text() for widget in window.findChildren(QLabel)}
    assert "Расширенный\nброкерский отчет" in labels
    assert labels.isdisjoint({"↗", "Финам · Т-Банк", "Excel и JSON"})
    assert window.broker_box.itemText(window.broker_box.findData("bcs")) == "БКС"
