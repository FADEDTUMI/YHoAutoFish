import html
import math
import random
from collections import Counter, defaultdict

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPointF, QRectF, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from gui.theme import (
    APP_COLORS,
    RARITY_META,
    RARITY_ORDER,
    add_shadow,
    combo_stylesheet,
    line_edit_stylesheet,
    panel_stylesheet,
    rounded_pixmap,
    secondary_button_stylesheet,
    table_stylesheet,
)


class DashboardPanel(QFrame):
    def __init__(self, variant="elevated", parent=None):
        super().__init__(parent)
        self.setProperty("variant", variant)
        self.setStyleSheet(panel_stylesheet())
        add_shadow(self, blur=22, alpha=92, offset=(0, 8))


class StatCard(DashboardPanel):
    def __init__(self, title, accent, parent=None):
        super().__init__("elevated", parent)
        self.accent = accent

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; font-weight: 700;"
        )
        layout.addWidget(title_label)

        self.value_label = QLabel("--")
        self.value_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 29px; font-weight: 900;"
        )
        layout.addWidget(self.value_label)

        self.note_label = QLabel("")
        self.note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        layout.addWidget(self.note_label)

        accent_bar = QFrame()
        accent_bar.setFixedHeight(4)
        accent_bar.setStyleSheet(
            f"background-color: {accent}; border: none; border-radius: 2px;"
        )
        layout.addWidget(accent_bar)

    def set_data(self, value, note=""):
        self.value_label.setText(value)
        self.note_label.setText(note)


class ChartModeButton(QPushButton):
    def __init__(self, text, mode, parent=None):
        super().__init__(text, parent)
        self.mode = mode
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(secondary_button_stylesheet())


class DialogCloseButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setFixedSize(38, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; outline: none; }")

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(3, 3, -3, -3)
        if self.isDown():
            bg = QColor(255, 255, 255, 44)
        elif self.underMouse():
            bg = QColor(255, 102, 126, 110)
        else:
            bg = QColor(255, 255, 255, 14)
        painter.setPen(QPen(QColor(111, 145, 182, 42), 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 12, 12)

        icon_color = QColor(255, 255, 255) if self.underMouse() else QColor(APP_COLORS["text_dim"])
        pen = QPen(icon_color, 1.45)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        cx = self.width() / 2
        cy = self.height() / 2
        painter.drawLine(int(cx - 5), int(cy - 5), int(cx + 5), int(cy + 5))
        painter.drawLine(int(cx + 5), int(cy - 5), int(cx - 5), int(cy + 5))


class InsightChart(QWidget):
    rarityActivated = Signal(str, QPointF)
    trendActivated = Signal(str, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "bar"
        self.distribution = {}
        self.trend_points = []
        self.trend_granularity = "day"
        self.total_count = 0
        self._rarity_regions = {}
        self._trend_regions = {}
        self._hover_rarity = ""
        self._hover_trend = ""
        self._hover_pulse = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(45)
        self._pulse_timer.timeout.connect(self._tick_hover_pulse)
        self.setMouseTracking(True)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_mode(self, mode):
        if self.mode == mode:
            return
        self.mode = mode
        self.update()

    def set_data(self, distribution, trend_points, trend_granularity="day"):
        if (
            self.distribution == (distribution or {})
            and self.trend_points == (trend_points or [])
            and self.trend_granularity == trend_granularity
        ):
            return
        self.distribution = distribution or {}
        self.trend_points = trend_points or []
        self.trend_granularity = trend_granularity
        self.total_count = sum(self.distribution.values())
        self.update()

    def _tick_hover_pulse(self):
        self._hover_pulse = (self._hover_pulse + 0.12) % (math.pi * 2)
        self.update()

    def _set_hover(self, rarity="", trend=""):
        changed = rarity != self._hover_rarity or trend != self._hover_trend
        if not changed:
            return
        self._hover_rarity = rarity
        self._hover_trend = trend
        if rarity or trend:
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
            self.setCursor(Qt.PointingHandCursor)
        else:
            self._pulse_timer.stop()
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        rarity = ""
        trend = ""
        if self.mode in {"bar", "pie"}:
            for key, region in self._rarity_regions.items():
                if region.contains(pos):
                    rarity = key
                    break
        elif self.mode == "line":
            for key, region in self._trend_regions.items():
                if region.contains(pos):
                    trend = key
                    break
        self._set_hover(rarity, trend)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_hover("", "")
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position()
            if self.mode in {"bar", "pie"}:
                for rarity, region in self._rarity_regions.items():
                    if region.contains(pos):
                        self.rarityActivated.emit(rarity, region.center())
                        return
            elif self.mode == "line":
                for key, region in self._trend_regions.items():
                    if region.contains(pos):
                        self.trendActivated.emit(key, region.center())
                        return
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._rarity_regions = {}
        self._trend_regions = {}

        rect = self.rect().adjusted(10, 10, -10, -10)
        shell_path = QPainterPath()
        shell_path.addRoundedRect(rect, 28, 28)

        background = QLinearGradient(rect.topLeft(), rect.bottomRight())
        background.setColorAt(0.0, QColor(20, 33, 51, 215))
        background.setColorAt(0.5, QColor(18, 31, 47, 228))
        background.setColorAt(1.0, QColor(10, 20, 33, 236))
        painter.fillPath(shell_path, background)

        glow = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        glow.setColorAt(0.0, QColor(29, 208, 214, 32))
        glow.setColorAt(1.0, QColor(29, 208, 214, 0))
        painter.fillPath(shell_path, glow)

        painter.setPen(QPen(QColor(115, 146, 182, 36), 1))
        painter.drawPath(shell_path)

        if self.mode == "pie":
            self._draw_pie(painter, rect)
        elif self.mode == "line":
            self._draw_line(painter, rect)
        else:
            self._draw_bar(painter, rect)

    def _draw_empty(self, painter, rect, text):
        painter.setPen(QColor(APP_COLORS["text_soft"]))
        painter.setFont(QFont("Microsoft YaHei UI", 12))
        painter.drawText(rect, Qt.AlignCenter, text)

    def _distribution_items(self):
        total = sum(self.distribution.values())
        return [
            (rarity, self.distribution[rarity], total)
            for rarity in RARITY_ORDER
            if self.distribution.get(rarity, 0)
        ]

    def _draw_bar(self, painter, rect):
        items = self._distribution_items()
        if not items:
            self._draw_empty(painter, rect, "暂无捕获数据")
            return

        header_rect = QRectF(rect.left() + 16, rect.top() + 14, rect.width() - 32, 28)
        painter.setPen(QColor(APP_COLORS["text_dim"]))
        painter.setFont(QFont("Microsoft YaHei UI", 10))
        painter.drawText(header_rect, Qt.AlignLeft | Qt.AlignVCenter, "稀有度分布")
        painter.drawText(header_rect, Qt.AlignRight | Qt.AlignVCenter, f"总计 {self.total_count} 条")

        content_rect = rect.adjusted(18, 52, -18, -18)
        max_count = max(count for _, count, _ in items)
        row_gap = 8
        available_height = max(1, content_rect.height() - row_gap * max(0, len(items) - 1))
        row_height = max(40, min(48, available_height / max(1, len(items))))

        for index, (rarity, count, total) in enumerate(items):
            meta = RARITY_META[rarity]
            top = content_rect.top() + index * (row_height + row_gap)
            row_rect = QRectF(content_rect.left() + 4, top, content_rect.width() - 8, row_height)
            self._rarity_regions[rarity] = row_rect
            hovered = rarity == self._hover_rarity
            pulse = (math.sin(self._hover_pulse) + 1) / 2 if hovered else 0

            row_path = QPainterPath()
            row_path.addRoundedRect(row_rect, 18, 18)
            row_bg = QLinearGradient(row_rect.topLeft(), row_rect.topRight())
            row_bg.setColorAt(0.0, QColor(7, 17, 29, 255))
            row_bg.setColorAt(0.55, QColor(8, 19, 31, 255))
            row_bg.setColorAt(1.0, QColor(4, 10, 18, 255))
            painter.fillPath(row_path, row_bg)
            border_color = QColor(meta["color"])
            border_color.setAlpha(155 if hovered else 42)
            painter.setPen(QPen(border_color, 1.2 + pulse * 1.4))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(row_path)
            if hovered:
                accent = QColor(meta["color"])
                accent.setAlpha(110 + int(pulse * 80))
                painter.setPen(QPen(accent, 3.0))
                painter.drawLine(row_rect.left() + 18, row_rect.bottom() - 7, row_rect.right() - 18, row_rect.bottom() - 7)

            painter.setPen(Qt.NoPen)
            halo = QColor(meta["color"])
            halo.setAlpha(48 if hovered else 34)
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(row_rect.left() + 16, row_rect.center().y() - 10, 20, 20))
            painter.setBrush(QColor(meta["color"]))
            painter.drawEllipse(QRectF(row_rect.left() + 20, row_rect.center().y() - 6, 12, 12))

            label_rect = QRectF(row_rect.left() + 48, row_rect.top() + 7, 72, 16)
            painter.setPen(QColor(APP_COLORS["text"]))
            painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, meta["label"])

            count_rect = QRectF(row_rect.left() + 48, row_rect.top() + 24, 80, 13)
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(count_rect, Qt.AlignLeft | Qt.AlignVCenter, f"{count} 条")

            percent = int(count / total * 100)
            percent_rect = QRectF(row_rect.right() - 58, row_rect.top() + 7, 48, 16)
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            painter.drawText(percent_rect, Qt.AlignRight | Qt.AlignVCenter, f"{percent}%")

            value_rect = QRectF(row_rect.right() - 84, row_rect.top() + 24, 74, 13)
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            painter.drawText(value_rect, Qt.AlignRight | Qt.AlignVCenter, f"{count} / {percent}%")

            track_left = row_rect.left() + 132
            track_right = row_rect.right() - 98
            track_width = max(44, track_right - track_left)
            track_rect = QRectF(track_left, row_rect.center().y() - 4, track_width, 8)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 18 if hovered else 11))
            painter.drawRoundedRect(track_rect, 4, 4)

            fill_width = track_rect.width() * (count / max_count if max_count else 0)
            fill_rect = QRectF(track_rect.left(), track_rect.top(), max(4, fill_width), track_rect.height())
            gradient = QLinearGradient(fill_rect.topLeft(), fill_rect.topRight())
            gradient.setColorAt(0.0, QColor(meta["color"]))
            gradient.setColorAt(1.0, QColor(meta["glow"]))
            painter.setBrush(gradient)
            painter.drawRoundedRect(fill_rect, 4, 4)

    def _draw_pie(self, painter, rect):
        items = self._distribution_items()
        if not items:
            self._draw_empty(painter, rect, "暂无捕获数据")
            return

        total = sum(count for _, count, _ in items)
        legend_width = min(160, max(138, rect.width() * 0.35))
        row_height = 38
        row_gap = 8
        legend_content_height = len(items) * row_height + max(0, len(items) - 1) * row_gap
        legend_height = min(rect.height() - 28, legend_content_height + 22)
        legend_rect = QRectF(
            rect.right() - legend_width - 18,
            rect.center().y() - legend_height / 2,
            legend_width,
            legend_height,
        )
        gap = 22
        available_pie_width = max(96, legend_rect.left() - rect.left() - gap - 24)
        size = min(available_pie_width, rect.height() * 0.70)
        pie_rect = QRectF(rect.left() + 24, rect.center().y() - size / 2, size, size)
        ring_width = max(16, min(21, int(size * 0.12)))

        aura_rect = pie_rect.adjusted(-18, -18, 18, 18)
        aura_gradient = QLinearGradient(aura_rect.topLeft(), aura_rect.bottomRight())
        aura_gradient.setColorAt(0.0, QColor(29, 208, 214, 34))
        aura_gradient.setColorAt(1.0, QColor(10, 18, 29, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(aura_gradient)
        painter.drawEllipse(aura_rect)

        painter.setPen(QPen(QColor(255, 255, 255, 10), ring_width))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(pie_rect)

        start_angle = 90 * 16
        for rarity, count, _ in items:
            color = QColor(RARITY_META[rarity]["color"])
            glow = QColor(RARITY_META[rarity]["glow"])
            span = -int((count / total) * 360 * 16)

            glow_pen = QPen(glow, ring_width + 8)
            glow_pen.setCapStyle(Qt.RoundCap)
            painter.setOpacity(0.20)
            painter.setPen(glow_pen)
            painter.drawArc(pie_rect, start_angle, span)

            painter.setOpacity(1.0)
            pen = QPen(color, ring_width)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(pie_rect, start_angle, span)
            start_angle += span

        inner_margin = max(30, int(size * 0.24))
        inner_rect = pie_rect.adjusted(inner_margin, inner_margin, -inner_margin, -inner_margin)
        painter.setPen(Qt.NoPen)
        inner_gradient = QLinearGradient(inner_rect.topLeft(), inner_rect.bottomRight())
        inner_gradient.setColorAt(0.0, QColor(25, 42, 64, 235))
        inner_gradient.setColorAt(1.0, QColor(7, 15, 26, 245))
        painter.setBrush(inner_gradient)
        painter.drawEllipse(inner_rect)

        center_rect = inner_rect.adjusted(-8, 0, 8, 0)
        label_rect = QRectF(center_rect.left(), center_rect.center().y() - 22, center_rect.width(), 18)
        value_rect = QRectF(center_rect.left(), center_rect.center().y() - 5, center_rect.width(), 34)

        painter.setPen(QColor(APP_COLORS["text_dim"]))
        painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        painter.drawText(label_rect, Qt.AlignCenter, "累计")
        painter.setPen(QColor(APP_COLORS["text"]))
        value_font_size = 20 if total < 1000 else 17
        painter.setFont(QFont("Microsoft YaHei UI", value_font_size, QFont.Bold))
        painter.drawText(value_rect, Qt.AlignCenter, str(total))

        legend_path = QPainterPath()
        legend_path.addRoundedRect(legend_rect, 22, 22)
        legend_bg = QLinearGradient(legend_rect.topLeft(), legend_rect.bottomRight())
        legend_bg.setColorAt(0.0, QColor(8, 19, 31, 238))
        legend_bg.setColorAt(0.55, QColor(7, 17, 28, 242))
        legend_bg.setColorAt(1.0, QColor(4, 10, 18, 248))
        painter.setPen(Qt.NoPen)
        painter.fillPath(legend_path, legend_bg)
        painter.setPen(QPen(QColor(99, 228, 228, 22), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(legend_path)

        legend_x = legend_rect.left() + 10
        start_top = legend_rect.center().y() - legend_content_height / 2
        for index, (rarity, count, _) in enumerate(items):
            percent = int(count / total * 100)
            meta = RARITY_META[rarity]
            top = start_top + index * (row_height + row_gap)
            badge_rect = QRectF(legend_x, top, legend_rect.width() - 20, row_height)
            self._rarity_regions[rarity] = badge_rect
            hovered = rarity == self._hover_rarity
            pulse = (math.sin(self._hover_pulse) + 1) / 2 if hovered else 0
            badge_path = QPainterPath()
            badge_path.addRoundedRect(badge_rect, 15, 15)
            badge_bg = QLinearGradient(badge_rect.topLeft(), badge_rect.topRight())
            badge_bg.setColorAt(0.0, QColor(7, 17, 29, 255))
            badge_bg.setColorAt(0.58, QColor(8, 19, 31, 255))
            badge_bg.setColorAt(1.0, QColor(4, 10, 18, 255))
            painter.fillPath(badge_path, badge_bg)
            border_color = QColor(meta["color"])
            border_color.setAlpha(150 if hovered else 38)
            painter.setPen(QPen(border_color, 1.2 + pulse * 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(badge_path)
            if hovered:
                accent = QColor(meta["color"])
                accent.setAlpha(100 + int(pulse * 76))
                painter.setPen(QPen(accent, 2.4))
                painter.drawLine(badge_rect.left() + 16, badge_rect.bottom() - 6, badge_rect.right() - 16, badge_rect.bottom() - 6)

            painter.setPen(Qt.NoPen)
            color = QColor(meta["color"])
            halo = QColor(meta["color"])
            halo.setAlpha(48 if hovered else 36)
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(badge_rect.left() + 10, badge_rect.center().y() - 10, 20, 20))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(badge_rect.left() + 14, badge_rect.center().y() - 6, 12, 12))

            painter.setPen(QColor(APP_COLORS["text"]))
            painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            painter.drawText(
                QRectF(badge_rect.left() + 38, badge_rect.top() + 5, 54, 15),
                Qt.AlignLeft | Qt.AlignVCenter,
                meta["label"],
            )
            painter.setPen(QColor(APP_COLORS["text"]))
            painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(badge_rect.right() - 40, badge_rect.top() + 5, 32, 15),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{percent}%",
            )
            painter.setPen(QColor(APP_COLORS["text"]))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(
                QRectF(badge_rect.left() + 38, badge_rect.top() + 21, 72, 13),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{count} 条",
            )

            track_width = max(16, badge_rect.width() - 118)
            track_rect = QRectF(badge_rect.right() - track_width - 10, badge_rect.top() + 27, track_width, 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 18))
            painter.drawRoundedRect(track_rect, 2, 2)
            fill_rect = QRectF(track_rect.left(), track_rect.top(), max(2, track_rect.width() * percent / 100), track_rect.height())
            fill_color = QColor(meta["color"])
            fill_color.setAlpha(170)
            painter.setBrush(fill_color)
            painter.drawRoundedRect(fill_rect, 2, 2)

    def _draw_line(self, painter, rect):
        if not self.trend_points:
            self._draw_empty(painter, rect, "暂无趋势数据")
            return

        header_rect = QRectF(rect.left() + 16, rect.top() + 12, rect.width() - 32, 24)
        title = "按小时捕获趋势" if self.trend_granularity == "hour" else "按日期捕获趋势"
        painter.setPen(QColor(APP_COLORS["text_dim"]))
        painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
        painter.drawText(header_rect, Qt.AlignLeft | Qt.AlignVCenter, title)
        painter.drawText(header_rect, Qt.AlignRight | Qt.AlignVCenter, f"峰值 {max(value for _, value in self.trend_points)} 条")

        plot_rect = rect.adjusted(24, 50, -22, -38)
        painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for index in range(5):
            y = plot_rect.top() + index * plot_rect.height() / 4
            painter.drawLine(plot_rect.left(), y, plot_rect.right(), y)

        max_value = max(value for _, value in self.trend_points) or 1
        x_step = plot_rect.width() / max(1, len(self.trend_points) - 1)
        points = []
        for index, (label, value) in enumerate(self.trend_points):
            x = plot_rect.left() + index * x_step
            y = plot_rect.bottom() - (value / max_value) * plot_rect.height()
            points.append((QPointF(x, y), label, value))

        path = QPainterPath(points[0][0])
        for index in range(1, len(points)):
            prev = points[index - 1][0]
            current = points[index][0]
            control_x = (prev.x() + current.x()) / 2
            path.cubicTo(QPointF(control_x, prev.y()), QPointF(control_x, current.y()), current)

        fill_path = QPainterPath(path)
        fill_path.lineTo(plot_rect.right(), plot_rect.bottom())
        fill_path.lineTo(plot_rect.left(), plot_rect.bottom())
        fill_path.closeSubpath()

        fill_gradient = QLinearGradient(plot_rect.topLeft(), plot_rect.bottomLeft())
        fill_gradient.setColorAt(0.0, QColor(29, 208, 214, 72))
        fill_gradient.setColorAt(1.0, QColor(29, 208, 214, 0))
        painter.fillPath(fill_path, fill_gradient)

        painter.setPen(QPen(QColor(APP_COLORS["accent_soft"]), 7))
        painter.setOpacity(0.18)
        painter.drawPath(path)
        painter.setOpacity(1.0)
        painter.setPen(QPen(QColor(APP_COLORS["accent"]), 3))
        painter.drawPath(path)

        label_step = max(1, len(points) // 8)
        for index, (point, label, value) in enumerate(points):
            hit_rect = QRectF(point.x() - 13, point.y() - 13, 26, 26)
            self._trend_regions[label] = hit_rect
            hovered = label == self._hover_trend
            pulse = (math.sin(self._hover_pulse) + 1) / 2 if hovered else 0
            painter.setPen(Qt.NoPen)
            if hovered:
                halo_color = QColor(APP_COLORS["accent_soft"])
                halo_color.setAlpha(54 + int(pulse * 44))
                painter.setBrush(halo_color)
                painter.drawEllipse(point, 17 + pulse * 5, 17 + pulse * 5)
                ring_color = QColor(APP_COLORS["accent"])
                ring_color.setAlpha(115 + int(pulse * 65))
                painter.setPen(QPen(ring_color, 1.4 + pulse))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(point, 23 + pulse * 5, 23 + pulse * 5)
                painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(APP_COLORS["accent"]))
            painter.drawEllipse(point, 8 if hovered else 5, 8 if hovered else 5)
            painter.setBrush(QColor(255, 255, 255, 70))
            painter.drawEllipse(point, 9, 9)

            if index % label_step == 0 or index == len(points) - 1:
                painter.setPen(QColor(APP_COLORS["text"]))
                painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
                painter.drawText(QRectF(point.x() - 18, point.y() - 26, 36, 16), Qt.AlignCenter, str(value))

                painter.setPen(QColor(APP_COLORS["text_dim"]))
                painter.setFont(QFont("Microsoft YaHei UI", 9))
                if self.trend_granularity == "hour":
                    label_text = label[11:16] if len(label) >= 16 else label
                else:
                    label_text = label[5:] if len(label) >= 10 else label
                painter.drawText(
                    QRectF(point.x() - 40, plot_rect.bottom() + 10, 80, 16),
                    Qt.AlignCenter,
                    label_text,
                )


class ChartEffectOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.items = []
        self.particles = []
        self.shockwaves = []
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.hide()

    def resize_to_parent(self):
        parent = self.parentWidget()
        if parent:
            self.setGeometry(parent.rect())

    def _bounded_names(self, names, limit=22):
        unique = []
        seen = set()
        for name in names:
            if not name or name in seen:
                continue
            seen.add(name)
            unique.append(name)
            if len(unique) >= limit:
                break
        return unique

    def play_rarity_burst(self, names, color, origin):
        self.resize_to_parent()
        base = QPointF(origin)
        names = self._bounded_names(names, 22)
        if not names:
            names = ["暂无记录"]
        color = QColor(color)
        self.shockwaves.append(
            {
                "mode": "burst",
                "x": base.x(),
                "y": base.y(),
                "life": 0,
                "ttl": 44,
                "radius": 24,
                "max_radius": 205,
                "color": QColor(color),
                "width": 3.2,
            }
        )
        self.shockwaves.append(
            {
                "mode": "burst",
                "x": base.x(),
                "y": base.y(),
                "life": -9,
                "ttl": 46,
                "radius": 10,
                "max_radius": 145,
                "color": QColor(color),
                "width": 2.0,
            }
        )
        for index, name in enumerate(names):
            angle = (math.pi * 2 / max(1, len(names))) * index + random.uniform(-0.25, 0.25)
            distance = random.uniform(92, 215)
            self.items.append(
                {
                    "mode": "burst",
                    "text": name,
                    "color": QColor(color),
                    "x": base.x(),
                    "y": base.y(),
                    "tx": base.x() + math.cos(angle) * distance,
                    "ty": base.y() + math.sin(angle) * distance,
                    "life": 0,
                    "ttl": random.randint(66, 88),
                    "size": random.randint(11, 15),
                }
            )
        for _ in range(min(150, 46 + len(names) * 6)):
            angle = random.uniform(0, math.pi * 2)
            speed = random.uniform(1.3, 5.4)
            particle_color = QColor(color)
            particle_color.setAlpha(random.randint(120, 210))
            self.particles.append(
                {
                    "x": base.x(),
                    "y": base.y(),
                    "vx": math.cos(angle) * speed,
                    "vy": math.sin(angle) * speed,
                    "life": 0,
                    "ttl": random.randint(38, 70),
                    "color": particle_color,
                    "r": random.uniform(1.4, 3.6),
                }
            )
        self._start()

    def play_blackhole(self, events, target):
        self.resize_to_parent()
        target = QPointF(target)
        if not events:
            events = [{"name": "暂无记录", "color": APP_COLORS["text_dim"]}]
        events = events[:30]
        width = max(1, self.width())
        height = max(1, self.height())
        self.shockwaves.append(
            {
                "mode": "blackhole",
                "x": target.x(),
                "y": target.y(),
                "life": 0,
                "ttl": 56,
                "radius": 170,
                "max_radius": 18,
                "color": QColor(APP_COLORS["accent_soft"]),
                "width": 2.6,
            }
        )
        self.shockwaves.append(
            {
                "mode": "blackhole",
                "x": target.x(),
                "y": target.y(),
                "life": -10,
                "ttl": 64,
                "radius": 250,
                "max_radius": 34,
                "color": QColor(APP_COLORS["accent"]),
                "width": 1.6,
            }
        )
        edge_points = [
            lambda: QPointF(random.uniform(10, width - 10), -24),
            lambda: QPointF(random.uniform(10, width - 10), height + 24),
            lambda: QPointF(-36, random.uniform(20, height - 20)),
            lambda: QPointF(width + 36, random.uniform(20, height - 20)),
        ]
        for index, event in enumerate(events):
            start = edge_points[index % 4]()
            color = QColor(event.get("color", APP_COLORS["accent_soft"]))
            self.items.append(
                {
                    "mode": "blackhole",
                    "text": event.get("name", "未知鱼类"),
                    "color": color,
                    "x": start.x(),
                    "y": start.y(),
                    "tx": target.x(),
                    "ty": target.y(),
                    "life": -index * 2,
                    "ttl": 86,
                    "size": 11,
                }
            )
        for _ in range(128):
            angle = random.uniform(0, math.pi * 2)
            radius = random.uniform(18, 84)
            color = QColor(APP_COLORS["accent_soft"])
            color.setAlpha(random.randint(90, 170))
            self.particles.append(
                {
                    "x": target.x() + math.cos(angle) * radius,
                    "y": target.y() + math.sin(angle) * radius,
                    "vx": -math.cos(angle) * random.uniform(0.3, 1.2),
                    "vy": -math.sin(angle) * random.uniform(0.3, 1.2),
                    "life": 0,
                    "ttl": random.randint(44, 86),
                    "color": color,
                    "r": random.uniform(1.0, 2.8),
                }
            )
        self._start()

    def _start(self):
        self.items = self.items[-96:]
        self.particles = self.particles[-240:]
        self.shockwaves = self.shockwaves[-8:]
        self.show()
        self.raise_()
        if not self.timer.isActive():
            self.timer.start()
        self.update()

    def _tick(self):
        next_items = []
        for item in self.items:
            item["life"] += 1
            if item["life"] <= item["ttl"]:
                next_items.append(item)
        self.items = next_items

        next_particles = []
        for particle in self.particles:
            particle["life"] += 1
            particle["x"] += particle["vx"]
            particle["y"] += particle["vy"]
            particle["vy"] += 0.015
            if particle["life"] <= particle["ttl"]:
                next_particles.append(particle)
        self.particles = next_particles

        next_shockwaves = []
        for wave in self.shockwaves:
            wave["life"] += 1
            if wave["life"] <= wave["ttl"]:
                next_shockwaves.append(wave)
        self.shockwaves = next_shockwaves

        if not self.items and not self.particles and not self.shockwaves:
            self.timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        for wave in self.shockwaves:
            if wave["life"] < 0:
                continue
            progress = max(0.0, min(1.0, wave["life"] / max(1, wave["ttl"])))
            if wave.get("mode") == "blackhole":
                radius = wave["radius"] + (wave["max_radius"] - wave["radius"]) * progress
                alpha = int(155 * (1.0 - progress))
            else:
                eased = 1 - pow(1 - progress, 3)
                radius = wave["radius"] + (wave["max_radius"] - wave["radius"]) * eased
                alpha = int(180 * (1.0 - progress))
            color = QColor(wave["color"])
            color.setAlpha(max(0, alpha))
            pen = QPen(color, max(0.6, wave["width"] * (1.0 - progress * 0.45)))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(wave["x"], wave["y"]), radius, radius)

        for particle in self.particles:
            progress = max(0.0, min(1.0, particle["life"] / max(1, particle["ttl"])))
            color = QColor(particle["color"])
            color.setAlpha(int(color.alpha() * (1.0 - progress)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(particle["x"], particle["y"]), particle["r"], particle["r"])

        for item in self.items:
            if item["life"] < 0:
                continue
            progress = max(0.0, min(1.0, item["life"] / max(1, item["ttl"])))
            if item["mode"] == "blackhole":
                eased = progress * progress
                x = item["x"] + (item["tx"] - item["x"]) * eased
                y = item["y"] + (item["ty"] - item["y"]) * eased
                alpha = int(235 * (1.0 - max(0, progress - 0.70) / 0.30))
            else:
                eased = 1 - pow(1 - progress, 3)
                x = item["x"] + (item["tx"] - item["x"]) * eased
                y = item["y"] + (item["ty"] - item["y"]) * eased - math.sin(progress * math.pi) * 24
                alpha = int(235 * (1.0 - max(0, progress - 0.68) / 0.32))
            color = QColor(item["color"])
            color.setAlpha(max(0, alpha))
            painter.setPen(color)
            painter.setFont(QFont("Microsoft YaHei UI", item["size"], QFont.Bold))
            painter.drawText(QRectF(x - 58, y - 12, 116, 24), Qt.AlignCenter, item["text"])


class SummaryAchievementPanel(QWidget):
    def __init__(self, stats, mode="stage", parent=None):
        super().__init__(parent)
        self.stats = stats
        self.mode = mode
        self._hover_rarity = ""
        self._rarity_regions = {}
        self._pulse = 0.0
        self.setMouseTracking(True)
        self.setMinimumHeight(176)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.timer = QTimer(self)
        self.timer.setInterval(45)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        self._pulse = (self._pulse + 0.12) % (math.pi * 2)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        hovered = ""
        for rarity, region in self._rarity_regions.items():
            if region.contains(pos):
                hovered = rarity
                break
        if hovered != self._hover_rarity:
            self._hover_rarity = hovered
            self.setCursor(Qt.PointingHandCursor if hovered else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_rarity = ""
        self.setCursor(Qt.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._rarity_regions = {}

        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        bg = QLinearGradient(rect.topLeft(), rect.bottomRight())
        bg.setColorAt(0.0, QColor(12, 31, 48, 235))
        bg.setColorAt(0.55, QColor(9, 21, 34, 244))
        bg.setColorAt(1.0, QColor(5, 12, 22, 248))
        painter.fillPath(path, bg)
        painter.setPen(QPen(QColor(99, 228, 228, 32), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        total = max(0, int(self.stats.get("total", 0)))
        species = max(0, int(self.stats.get("species", 0)))
        total_weight = max(0, int(self.stats.get("total_weight", 0)))
        max_weight = max(0, int(self.stats.get("max_weight", 0)))
        rarity_counter = self.stats.get("rarity_counter", Counter())
        max_rarity_count = max(rarity_counter.values(), default=1)
        score = total * 10 + species * 45 + int(total_weight / 1000)

        title_rect = QRectF(rect.left() + 18, rect.top() + 12, rect.width() - 36, 22)
        painter.setPen(QColor(APP_COLORS["text"]))
        painter.setFont(QFont("Microsoft YaHei UI", 12, QFont.Bold))
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, "成果能量总览")
        painter.setPen(QColor(APP_COLORS["text_dim"]))
        painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
        painter.drawText(title_rect, Qt.AlignRight | Qt.AlignVCenter, "完整" if self.mode == "full" else "阶段")

        orb_center = QPointF(rect.left() + 88, rect.center().y() + 12)
        pulse = (math.sin(self._pulse) + 1) / 2
        orb_radius = 42 + pulse * 2
        orb_glow = QColor(APP_COLORS["accent_soft"])
        orb_glow.setAlpha(34 + int(pulse * 24))
        painter.setPen(Qt.NoPen)
        painter.setBrush(orb_glow)
        painter.drawEllipse(orb_center, orb_radius + 14, orb_radius + 14)
        orb_bg = QLinearGradient(QPointF(orb_center.x() - orb_radius, orb_center.y() - orb_radius), QPointF(orb_center.x() + orb_radius, orb_center.y() + orb_radius))
        orb_bg.setColorAt(0.0, QColor(22, 61, 78, 245))
        orb_bg.setColorAt(1.0, QColor(5, 12, 22, 255))
        painter.setBrush(orb_bg)
        painter.drawEllipse(orb_center, orb_radius, orb_radius)
        painter.setPen(QColor(APP_COLORS["text_dim"]))
        painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        painter.drawText(QRectF(orb_center.x() - 38, orb_center.y() - 21, 76, 16), Qt.AlignCenter, "成果值")
        painter.setPen(QColor(APP_COLORS["text"]))
        painter.setFont(QFont("Microsoft YaHei UI", 19, QFont.Bold))
        painter.drawText(QRectF(orb_center.x() - 40, orb_center.y() - 3, 80, 30), Qt.AlignCenter, str(score))

        metric_left = rect.left() + 174
        metric_top = rect.top() + 48
        metrics = [
            ("记录", f"{total} 条", APP_COLORS["accent"]),
            ("鱼种", f"{species} 种", APP_COLORS["success"]),
            ("总重", f"{total_weight} g", APP_COLORS["warning"]),
            ("峰值", f"{max_weight} g", "#B677FF"),
        ]
        metric_width = max(108, (rect.width() - metric_left - 22) / 4 - 9)
        for index, (name, value, color) in enumerate(metrics):
            card = QRectF(metric_left + index * (metric_width + 9), metric_top, metric_width, 44)
            card_path = QPainterPath()
            card_path.addRoundedRect(card, 14, 14)
            painter.fillPath(card_path, QColor(255, 255, 255, 10))
            painter.setPen(QPen(QColor(color), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(card_path)
            painter.setPen(QColor(color))
            painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
            painter.drawText(QRectF(card.left() + 10, card.top() + 5, card.width() - 20, 12), Qt.AlignLeft | Qt.AlignVCenter, name)
            painter.setPen(QColor(APP_COLORS["text"]))
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            painter.drawText(QRectF(card.left() + 10, card.top() + 20, card.width() - 20, 17), Qt.AlignLeft | Qt.AlignVCenter, value)

        bar_top = rect.top() + 106
        bar_left = metric_left
        bar_right = rect.right() - 22
        for index, rarity in enumerate(RARITY_ORDER):
            count = rarity_counter.get(rarity, 0)
            meta = RARITY_META.get(rarity, {"label": rarity, "color": APP_COLORS["text_dim"], "glow": APP_COLORS["text_dim"]})
            y = bar_top + index * 12
            label_rect = QRectF(bar_left, y - 5, 50, 12)
            value_rect = QRectF(bar_right - 76, y - 5, 76, 12)
            track_rect = QRectF(bar_left + 54, y - 2, max(28, bar_right - bar_left - 138), 6)
            self._rarity_regions[rarity] = track_rect.adjusted(-4, -6, 4, 6)
            hovered = rarity == self._hover_rarity
            painter.setPen(QColor(meta["color"] if hovered else APP_COLORS["text_dim"]))
            painter.setFont(QFont("Microsoft YaHei UI", 7, QFont.Bold))
            painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, meta["label"])
            painter.setPen(QColor(APP_COLORS["text"]))
            painter.drawText(value_rect, Qt.AlignRight | Qt.AlignVCenter, f"{count} 条")
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 12))
            painter.drawRoundedRect(track_rect, 3, 3)
            fill = QRectF(track_rect.left(), track_rect.top(), max(3, track_rect.width() * count / max_rarity_count), track_rect.height())
            fill_color = QColor(meta["color"])
            fill_color.setAlpha(220 if hovered else 165)
            painter.setBrush(fill_color)
            painter.drawRoundedRect(fill, 3, 3)


class SummaryChoiceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_mode = None
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 340)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(8, 18, 31, 0.97);
                border: 1px solid rgba(99, 228, 228, 0.34);
                border-radius: 28px;
            }}
            """
        )
        add_shadow(shell, blur=34, alpha=130, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("选择总结范围")
        title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 24px; font-weight: 900;")
        header.addWidget(title)
        header.addStretch()
        close_btn = DialogCloseButton()
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        layout.addLayout(header)

        tip = QLabel("完整总结用于查看全部历史成果；阶段总结只统计上次阶段总结后的新增记录。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; font-weight: 700;")
        layout.addWidget(tip)

        options = QHBoxLayout()
        options.setSpacing(14)
        options.addWidget(self._make_option("完整总结", "全部历史成果", "full"))
        options.addWidget(self._make_option("阶段总结", "本次新增成果", "stage"))
        layout.addLayout(options, 1)

    def _make_option(self, title, desc, mode):
        button = QPushButton(f"{title}\n{desc}")
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(142)
        button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(15, 31, 50, 0.84);
                color: {APP_COLORS['text']};
                border: 1px solid rgba(99, 228, 228, 0.24);
                border-radius: 24px;
                padding: 18px;
                font-size: 14px;
                font-weight: 900;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: rgba(20, 54, 70, 0.94);
                border: 1px solid rgba(99, 228, 228, 0.70);
            }}
            QPushButton:pressed {{
                background-color: rgba(29, 208, 214, 0.22);
            }}
            """
        )
        button.clicked.connect(lambda: self._choose(mode))
        return button

    def _choose(self, mode):
        self.selected_mode = mode
        self.accept()


