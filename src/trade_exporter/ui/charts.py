from bisect import bisect_left
from math import ceil, floor, log10

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from trade_exporter.ui.theme import GREEN, GRID, INK, MUTED, PURPLE, RED, SURFACE, number


def bounds(values):
    low, high = min([0, *values]), max([0, *values])
    target = (high - low) / 4 or 1
    scale = 10 ** floor(log10(target))
    step = next(multiplier * scale for multiplier in (1, 2, 5, 10) if multiplier * scale >= target)
    return floor(low / step) * step, ceil(high / step) * step if high else step, step


class AssetChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.hits = []
        self.currency = "RUB"
        self.setMouseTracking(True)
        self.setMinimumHeight(260)

    def set_data(self, rows, currency):
        self.rows = sorted(rows, key=lambda row: (row[3] is not None, row[3] or 0), reverse=True)
        self.currency = currency
        self.setMinimumHeight(max(260, len(self.rows) * 32 + 70))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        painter.setFont(QFont("Segoe UI", 8))
        self.hits = []
        if not self.rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Нет результатов по активам")
            painter.end()
            return
        low, high, step = bounds([float(row[3]) for row in self.rows if row[3] is not None])
        left, right, top = 155, self.width() - 120, 22
        width = max(80, right - left)
        zero = left + (0 - low) / (high - low) * width
        painter.setFont(QFont("Segoe UI", 8))
        for index in range(round((high - low) / step) + 1):
            value = low + index * step
            x = left + (value - low) / (high - low) * width
            painter.setPen(QPen(QColor(GRID), 1))
            painter.drawLine(QPointF(x, top), QPointF(x, self.height() - 40))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(x - 50, self.height() - 31, 100, 24), Qt.AlignmentFlag.AlignCenter, number(value, digits=0 if step >= 1 else 2))
        painter.setPen(QPen(QColor("#58627A"), 1))
        painter.drawLine(QPointF(zero, top), QPointF(zero, self.height() - 40))
        painter.setFont(QFont("Segoe UI", 8))
        for index, (ticker, realized, opened, total) in enumerate(self.rows):
            y = top + index * 32
            painter.setPen(QColor(INK))
            text = painter.fontMetrics().elidedText(str(ticker), Qt.TextElideMode.ElideMiddle, left - 28)
            painter.drawText(QRectF(10, y, left - 28, 28), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
            if total is not None:
                x = left + (float(total) - low) / (high - low) * width
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(GREEN if total >= 0 else RED))
                painter.drawRoundedRect(QRectF(min(x, zero), y + 5, max(abs(x - zero), 2), 18), 4, 4)
                painter.setPen(QColor(GREEN if total >= 0 else RED))
                painter.drawText(QRectF(right + 12, y, 108, 28), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, number(total))
            else:
                painter.setPen(QColor(MUTED))
                painter.drawText(QRectF(right + 10, y, 110, 28), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "Нет оценки")
            self.hits.append((QRectF(0, y, self.width(), 32), f"{ticker}\nЗакрыто: {number(realized)} {self.currency}\nОткрыто: {number(opened)} {self.currency}\nИтого: {number(total)} {self.currency}"))
        painter.end()

    def mouseMoveEvent(self, event):
        for rect, text in self.hits:
            if rect.contains(event.position()):
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QToolTip.hideText()


class ResultChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.points = []
        self.setMouseTracking(True)
        self.setMinimumHeight(250)

    def set_data(self, rows):
        self.rows = list(rows)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        painter.setFont(QFont("Segoe UI", 8))
        self.points = []
        if not self.rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "В файле нет истории сделок")
            painter.end()
            return
        low, high, step = bounds([float(value) for _, value in self.rows])
        left, top, right, bottom = 92, 24, self.width() - 22, self.height() - 42
        first, last = self.rows[0][0].toordinal(), self.rows[-1][0].toordinal()
        for index in range(round((high - low) / step) + 1):
            value = low + index * step
            y = bottom - (value - low) / (high - low) * (bottom - top)
            painter.setPen(QPen(QColor(GRID), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, y - 12, left - 12, 24), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, number(value, digits=0 if step >= 1 else 2))
        path = QPainterPath()
        for index, (day, value) in enumerate(self.rows):
            x = left + (day.toordinal() - first) / max(last - first, 1) * (right - left)
            y = bottom - (float(value) - low) / (high - low) * (bottom - top)
            point = QPointF(x, y)
            self.points.append(point)
            if index:
                path.lineTo(point)
            else:
                path.moveTo(point)
        area = QPainterPath(path)
        area.lineTo(self.points[-1].x(), bottom)
        area.lineTo(self.points[0].x(), bottom)
        area.closeSubpath()
        gradient = QLinearGradient(0, top, 0, bottom)
        gradient.setColorAt(0, QColor("#493D69"))
        gradient.setColorAt(1, QColor(SURFACE))
        painter.fillPath(area, gradient)
        painter.setPen(QPen(QColor(PURPLE), 2.5))
        painter.drawPath(path)
        painter.setBrush(QColor(PURPLE))
        painter.drawEllipse(self.points[-1], 4, 4)
        painter.setPen(QColor(MUTED))
        for index in sorted({0, len(self.rows) // 2, len(self.rows) - 1}):
            point = self.points[index]
            x = min(max(point.x() - 45, left - 25), right - 70)
            painter.drawText(QRectF(x, bottom + 12, 90, 24), Qt.AlignmentFlag.AlignCenter, self.rows[index][0].strftime("%d.%m.%y"))
        painter.end()

    def mouseMoveEvent(self, event):
        if not self.points:
            return
        x_values = [point.x() for point in self.points]
        index = min(bisect_left(x_values, event.position().x()), len(self.points) - 1)
        if index and abs(x_values[index - 1] - event.position().x()) < abs(x_values[index] - event.position().x()):
            index -= 1
        day, value = self.rows[index]
        QToolTip.showText(event.globalPosition().toPoint(), f"{day:%d.%m.%Y}\nРезультат сделок: {number(value)}", self)
