from decimal import Decimal

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from trade_exporter.ui.theme import number, tone


class TableModel(QAbstractTableModel):
    def __init__(self, headers, rows=(), *, profit_columns=(), price_columns=(), parent=None):
        super().__init__(parent)
        self.headers = tuple(headers)
        self.all_rows = list(rows)
        self.rows = list(self.all_rows)
        self.profit_columns = set(profit_columns)
        self.price_columns = set(price_columns)
        self.query = ""
        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() in self.price_columns and isinstance(value, (Decimal, float)):
                return number(value, digits=8).rstrip("0").rstrip(",")
            return number(value, digits=0 if isinstance(value, int) else 2)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(value) if value is not None else "Значение недоступно"
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if isinstance(value, (Decimal, float, int)) else Qt.AlignmentFlag.AlignLeft))
        if role == Qt.ItemDataRole.ForegroundRole and index.column() in self.profit_columns and isinstance(value, (Decimal, float, int)):
            return tone(value)
        return None

    def replace(self, rows):
        self.all_rows = list(rows)
        self.refresh()

    def filter(self, text):
        self.query = str(text).strip().casefold()
        self.refresh()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self.refresh()

    def refresh(self):
        self.beginResetModel()
        matching = [row for row in self.all_rows if not self.query or any(self.query in str(value).casefold() for value in row)]
        column = self.sort_column
        known = [row for row in matching if row[column] is not None]
        missing = [row for row in matching if row[column] is None]
        known.sort(key=lambda row: row[column], reverse=self.sort_order == Qt.SortOrder.DescendingOrder)
        self.rows = known + missing
        self.endResetModel()


def table(headers, rows=(), *, profit_columns=(), price_columns=(), stretch_column=1):
    view = QTableView()
    model = TableModel(headers, rows, profit_columns=profit_columns, price_columns=price_columns, parent=view)
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(34)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.horizontalHeader().setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    view.setSortingEnabled(True)
    view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    return view, model
