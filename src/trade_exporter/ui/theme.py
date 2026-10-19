from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout


TITLE = "Расширенный брокерский отчет"
BACKGROUND = "#171923"
SURFACE = "#202330"
GRID = "#303647"
INK = "#E5E8F2"
MUTED = "#A2ABC0"
PURPLE = "#AF9CF5"
GREEN = "#66D2B2"
RED = "#F28BA0"
STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 12px; color: #E5E8F2; }
QMainWindow, QDialog { background: #171923; }
QWidget#Content { background: #171923; }
QFrame#Card { background: #202330; border: 1px solid #34394B; border-radius: 13px; }
QFrame#Rail { background: #13151E; border: none; }
QFrame#Rail QLabel { color: #E5E8F2; background: transparent; }
QFrame#Rail QLabel#Muted { color: #A2ABC0; }
QLabel { background: transparent; }
QLabel#Heading { font-size: 23px; font-weight: 700; color: #F0F1F8; }
QLabel#Section { font-size: 14px; font-weight: 600; }
QLabel#Muted { color: #A2ABC0; }
QLabel#Eyebrow { color: #AF9CF5; font-size: 10px; font-weight: 700; }
QLabel#MetricValue { font-size: 23px; font-weight: 700; }
QLabel#Notice { color: #E4C58C; background: #332E26; border-radius: 8px; padding: 8px; }
QLabel#Error { color: #F2A7B7; background: #392733; border-radius: 8px; padding: 8px; }
QLineEdit, QComboBox, QDateEdit { background: #191D29; border: 1px solid #41495E; border-radius: 7px; min-height: 22px; padding: 6px 8px; selection-background-color: #584A7C; selection-color: #FFFFFF; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #AF9CF5; background: #242838; }
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled { color: #788297; background: #1C202B; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: #202330; border: 1px solid #41495E; selection-background-color: #483D68; selection-color: #FFFFFF; padding: 4px; }
QPushButton { background: #2A2F40; border: 1px solid #41495E; border-radius: 7px; padding: 8px 12px; font-weight: 600; }
QPushButton:hover { background: #39324E; border-color: #8F7DCC; }
QPushButton:pressed { background: #483D68; }
QPushButton:disabled { color: #788297; background: #242835; border-color: #34394B; }
QPushButton#Primary { background: #8B73DD; color: #FFFFFF; border: 1px solid #8B73DD; }
QPushButton#Primary:hover { background: #9B84EB; }
QPushButton#Primary:disabled { background: #483F66; border-color: #483F66; color: #A6A0B8; }
QPushButton#Nav { text-align: left; background: transparent; color: #B7B3CF; border: none; padding: 11px 12px; border-radius: 8px; }
QPushButton#Nav:hover { background: #292635; color: #FFFFFF; }
QPushButton#Nav:checked { background: #39304E; color: #EDE7FF; }
QProgressBar { border: none; background: #343047; border-radius: 3px; min-height: 5px; max-height: 5px; }
QProgressBar::chunk { background: #AF9CF5; border-radius: 3px; }
QTableView, QTableWidget { background: #202330; alternate-background-color: #252A38; border: 1px solid #34394B; border-radius: 7px; gridline-color: #303647; selection-background-color: #483D68; selection-color: #FFFFFF; }
QTableView::item { padding: 5px; border-bottom: 1px solid #303647; }
QHeaderView::section, QTableCornerButton::section { background: #292E3E; border: none; border-bottom: 1px solid #34394B; padding: 8px 6px; color: #A2ABC0; font-weight: 600; }
QTabWidget::pane { background: #202330; border: 1px solid #34394B; border-radius: 10px; }
QTabBar::tab { background: transparent; color: #A2ABC0; padding: 9px 14px; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #C2B0FF; border-bottom: 3px solid #AF9CF5; font-weight: 600; }
QPlainTextEdit { background: #191D29; border: 1px solid #34394B; border-radius: 7px; padding: 7px; font-size: 11px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px; }
QScrollBar::handle:vertical { background: #505871; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 3px; }
QScrollBar::handle:horizontal { background: #505871; border-radius: 3px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QToolTip { background: #30364A; color: #F0F1F8; border: 1px solid #505871; padding: 7px; }
"""


def configure_app(application):
    application.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    application.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, BACKGROUND), (QPalette.ColorRole.WindowText, INK),
        (QPalette.ColorRole.Base, SURFACE), (QPalette.ColorRole.AlternateBase, "#252A38"),
        (QPalette.ColorRole.Text, INK), (QPalette.ColorRole.Button, "#2A2F40"),
        (QPalette.ColorRole.ButtonText, INK), (QPalette.ColorRole.Highlight, "#483D68"),
        (QPalette.ColorRole.HighlightedText, "#FFFFFF"), (QPalette.ColorRole.PlaceholderText, MUTED),
        (QPalette.ColorRole.ToolTipBase, "#30364A"), (QPalette.ColorRole.ToolTipText, INK),
        (QPalette.ColorRole.Light, "#505871"), (QPalette.ColorRole.Midlight, "#41495E"),
        (QPalette.ColorRole.Mid, GRID), (QPalette.ColorRole.Dark, "#13151E"),
        (QPalette.ColorRole.Shadow, "#0E1017"), (QPalette.ColorRole.Link, PURPLE),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#788297"))
    application.setPalette(palette)
    application.setFont(QFont("Segoe UI", 9))
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
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
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