class SummaryDialog(QDialog):
    def __init__(self, records, encyclopedia, mode="stage", parent=None):
        super().__init__(parent)
        self.records = records or []
        self.encyclopedia = encyclopedia or {}
        self.mode = mode if mode in {"full", "stage"} else "stage"
        self.stats = self._compute_stats()
        self.full_text = self._build_summary_text()
        self.full_html = self._build_summary_html()
        self.cursor = 0
        self.particles = []
        self.rings = []
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(900, 790)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(8, 18, 31, 0.97);
                border: 1px solid rgba(99, 228, 228, 0.32);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=34, alpha=130, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_text = "完整钓鱼成果总结" if self.mode == "full" else "阶段钓鱼成果总结"
        title = QLabel(title_text)
        title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 24px; font-weight: 900;")
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("完成")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setStyleSheet(secondary_button_stylesheet())
        close_btn.clicked.connect(self.accept)
        header.addWidget(close_btn)
        layout.addLayout(header)

        image_panel = QFrame()
        image_panel.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(15, 29, 47, 0.62);
                border: 1px solid rgba(111, 145, 182, 0.16);
                border-radius: 22px;
            }}
            """
        )
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(14, 12, 14, 12)
        image_layout.setSpacing(8)
        image_title = QLabel("收获鱼种")
        image_title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 12px; font-weight: 900;")
        image_layout.addWidget(image_title)

        self.fish_scroll = QScrollArea()
        self.fish_scroll.setWidgetResizable(False)
        self.fish_scroll.setFrameShape(QFrame.NoFrame)
        self.fish_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.fish_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.fish_scroll.setFixedHeight(134)
        self.fish_scroll.viewport().setStyleSheet("background: transparent; border: none;")
        self.fish_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollBar:horizontal {{
                background: rgba(5, 12, 22, 0.96);
                border: 1px solid rgba(99, 228, 228, 0.18);
                height: 10px;
                border-radius: 5px;
                margin: 0px 8px 0px 8px;
            }}
            QScrollBar::handle:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(29, 208, 214, 0.85),
                    stop:1 rgba(99, 228, 228, 0.62));
                margin: 2px 0px 2px 0px;
                border-radius: 4px;
                min-width: 48px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: rgba(99, 228, 228, 0.92);
            }}
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background: rgba(5, 12, 22, 0.96);
                border-radius: 4px;
            }}
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
                image: none;
            }}
            QScrollBar::left-arrow:horizontal,
            QScrollBar::right-arrow:horizontal {{
                width: 0px;
                height: 0px;
                background: transparent;
                image: none;
            }}
            """
        )
        self.fish_strip = QWidget()
        self.fish_strip.setStyleSheet("background: transparent; border: none;")
        self.image_row = QHBoxLayout(self.fish_strip)
        self.image_row.setContentsMargins(0, 0, 0, 0)
        self.image_row.setSpacing(10)
        self._populate_images()
        self.fish_scroll.setWidget(self.fish_strip)
        image_layout.addWidget(self.fish_scroll)
        layout.addWidget(image_panel)

        self.achievement_panel = SummaryAchievementPanel(self.stats, self.mode)
        layout.addWidget(self.achievement_panel)

        self.text_label = QLabel("")
        self.text_label.setTextFormat(Qt.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text_label.setMinimumHeight(260)
        self.text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.text_label.setCursor(Qt.PointingHandCursor)
        self.text_label.setToolTip("总结播放完成后会自动高亮关键成果数据")
        self.text_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(8, 19, 31, 0.88);
                border: 1px solid rgba(99, 228, 228, 0.26);
                border-radius: 22px;
                color: {APP_COLORS['text']};
                font-size: 14px;
                font-weight: 850;
                padding: 18px;
            }}
            QLabel:hover {{
                border: 1px solid rgba(99, 228, 228, 0.62);
                background-color: rgba(11, 25, 39, 0.92);
            }}
            """
        )
        layout.addWidget(self.text_label, 1)

        self._seed_opening_effect()
        self.type_timer = QTimer(self)
        self.type_timer.setInterval(18)
        self.type_timer.timeout.connect(self._type_next)
        self.particle_timer = QTimer(self)
        self.particle_timer.setInterval(16)
        self.particle_timer.timeout.connect(self._tick_particles)
        self.type_timer.start()
        self.particle_timer.start()

    def _make_stat_card(self, title, value, color):
        card = QFrame()
        card.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(8, 19, 31, 0.78);
                border: 1px solid rgba(99, 228, 228, 0.18);
                border-radius: 18px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )
        card.setMinimumHeight(74)
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(4)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {APP_COLORS['text_dim']}; font-size: 12px; font-weight: 800;")
        value_label = QLabel(value)
        value_label.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: 950;")
        box.addWidget(title_label)
        box.addWidget(value_label)
        return card

    def _compute_stats(self):
        total = len(self.records)
        weights = [int(record.get("weight", 0) or 0) for record in self.records]
        species_counter = Counter(record.get("fish_name", "未知鱼类") for record in self.records)
        rarity_counter = Counter(record.get("rarity", "未知稀有度") for record in self.records)
        species_rarity = {}
        rarity_rank = {rarity: index for index, rarity in enumerate(RARITY_ORDER)}
        fallback_rank = len(RARITY_ORDER) + 1
        for record in self.records:
            name = record.get("fish_name", "未知鱼类")
            rarity = record.get("rarity", "未知稀有度")
            current = species_rarity.get(name)
            if current is None or rarity_rank.get(rarity, fallback_rank) < rarity_rank.get(current, fallback_rank):
                species_rarity[name] = rarity
        max_weight = max(weights, default=0)
        max_record = max(self.records, key=lambda item: int(item.get("weight", 0) or 0), default={})
        dominant_rarity = rarity_counter.most_common(1)[0][0] if rarity_counter else "未知稀有度"
        return {
            "total": total,
            "total_weight": sum(weights),
            "species": len(species_counter),
            "species_counter": species_counter,
            "species_rarity": species_rarity,
            "rarity_counter": rarity_counter,
            "max_weight": max_weight,
            "max_record": max_record,
            "dominant_rarity": dominant_rarity,
        }

    def _format_weight(self, weight):
        return f"{int(weight)} g"

    def _ordered_species(self):
        species_counter = self.stats["species_counter"]
        species_rarity = self.stats["species_rarity"]
        grouped = defaultdict(list)
        for name, count in species_counter.items():
            grouped[species_rarity.get(name, "未知稀有度")].append((name, count))
        ordered = []
        for rarity in RARITY_ORDER + ["未知稀有度"]:
            for name, count in sorted(grouped.get(rarity, []), key=lambda item: (-item[1], item[0])):
                ordered.append((name, count, rarity))
        return ordered

    def _populate_images(self):
        entries = self._ordered_species()
        if not entries:
            tip = QLabel("暂无可展示鱼图鉴")
            tip.setAlignment(Qt.AlignCenter)
            tip.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px; font-weight: 800;")
            self.image_row.addWidget(tip)
            self.fish_strip.setMinimumWidth(420)
            return
        card_width = 116
        for name, count, rarity in entries:
            meta = RARITY_META.get(rarity, RARITY_META["未知稀有度"])
            image_path = self.encyclopedia.get(name, {}).get("image_path") or ""
            card = QFrame()
            card.setObjectName("summaryFishCard")
            card.setAttribute(Qt.WA_Hover, True)
            card.setFixedSize(card_width, 118)
            card.setStyleSheet(
                f"""
                QFrame#summaryFishCard {{
                    background-color: rgba(7, 17, 29, 0.92);
                    border: 1px solid rgba(99, 228, 228, 0.16);
                    border-radius: 18px;
                }}
                QFrame#summaryFishCard:hover {{
                    background-color: rgba(10, 24, 38, 0.98);
                    border: 1px solid {meta['color']};
                }}
                QLabel {{
                    background: transparent;
                    border: none;
                    color: {APP_COLORS['text_dim']};
                }}
                """
            )
            box = QVBoxLayout(card)
            box.setContentsMargins(8, 8, 8, 8)
            box.setSpacing(4)

            image_label = QLabel()
            image_label.setFixedSize(98, 58)
            image_label.setAlignment(Qt.AlignCenter)
            image_label.setStyleSheet(
                f"background-color: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); border-radius: 13px; color: {APP_COLORS['text_dim']}; font-size: 11px; font-weight: 900;"
            )
            if image_path:
                pixmap = rounded_pixmap(image_path, 96, 56, 12, keep_full=True)
                if not pixmap.isNull():
                    image_label.setPixmap(pixmap)
                else:
                    image_label.setText(name[:4])
            else:
                image_label.setText(name[:4])
            box.addWidget(image_label, 0, Qt.AlignCenter)

            name_label = QLabel(name)
            name_label.setAlignment(Qt.AlignCenter)
            name_label.setToolTip(name)
            name_label.setStyleSheet(f"color: {APP_COLORS['text']}; font-size: 11px; font-weight: 900;")
            box.addWidget(name_label)

            meta_row = QLabel(f"{meta['label']}  x{count}")
            meta_row.setAlignment(Qt.AlignCenter)
            meta_row.setStyleSheet(f"color: {meta['color']}; font-size: 10px; font-weight: 900;")
            box.addWidget(meta_row)

            spark = QFrame()
            spark.setFixedHeight(3)
            spark.setStyleSheet(f"background-color: {meta['color']}; border: none; border-radius: 2px;")
            box.addWidget(spark)

            self.image_row.addWidget(card)
        self.image_row.addStretch()
        self.fish_strip.setMinimumWidth(max(420, len(entries) * (card_width + 10) + 20))

    def _build_summary_text(self):
        if not self.records:
            if self.mode == "full":
                return "当前暂无钓鱼记录可总结。\n开始自动钓鱼并产生记录后，再回到这里生成完整成果总结。"
            return "本阶段暂无新的钓鱼记录可总结。\n阶段总结只统计上次阶段总结之后的新记录；如果需要查看全部历史成果，请选择完整总结。"

        total = self.stats["total"]
        total_weight = self.stats["total_weight"]
        species_counter = self.stats["species_counter"]
        rarity_counter = self.stats["rarity_counter"]
        max_record = self.stats["max_record"]
        rare_lines = []
        for rarity in RARITY_ORDER:
            if rarity_counter.get(rarity, 0):
                meta = RARITY_META.get(rarity, {"label": rarity})
                rare_lines.append(f"{meta['label']} {rarity_counter[rarity]} 条")
        top_species = "、".join(f"{name} x{count}" for name, count in species_counter.most_common(6))
        scope = "完整累计" if self.mode == "full" else "本阶段新增"
        tail = (
            "完整总结统计当前本地保存的全部历史记录，不会改变阶段总结进度。"
            if self.mode == "full"
            else "阶段总结完成后会记录本次进度，下次阶段总结将只统计之后新增的数据。"
        )
        return (
            f"{scope}钓鱼记录 {total} 条，累计重量 {total_weight} g。\n"
            f"出现鱼种 {len(species_counter)} 种，代表收获为：{top_species or '暂无'}。\n"
            f"稀有度分布：{'，'.join(rare_lines) if rare_lines else '暂无'}。\n"
            f"最大重量为 {int(max_record.get('weight', 0) or 0)} g，鱼种是 {max_record.get('fish_name', '未知鱼类')}。\n"
            f"本次成果已生成可视化回顾：高频鱼种、稀有度分布和重量峰值会一起计入本次总结。\n"
            f"{tail}"
        )

    def _build_summary_html(self):
        if not self.records:
            return f"<div style='line-height:1.7; color:{APP_COLORS['text']};'>{html.escape(self.full_text).replace(chr(10), '<br>')}</div>"

        total = self.stats["total"]
        total_weight = self.stats["total_weight"]
        species_counter = self.stats["species_counter"]
        rarity_counter = self.stats["rarity_counter"]
        max_record = self.stats["max_record"]
        max_name = html.escape(max_record.get("fish_name", "未知鱼类"))
        top_species = "、".join(
            f"<span style='color:{APP_COLORS['accent_soft']}; font-weight:900;'>{html.escape(name)}</span> x{count}"
            for name, count in species_counter.most_common(6)
        )
        rare_chips = []
        for rarity in RARITY_ORDER:
            count = rarity_counter.get(rarity, 0)
            if not count:
                continue
            meta = RARITY_META.get(rarity, {"label": rarity, "color": APP_COLORS["text_dim"]})
            rare_chips.append(
                f"<span style='color:{meta['color']}; font-weight:900;'>{html.escape(meta['label'])} {count} 条</span>"
            )
        scope = "完整累计" if self.mode == "full" else "本阶段新增"
        tail = (
            "完整总结不会改变阶段总结进度。"
            if self.mode == "full"
            else "阶段总结完成后会记录本次进度。"
        )
        return (
            "<div style='line-height:1.75;'>"
            f"<p><span style='color:{APP_COLORS['accent_soft']}; font-weight:950;'>{scope}</span>"
            f"钓鱼记录 <span style='color:{APP_COLORS['accent']}; font-size:20px; font-weight:950;'>{total}</span> 条，"
            f"累计重量 <span style='color:{APP_COLORS['warning']}; font-weight:950;'>{total_weight} g</span>。</p>"
            f"<p>出现鱼种 <span style='color:{APP_COLORS['success']}; font-weight:950;'>{len(species_counter)}</span> 种，"
            f"高频收获：{top_species or '暂无'}。</p>"
            f"<p>稀有度能量：{'　'.join(rare_chips) if rare_chips else '暂无'}。</p>"
            f"<p>重量峰值 <span style='color:#B677FF; font-size:18px; font-weight:950;'>{int(max_record.get('weight', 0) or 0)} g</span>，"
            f"来自 <span style='color:{APP_COLORS['text']}; font-weight:950;'>{max_name}</span>。</p>"
            f"<p style='color:{APP_COLORS['text_dim']};'>{tail} 成果能量总览、收获鱼种和稀有度分布已同步生成。</p>"
            "</div>"
        )

    def _seed_opening_effect(self):
        color = QColor(RARITY_META.get(self.stats["dominant_rarity"], {}).get("color", APP_COLORS["accent_soft"]))
        for index in range(3):
            self.rings.append(
                {
                    "x": self.width() / 2,
                    "y": self.height() / 2,
                    "life": -index * 10,
                    "ttl": 74,
                    "radius": 40 + index * 18,
                    "max_radius": 420 - index * 42,
                    "color": QColor(color),
                    "width": 2.6 - index * 0.4,
                }
            )

    def _type_next(self):
        if self.cursor >= len(self.full_text):
            self.type_timer.stop()
            self.text_label.setTextFormat(Qt.RichText)
            self.text_label.setText(self.full_html)
            return
        step = 3 if self.full_text[self.cursor] != "\n" else 1
        self.cursor = min(len(self.full_text), self.cursor + step)
        self.text_label.setText(self.full_text[:self.cursor])
        self._spawn_type_particles()
        if self.cursor >= len(self.full_text):
            self.type_timer.stop()
            self.text_label.setTextFormat(Qt.RichText)
            self.text_label.setText(self.full_html)

    def _spawn_type_particles(self):
        rarity_counter = self.stats["rarity_counter"]
        color_pool = []
        for rarity, count in rarity_counter.items():
            meta = RARITY_META.get(rarity)
            if meta:
                color_pool.extend([meta["color"]] * min(8, count))
        if not color_pool:
            color_pool = [APP_COLORS["accent_soft"]]
        base = QPointF(70 + random.uniform(0, max(1, self.width() - 140)), self.height() - 100 + random.uniform(-22, 18))
        for _ in range(7):
            color = QColor(random.choice(color_pool))
            color.setAlpha(random.randint(100, 190))
            self.particles.append(
                {
                    "x": base.x(),
                    "y": base.y(),
                    "vx": random.uniform(-1.8, 1.8),
                    "vy": random.uniform(-2.8, -0.5),
                    "life": 0,
                    "ttl": random.randint(28, 54),
                    "color": color,
                    "r": random.uniform(1.2, 3.5),
                }
            )
        self.particles = self.particles[-260:]

    def _tick_particles(self):
        next_particles = []
        for particle in self.particles:
            particle["life"] += 1
            particle["x"] += particle["vx"]
            particle["y"] += particle["vy"]
            particle["vy"] += 0.035
            if particle["life"] <= particle["ttl"]:
                next_particles.append(particle)
        self.particles = next_particles

        next_rings = []
        for ring in self.rings:
            ring["life"] += 1
            if ring["life"] <= ring["ttl"]:
                next_rings.append(ring)
        self.rings = next_rings

        if not self.particles and not self.rings and not self.type_timer.isActive():
            self.particle_timer.stop()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        for ring in self.rings:
            if ring["life"] < 0:
                continue
            progress = max(0.0, min(1.0, ring["life"] / max(1, ring["ttl"])))
            radius = ring["radius"] + (ring["max_radius"] - ring["radius"]) * (1 - pow(1 - progress, 3))
            color = QColor(ring["color"])
            color.setAlpha(int(150 * (1.0 - progress)))
            painter.setPen(QPen(color, max(0.6, ring["width"] * (1.0 - progress * 0.4))))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(ring["x"], ring["y"]), radius, radius)

        for particle in self.particles:
            progress = particle["life"] / max(1, particle["ttl"])
            color = QColor(particle["color"])
            color.setAlpha(int(color.alpha() * (1 - progress)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(particle["x"], particle["y"]), particle["r"], particle["r"])


class ShareDialog(QDialog):
    """分享战绩弹窗: 预览卡片 -> 复制到剪贴板 / 保存为 PNG"""

    def __init__(self, stats_data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("分享钓鱼战绩")
        self.setMinimumSize(440, 600)
        self.setStyleSheet(f"background-color: {APP_COLORS['bg']}; color: {APP_COLORS['text']};")
        self.stats_data = stats_data
        self._preview_pixmap = None
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(1600)
        self._feedback_timer.timeout.connect(lambda: self.copy_btn.setText("复制到剪贴板"))
        self._build_ui()
        self._render_preview()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self.preview_label, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(14)

        self.copy_btn = QPushButton("复制到剪贴板")
        self.copy_btn.setFocusPolicy(Qt.NoFocus)
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setMinimumHeight(42)
        self.copy_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(29, 208, 214, 0.18);
                color: {APP_COLORS['text']};
                border: 1px solid rgba(99, 228, 228, 0.46);
                border-radius: 18px;
                padding: 9px 18px;
                font-size: 13px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(29, 208, 214, 0.28);
                border: 1px solid rgba(99, 228, 228, 0.72);
            }}
            QPushButton:pressed {{
                background-color: rgba(29, 208, 214, 0.34);
            }}
            """
        )
        self.copy_btn.clicked.connect(self._copy_to_clipboard)
        btn_row.addWidget(self.copy_btn)

        save_btn = QPushButton("保存为 PNG")
        save_btn.setFocusPolicy(Qt.NoFocus)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setMinimumHeight(42)
        save_btn.setStyleSheet(secondary_button_stylesheet())
        save_btn.clicked.connect(self._save_png)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _render_preview(self):
        card_w, card_h = 400, 520
        pixmap = QPixmap(card_w, card_h)
        pixmap.fill(Qt.transparent)

        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(0, 0, card_w, card_h, 28, 28)
        p.setClipPath(path)

        bg = QLinearGradient(0, 0, card_w, card_h)
        bg.setColorAt(0.0, QColor(APP_COLORS["bg"]))
        bg.setColorAt(1.0, QColor(APP_COLORS["panel"]))
        p.fillRect(0, 0, card_w, card_h, bg)

        accent = QColor(APP_COLORS["accent"])
        accent.setAlpha(18)
        glow = QLinearGradient(0, 0, card_w, 0)
        glow.setColorAt(0.0, accent)
        glow.setColorAt(0.5, Qt.transparent)
        glow.setColorAt(1.0, accent)
        p.fillRect(0, 0, card_w, card_h, glow)

        p.setPen(QPen(QColor(APP_COLORS["accent_soft"]).lighter(130), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(1, 1, card_w - 2, card_h - 2, 27, 27)

        y = 36
        p.setPen(QColor(APP_COLORS["accent"]))
        p.setFont(QFont("Microsoft YaHei UI", 20, QFont.ExtraBold))
        p.drawText(QRectF(0, y, card_w, 34), Qt.AlignCenter, "异环钓鱼战绩")
        y += 34
        p.setPen(QColor(APP_COLORS["accent_soft"]))
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.drawText(QRectF(0, y, card_w, 18), Qt.AlignCenter, "FISHING RECORD")
        y += 32

        p.setPen(QPen(QColor(APP_COLORS["accent_soft"]), 1))
        accent_line = QColor(APP_COLORS["accent_soft"])
        accent_line.setAlpha(55)
        p.setPen(accent_line)
        p.drawLine(40, y, card_w - 40, y)
        y += 20

        data = self.stats_data
        total = int(data.get("total_caught", 0))
        runtime_sec = int(data.get("total_time_seconds", 0))
        success_rate = data.get("success_rate", 0.0)

        metrics = [
            ("累计钓起", str(total), "条", APP_COLORS["accent"]),
            ("运行时长", f"{runtime_sec // 3600}h {(runtime_sec % 3600) // 60}m", "", "#58C7FF"),
            ("成功率", f"{success_rate:.1f}%", "", APP_COLORS["success"]),
        ]
        col_w = (card_w - 60) / 3
        for idx, (label, value, unit, color) in enumerate(metrics):
            cx = 30 + idx * col_w + col_w / 2
            p.setPen(QColor(APP_COLORS["text_dim"]))
            p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            p.drawText(QRectF(cx - 50, y, 100, 16), Qt.AlignCenter, label)
            p.setPen(QColor(color))
            p.setFont(QFont("Microsoft YaHei UI", 22, QFont.ExtraBold))
            display = f"{value} {unit}" if unit else value
            p.drawText(QRectF(cx - 60, y + 18, 120, 34), Qt.AlignCenter, display)
        y += 64

        accent_line2 = accent_line
        p.setPen(accent_line2)
        p.drawLine(40, y, card_w - 40, y)
        y += 18

        distribution = data.get("distribution", {})
        total_fish = sum(distribution.values()) if distribution else 0
        if total_fish > 0:
            p.setPen(QColor(APP_COLORS["text_dim"]))
            p.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
            p.drawText(QRectF(30, y, 200, 20), Qt.AlignLeft | Qt.AlignVCenter, "稀有度分布")
            p.setPen(QColor(APP_COLORS["text_soft"]))
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.drawText(QRectF(card_w - 130, y, 100, 20), Qt.AlignRight | Qt.AlignVCenter, f"总计 {total_fish} 条")
            y += 28

            bar_left = 30
            bar_right = card_w - 30
            bar_total_w = bar_right - bar_left
            filled = 0
            for rarity in RARITY_ORDER:
                count = distribution.get(rarity, 0)
                if count <= 0:
                    continue
                meta = RARITY_META.get(rarity, RARITY_META["未知稀有度"])
                ratio = count / total_fish
                segment_w = max(2, int(bar_total_w * ratio))
                if filled + segment_w > bar_total_w:
                    segment_w = bar_total_w - filled
                seg_rect = QRectF(bar_left + filled, y, segment_w, 16)
                seg_path = QPainterPath()
                seg_path.addRoundedRect(seg_rect, 4, 4)
                color = QColor(meta["color"])
                color.setAlpha(210)
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.fillPath(seg_path, color)
                filled += segment_w
            y += 24

            for rarity in RARITY_ORDER:
                count = distribution.get(rarity, 0)
                if count <= 0:
                    continue
                meta = RARITY_META.get(rarity, RARITY_META["未知稀有度"])
                percent = int(count / total_fish * 100)

                dot_rect = QRectF(30, y + 2, 10, 10)
                dot_path = QPainterPath()
                dot_path.addEllipse(dot_rect)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(meta["color"]))
                p.fillPath(dot_path, QColor(meta["color"]))

                p.setPen(QColor(APP_COLORS["text"]))
                p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
                p.drawText(QRectF(46, y, 70, 16), Qt.AlignLeft | Qt.AlignVCenter, meta["label"])
                p.setPen(QColor(APP_COLORS["text_soft"]))
                p.setFont(QFont("Microsoft YaHei UI", 9))
                p.drawText(QRectF(120, y, 60, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{count} 条")

                p.setPen(QColor(meta["color"]))
                p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
                p.drawText(QRectF(card_w - 70, y, 40, 16), Qt.AlignRight | Qt.AlignVCenter, f"{percent}%")
                y += 18
            y += 6

        max_record = data.get("max_record", {})
        max_weight = int(max_record.get("weight", 0) or 0)
        max_name = max_record.get("fish_name", "")
        if max_weight > 0 and max_name:
            accent_line3 = accent_line
            p.setPen(accent_line3)
            p.drawLine(40, y, card_w - 40, y)
            y += 16

            p.setPen(QColor(APP_COLORS["text_dim"]))
            p.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            p.drawText(QRectF(30, y, 100, 18), Qt.AlignLeft | Qt.AlignVCenter, "最重鱼获")
            y += 22

            p.setPen(QColor("#B677FF"))
            p.setFont(QFont("Microsoft YaHei UI", 26, QFont.ExtraBold))
            p.drawText(QRectF(30, y, card_w - 60, 38), Qt.AlignCenter, f"{max_weight} g")
            y += 40

            p.setPen(QColor(APP_COLORS["text"]))
            p.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
            p.drawText(QRectF(30, y, card_w - 60, 20), Qt.AlignCenter, max_name)
            y += 24

        p.setPen(QColor(APP_COLORS["text_soft"]))
        p.setFont(QFont("Microsoft YaHei UI", 7))
        try:
            from core.version import APP_VERSION
            version_str = APP_VERSION
        except Exception:
            version_str = ""
        p.drawText(QRectF(0, card_h - 28, card_w, 18), Qt.AlignCenter, f"YHoAutoFish v{version_str}")

        p.end()
        self._preview_pixmap = pixmap
        self.preview_label.setPixmap(pixmap)

    def _copy_to_clipboard(self):
        if self._preview_pixmap and not self._preview_pixmap.isNull():
            QApplication.clipboard().setPixmap(self._preview_pixmap)
            self.copy_btn.setText("已复制!")
            self._feedback_timer.start()

    def _save_png(self):
        if not self._preview_pixmap or self._preview_pixmap.isNull():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存战绩卡片", "fishing_record.png", "PNG 图片 (*.png)"
        )
        if path:
            self._preview_pixmap.save(path, "PNG")


class FishingRecordTableModel(QAbstractTableModel):
    HEADERS = ["时间", "鱼种", "稀有度", "重量"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records = []
        self._signature = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        record = self._records[index.row()]
        column = index.column()
        rarity = record.get("rarity", "未知稀有度")
        rarity_meta = RARITY_META.get(rarity, {"label": "未知", "color": APP_COLORS["text"]})

        if role == Qt.DisplayRole:
            if column == 0:
                return record.get("time", "")
            if column == 1:
                return record.get("fish_name", "未知鱼种")
            if column == 2:
                return rarity_meta["label"]
            if column == 3:
                return f"{int(record.get('weight', 0) or 0)} g"
        if role == Qt.TextAlignmentRole:
            return (Qt.AlignLeft | Qt.AlignVCenter) if column == 0 else (Qt.AlignCenter | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            return QColor(rarity_meta["color"] if column == 2 else APP_COLORS["text"])
        return None

    def set_records(self, records):
        records = list(records or [])
        signature = tuple(
            (
                record.get("time", ""),
                record.get("fish_name", ""),
                record.get("rarity", ""),
                int(record.get("weight", 0) or 0),
            )
            for record in records
        )
        if signature == self._signature:
            return
        self.beginResetModel()
        self._records = records
        self._signature = signature
        self.endResetModel()


class FishingRecordWidget(QWidget):
    def __init__(self, record_mgr):
        super().__init__()
        self.record_mgr = record_mgr
        self.current_chart_mode = "bar"
        self._last_refresh_signature = None
        self._current_rarity_names = {}
        self._current_trend_events = {}
        self.effect_overlay = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.init_ui()

    def init_ui(self):
        self.setStyleSheet(panel_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(5)

        title = QLabel("钓鱼记录")
        title.setProperty("role", "headline")
        title_col.addWidget(title)

        subtitle = QLabel("默认首页展示自动钓鱼成果，支持查询、分类筛选和多图表切换。")
        subtitle.setProperty("role", "subtle")
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        badge = QLabel("记录视图")
        badge.setProperty("role", "accent-chip")
        header.addWidget(badge, 0, Qt.AlignTop)

        self.summary_btn = QPushButton("开始总结")
        self.summary_btn.setFocusPolicy(Qt.NoFocus)
        self.summary_btn.setCursor(Qt.PointingHandCursor)
        self.summary_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(29, 208, 214, 0.18);
                color: {APP_COLORS['text']};
                border: 1px solid rgba(99, 228, 228, 0.46);
                border-radius: 17px;
                padding: 9px 18px;
                font-size: 12px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(29, 208, 214, 0.28);
                border: 1px solid rgba(99, 228, 228, 0.72);
            }}
            QPushButton:pressed {{
                background-color: rgba(29, 208, 214, 0.34);
            }}
            """
        )
        self.summary_btn.clicked.connect(self.start_summary)
        header.addWidget(self.summary_btn, 0, Qt.AlignTop)

        self.share_btn = QPushButton("分享战绩")
        self.share_btn.setFocusPolicy(Qt.NoFocus)
        self.share_btn.setCursor(Qt.PointingHandCursor)
        self.share_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(183, 119, 255, 0.18);
                color: {APP_COLORS['text']};
                border: 1px solid rgba(183, 119, 255, 0.40);
                border-radius: 17px;
                padding: 9px 18px;
                font-size: 12px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(183, 119, 255, 0.28);
                border: 1px solid rgba(183, 119, 255, 0.66);
            }}
            QPushButton:pressed {{
                background-color: rgba(183, 119, 255, 0.34);
            }}
            """
        )
        self.share_btn.clicked.connect(self._open_share_dialog)
        header.addWidget(self.share_btn, 0, Qt.AlignTop)
        layout.addLayout(header)

        self._build_stats(layout)
        self._build_filter_bar(layout)
        self._build_content(layout)
        self.effect_overlay = ChartEffectOverlay(self)

    def _build_stats(self, parent_layout):
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        self.card_total = StatCard("累计钓起", APP_COLORS["accent"])
        self.card_runtime = StatCard("运行时长", "#58C7FF")
        self.card_success = StatCard("成功率", APP_COLORS["success"])
        self.card_weight = StatCard("最大重量", APP_COLORS["warning"])
        self.card_empty = StatCard("连续空竿", APP_COLORS["danger"])
        self.card_unlocked = StatCard("已解锁图鉴", "#B677FF")

        cards = [
            self.card_total,
            self.card_runtime,
            self.card_success,
            self.card_weight,
            self.card_empty,
            self.card_unlocked,
        ]
        for index, card in enumerate(cards):
            grid.addWidget(card, 0, index)
        parent_layout.addLayout(grid)

    def _build_filter_bar(self, parent_layout):
        panel = DashboardPanel()
        row = QHBoxLayout(panel)
        row.setContentsMargins(18, 16, 18, 16)
        row.setSpacing(12)

        label = QLabel("记录检索")
        label.setProperty("role", "section")
        row.addWidget(label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入鱼名快速查询")
        self.search_edit.setStyleSheet(line_edit_stylesheet())
        self.search_edit.textChanged.connect(self._schedule_refresh)
        row.addWidget(self.search_edit, 1)

        self.rarity_combo = QComboBox()
        self.rarity_combo.addItems(["全部稀有度"] + RARITY_ORDER)
        self.rarity_combo.setStyleSheet(combo_stylesheet())
        self.rarity_combo.setFixedWidth(146)
        self.rarity_combo.currentIndexChanged.connect(self.refresh_data)
        row.addWidget(self.rarity_combo)

        self.time_combo = QComboBox()
        self.time_combo.addItems(["全部时间", "今日", "最近24小时", "最近7天", "最近30天"])
        self.time_combo.setStyleSheet(combo_stylesheet())
        self.time_combo.setFixedWidth(136)
        self.time_combo.currentIndexChanged.connect(self.refresh_data)
        row.addWidget(self.time_combo)

        self.weight_combo = QComboBox()
        self.weight_combo.addItems(["全部重量", "小于100g", "100-999g", "1000-9999g", "10000g以上"])
        self.weight_combo.setStyleSheet(combo_stylesheet())
        self.weight_combo.setFixedWidth(138)
        self.weight_combo.currentIndexChanged.connect(self.refresh_data)
        row.addWidget(self.weight_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["按时间倒序", "按重量倒序", "按重量正序"])
        self.sort_combo.setStyleSheet(combo_stylesheet())
        self.sort_combo.setFixedWidth(132)
        self.sort_combo.currentIndexChanged.connect(self.refresh_data)
        row.addWidget(self.sort_combo)

        self.reset_btn = QPushButton("重置筛选")
        self.reset_btn.setFocusPolicy(Qt.NoFocus)
        self.reset_btn.setStyleSheet(secondary_button_stylesheet())
        self.reset_btn.clicked.connect(self._reset_filters)
        row.addWidget(self.reset_btn)

        parent_layout.addWidget(panel)

    def _build_content(self, parent_layout):
        row = QHBoxLayout()
        row.setSpacing(18)

        chart_panel = DashboardPanel()
        chart_layout = QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(18, 18, 18, 18)
        chart_layout.setSpacing(14)

        chart_header = QHBoxLayout()
        chart_title = QLabel("数据图表")
        chart_title.setProperty("role", "section")
        chart_header.addWidget(chart_title)
        chart_header.addStretch()

        self.trend_combo = QComboBox()
        self.trend_combo.addItems(["按日趋势", "按小时趋势"])
        self.trend_combo.setStyleSheet(combo_stylesheet())
        self.trend_combo.setFixedWidth(112)
        self.trend_combo.setVisible(False)
        self.trend_combo.currentIndexChanged.connect(self.refresh_data)
        chart_header.addWidget(self.trend_combo)

        self.chart_buttons = []
        for text, mode in [("柱状图", "bar"), ("扇形图", "pie"), ("折线图", "line")]:
            btn = ChartModeButton(text, mode)
            btn.clicked.connect(self._change_chart_mode)
            self.chart_buttons.append(btn)
            chart_header.addWidget(btn)
        self.chart_buttons[0].setChecked(True)
        chart_layout.addLayout(chart_header)

        self.chart_body = QFrame()
        self.chart_body.setProperty("variant", "soft")
        self.chart_body.setStyleSheet(panel_stylesheet())
        chart_body_layout = QVBoxLayout(self.chart_body)
        chart_body_layout.setContentsMargins(14, 14, 14, 14)
        chart_body_layout.setSpacing(0)

        self.chart = InsightChart()
        self.chart.rarityActivated.connect(self._play_rarity_effect)
        self.chart.trendActivated.connect(self._play_trend_effect)
        chart_body_layout.addWidget(self.chart)
        chart_layout.addWidget(self.chart_body, 1)
        row.addWidget(chart_panel, 4)

        list_panel = DashboardPanel()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(18, 18, 18, 18)
        list_layout.setSpacing(14)

        list_header = QHBoxLayout()
        list_title = QLabel("捕获记录")
        list_title.setProperty("role", "section")
        list_header.addWidget(list_title)
        list_header.addStretch()

        self.result_chip = QLabel("0 条记录")
        self.result_chip.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.04);
                color: {APP_COLORS['text_dim']};
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 16px;
                padding: 8px 14px;
                font-size: 12px;
                font-weight: 700;
            }}
            """
        )
        list_header.addWidget(self.result_chip)
        list_layout.addLayout(list_header)

        self.record_body = QFrame()
        self.record_body.setProperty("variant", "soft")
        self.record_body.setStyleSheet(panel_stylesheet())
        record_body_layout = QVBoxLayout(self.record_body)
        record_body_layout.setContentsMargins(12, 12, 12, 12)
        record_body_layout.setSpacing(8)

        self.record_model = FishingRecordTableModel(self)
        self.record_table = QTableView()
        self.record_table.setModel(self.record_model)
        self.record_table.setAlternatingRowColors(True)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.record_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.record_table.setFocusPolicy(Qt.NoFocus)
        self.record_table.setShowGrid(False)
        self.record_table.setWordWrap(False)
        self.record_table.verticalHeader().setVisible(False)
        self.record_table.verticalHeader().setDefaultSectionSize(42)
        self.record_table.horizontalHeader().setStretchLastSection(True)
        self.record_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.record_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.record_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.record_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.record_table.setColumnWidth(0, 154)
        self.record_table.setColumnWidth(2, 92)
        self.record_table.setColumnWidth(3, 104)
        self.record_table.setStyleSheet(table_stylesheet())
        record_body_layout.addWidget(self.record_table, 1)

        self.empty_tip = QLabel("当前筛选条件下暂无记录")
        self.empty_tip.setAlignment(Qt.AlignCenter)
        self.empty_tip.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 13px;"
        )
        record_body_layout.addWidget(self.empty_tip, 1)

        list_layout.addWidget(self.record_body, 1)
        row.addWidget(list_panel, 5)

        parent_layout.addLayout(row, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.effect_overlay is not None:
            self.effect_overlay.resize_to_parent()

    def _play_rarity_effect(self, rarity, chart_pos):
        if self.effect_overlay is None:
            return
        names = self._current_rarity_names.get(rarity, [])
        color = RARITY_META.get(rarity, RARITY_META["未知稀有度"])["color"]
        origin = QPointF(self.chart.mapTo(self, chart_pos.toPoint()))
        self.effect_overlay.play_rarity_burst(names, color, origin)

    def _play_trend_effect(self, trend_key, chart_pos):
        if self.effect_overlay is None:
            return
        events = self._current_trend_events.get(trend_key, [])
        target = QPointF(self.chart.mapTo(self, chart_pos.toPoint()))
        self.effect_overlay.play_blackhole(events, target)

    def start_summary(self):
        if getattr(self, "_active_summary_dialog", None) is not None:
            self._active_summary_dialog.raise_()
            self._active_summary_dialog.activateWindow()
            return

        choice = SummaryChoiceDialog(self.window())
        center = self.window().geometry().center()
        choice.move(center - choice.rect().center())
        if choice.exec() != QDialog.Accepted or not choice.selected_mode:
            return

        summary_mode = choice.selected_mode
        if summary_mode == "full":
            records = self.record_mgr.get_history()
        else:
            records = self.record_mgr.get_unsummarized_history()
        encyclopedia = self.record_mgr.get_encyclopedia()
        dialog = SummaryDialog(records, encyclopedia, summary_mode, self.window())
        center = self.window().geometry().center()
        dialog.move(center - dialog.rect().center())
        self._active_summary_dialog = dialog

        def _on_summary_closed(_result):
            if summary_mode == "stage" and records:
                self.record_mgr.mark_summary_completed()
            setattr(self, "_active_summary_dialog", None)

        dialog.finished.connect(_on_summary_closed)
        dialog.open()

    def _open_share_dialog(self):
        stats = self.record_mgr.get_stats()
        total_caught = stats.get("total_caught", 0)
        total_attempts = stats.get("total_attempts", 0)
        runtime = stats.get("total_time_seconds", 0)
        success_rate = (total_caught / total_attempts * 100) if total_attempts else 0.0
        all_history = self.record_mgr.get_history()
        distribution = self.record_mgr.get_rarity_distribution(all_history)
        encyclopedia = self.record_mgr.get_encyclopedia()
        max_record = {}
        max_weight = 0
        for record in all_history:
            w = int(record.get("weight", 0) or 0)
            if w > max_weight:
                max_weight = w
                max_record = record

        stats_data = {
            "total_caught": total_caught,
            "total_time_seconds": runtime,
            "success_rate": success_rate,
            "distribution": distribution,
            "max_record": max_record,
        }
        dialog = ShareDialog(stats_data, self.window())
        center = self.window().geometry().center()
        dialog.move(center - dialog.rect().center())
        dialog.open()

    def _schedule_refresh(self):
        self.refresh_timer.start()

    def _change_chart_mode(self):
        sender = self.sender()
        if not isinstance(sender, ChartModeButton):
            return
        self.current_chart_mode = sender.mode
        for button in self.chart_buttons:
            button.setChecked(button is sender)
        self.trend_combo.setVisible(self.current_chart_mode == "line")
        self.chart.set_mode(self.current_chart_mode)

    def _reset_filters(self):
        blockers = [
            QSignalBlocker(self.search_edit),
            QSignalBlocker(self.rarity_combo),
            QSignalBlocker(self.time_combo),
            QSignalBlocker(self.weight_combo),
            QSignalBlocker(self.sort_combo),
        ]
        self.search_edit.clear()
        self.rarity_combo.setCurrentIndex(0)
        self.time_combo.setCurrentIndex(0)
        self.weight_combo.setCurrentIndex(0)
        self.sort_combo.setCurrentIndex(0)
        del blockers
        self.refresh_data()

    def _populate_table(self, history):
        self.record_model.set_records(history)

    def refresh_data(self):
        stats = self.record_mgr.get_stats()
        encyclopedia = self.record_mgr.get_encyclopedia()

        keyword = self.search_edit.text().strip()
        rarity = self.rarity_combo.currentText()
        period = self.time_combo.currentText()
        weight_bucket = self.weight_combo.currentText()
        sort_mode = self.sort_combo.currentText()
        trend_granularity = "hour" if self.trend_combo.currentIndex() == 1 else "day"
        refresh_signature = (
            getattr(self.record_mgr, "_cache_version", 0),
            keyword,
            rarity,
            period,
            weight_bucket,
            sort_mode,
            trend_granularity,
        )
        if refresh_signature == self._last_refresh_signature:
            return
        self._last_refresh_signature = refresh_signature

        history = self.record_mgr.query_history(
            keyword=keyword,
            rarity=rarity,
            period=period,
            weight_bucket=weight_bucket,
        )

        if sort_mode == "按重量倒序":
            history.sort(key=lambda item: item.get("weight", 0), reverse=True)
        elif sort_mode == "按重量正序":
            history.sort(key=lambda item: item.get("weight", 0))
        else:
            history.sort(key=lambda item: item.get("time", ""), reverse=True)

        total_caught = stats.get("total_caught", 0)
        total_attempts = stats.get("total_attempts", 0)
        runtime = stats.get("total_time_seconds", 0)
        success_rate = (total_caught / total_attempts * 100) if total_attempts else 0
        max_weight = max((data.get("max_weight", 0) for data in encyclopedia.values()), default=0)
        unlocked = sum(1 for data in encyclopedia.values() if data.get("caught_count", 0) > 0)

        self.card_total.set_data(str(total_caught), f"检索结果 {len(history)} 条")
        self.card_runtime.set_data(f"{runtime // 3600}h {(runtime % 3600) // 60}m", "累计运行")
        self.card_success.set_data(f"{success_rate:.1f}%", f"总尝试 {total_attempts} 次")
        self.card_weight.set_data(f"{max_weight} g", "历史最大值")
        self.card_empty.set_data(str(stats.get("consecutive_empty", 0)), "空竿连续计数")
        self.card_unlocked.set_data(f"{unlocked}/{len(encyclopedia)}", "图鉴收集进度")

        distribution = self.record_mgr.get_rarity_distribution(history)
        trend_source = defaultdict(int)
        rarity_names = defaultdict(list)
        trend_events = defaultdict(list)
        for record in history:
            record_rarity = record.get("rarity", "未知稀有度")
            fish_name = record.get("fish_name", "未知鱼类")
            rarity_names[record_rarity].append(fish_name)
            record_time = record.get("time", "")
            if trend_granularity == "hour":
                key = f"{record_time[:13]}:00" if len(record_time) >= 13 else ""
            else:
                key = record_time[:10]
            if key:
                trend_source[key] += 1
                trend_events[key].append(
                    {
                        "name": fish_name,
                        "color": RARITY_META.get(record_rarity, RARITY_META["未知稀有度"])["color"],
                    }
                )
        limit = 24 if trend_granularity == "hour" else 14
        trend_points = [(key, trend_source[key]) for key in sorted(trend_source.keys())[-limit:]]
        visible_trend_keys = {key for key, _ in trend_points}
        self._current_rarity_names = {
            key: [name for name, _count in Counter(value).most_common(18)]
            for key, value in rarity_names.items()
        }
        self._current_trend_events = {
            key: value[-24:]
            for key, value in trend_events.items()
            if key in visible_trend_keys
        }

        self.chart.set_data(distribution, trend_points, trend_granularity)

        self._populate_table(history)

        has_records = len(history) > 0
        self.record_table.setVisible(has_records)
        self.empty_tip.setVisible(not has_records)
        self.result_chip.setText(f"{len(history)} 条记录")
