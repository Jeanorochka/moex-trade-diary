from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout


TITLE = "Расширенный брокерский отчет"
INK = "#263149"
MUTED = "#788298"
PURPLE = "#7964D8"
GREEN = "#249D84"
RED = "#CE6178"
STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 13px; color: #263149; }
QMainWindow, QDialog { background: #F4F5FA; }
QWidget#Content { background: #F4F5FA; }
QFrame#Card { background: #FFFFFF; border: 1px solid #E6E8F1; border-radius: 16px; }
QFrame#Rail { background: #252738; border: none; }
QFrame#Rail QLabel { color: #FFFFFF; background: transparent; }
QFrame#Rail QLabel#Muted { color: #A7A5BD; }
QLabel { background: transparent; }
QLabel#Heading { font-size: 26px; font-weight: 700; color: #252B40; }
QLabel#Section { font-size: 16px; font-weight: 600; }
QLabel#Muted { color: #788298; }
QLabel#Eyebrow { color: #7964D8; font-size: 11px; font-weight: 700; }
QLabel#MetricValue { font-size: 27px; font-weight: 700; }
QLabel#Notice { color: #756444; background: #FFF8E9; border-radius: 9px; padding: 10px; }
QLabel#Error { color: #B44D65; background: #FCEEF2; border-radius: 9px; padding: 10px; }
QLineEdit, QComboBox, QDateEdit { background: #FAFBFE; border: 1px solid #DFE3EE; border-radius: 8px; min-height: 24px; padding: 7px 10px; selection-background-color: #7964D8; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #8F7CE2; background: #FFFFFF; }
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled { color: #9CA2B2; background: #F1F3F8; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #E3E5EF; selection-background-color: #EDE9FD; selection-color: #403663; padding: 5px; }
QPushButton { background: #FFFFFF; border: 1px solid #DFE3EE; border-radius: 8px; padding: 10px 15px; font-weight: 600; }
QPushButton:hover { background: #F0ECFD; border-color: #C2B5EE; }
QPushButton:pressed { background: #E4DCF9; }
QPushButton:disabled { color: #A4A9B7; background: #F3F4F8; border-color: #E9EBF2; }
QPushButton#Primary { background: #7964D8; color: white; border: 1px solid #7964D8; }
QPushButton#Primary:hover { background: #6A54C8; }
QPushButton#Primary:disabled { background: #C4BAE8; border-color: #C4BAE8; color: #FFFFFF; }
QPushButton#Nav { text-align: left; background: transparent; color: #CBC8DD; border: none; padding: 13px 14px; border-radius: 9px; }
QPushButton#Nav:hover { background: #353347; color: #FFFFFF; }
QPushButton#Nav:checked { background: #48405F; color: #FFFFFF; }
QProgressBar { border: none; background: #EBE7F5; border-radius: 3px; min-height: 5px; max-height: 5px; }
QProgressBar::chunk { background: #9683E5; border-radius: 3px; }
QTableView, QTableWidget { background: #FFFFFF; alternate-background-color: #F9FAFD; border: 1px solid #E6E8F1; border-radius: 8px; gridline-color: #EEF0F6; selection-background-color: #EDE9FD; selection-color: #403663; }
QTableView::item { padding: 7px; border-bottom: 1px solid #EEF0F6; }
QHeaderView::section { background: #F4F5FA; border: none; border-bottom: 1px solid #E5E7F0; padding: 10px 8px; color: #788298; font-weight: 600; }
QTabWidget::pane { background: #FFFFFF; border: 1px solid #E6E8F1; border-radius: 12px; }
QTabBar::tab { background: transparent; color: #788298; padding: 11px 18px; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #6D58CA; border-bottom: 3px solid #8D77DF; font-weight: 600; }
QPlainTextEdit { background: #F8F9FD; border: 1px solid #E6E8F1; border-radius: 8px; padding: 8px; font-size: 12px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px; }
QScrollBar::handle:vertical { background: #D5D7E4; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #292C40; color: #FFFFFF; border: none; padding: 8px; }
"""


def configure_app(application):
    application.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#F4F5FA"), (QPalette.ColorRole.WindowText, INK),
        (QPalette.ColorRole.Base, "#FFFFFF"), (QPalette.ColorRole.AlternateBase, "#F9FAFD"),
        (QPalette.ColorRole.Text, INK), (QPalette.ColorRole.Button, "#FFFFFF"),
        (QPalette.ColorRole.ButtonText, INK), (QPalette.ColorRole.Highlight, "#EDE9FD"),
        (QPalette.ColorRole.HighlightedText, "#403663"), (QPalette.ColorRole.PlaceholderText, MUTED),
        (QPalette.ColorRole.ToolTipBase, "#292C40"), (QPalette.ColorRole.ToolTipText, "#FFFFFF"),
    ):
        palette.setColor(role, QColor(color))
    application.setPalette(palette)
    application.setFont(QFont("Segoe UI", 10))
    application.setApplicationName(TITLE)
    arrow = (Path(__file__).parent / "icons" / "chevron.svg").as_posix()
    application.setStyleSheet(STYLE + f'\nQComboBox::down-arrow, QDateEdit::down-arrow {{ image: url("{arrow}"); width: 12px; height: 8px; }}')


def label(text, kind=None, wrap=False):
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("Primary")
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(callback)
    return widget


def card(title=None, subtitle=None):
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(13)
    if title:
        layout.addWidget(label(title, "Section"))
    if subtitle:
        layout.addWidget(label(subtitle, "Muted", True))
    return frame, layout


def number(value, percent=False, digits=2):
    if value is None:
        return "—"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        amount = Decimal(str(value)) * (100 if percent else 1)
        return f"{amount:,.{digits}f}".replace(",", " ").replace(".", ",") + ("%" if percent else "")
    return str(value)


def tone(value):
    if value is None:
        return QColor(INK)
    amount = Decimal(str(value))
    return QColor(GREEN if amount > 0 else RED if amount < 0 else INK)
