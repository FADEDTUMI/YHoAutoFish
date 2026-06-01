import html
import json
import os
import queue
import random
import threading
import time
import ctypes
from ctypes import wintypes
from collections import deque

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPoint, Qt, QThread, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QDesktopServices, QFont, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QLinearGradient, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.auth_client import (
    AuthClient,
    AuthClientError,
    _process_hosts_update,
    auth_check_interval_seconds,
    auth_config_required,
    auth_offline_grace_seconds,
    build_pending_activation_state,
    classify_auth_recovery,
    decode_jwt_payload,
    decide_cached_authorization,
    should_persist_auth_failure,
    should_show_authorization_dialog_after_check,
)
from core.auth_device import build_device_hash, build_device_hash_v2, get_or_create_install_id
from core.auth_license import LICENSE_GRACE_DAYS
from core.auth_policy import get_auth_base_url, get_effective_hosts, is_ip_host, update_remote_hosts
from core.auth_store import AUTH_CLOCK_SKEW_WARNING_SECONDS, AuthState, load_auth_state, save_auth_state
from core.auth_ws import AuthWSWorker
from core.paths import ensure_writable_file, resource_path
from core.monthly_card_reset import (
    CONFIG_KEY_ENABLED as MONTHLY_CARD_RESET_ENABLED_KEY,
    CONFIG_KEY_LAST_DATE as MONTHLY_CARD_RESET_LAST_DATE_KEY,
    DEFAULT_CONFIG as MONTHLY_CARD_RESET_DEFAULT_CONFIG,
    MonthlyCardDailyResetScheduler,
    perform_double_escape_reset,
)
from core.state_machine import StateMachine
from core.tracker import EventTracker
from core.version import APP_AUTHOR, APP_DISPLAY_NAME, APP_REPOSITORY_URL, APP_VERSION
from core.updater import DownloadCancelled, UpdateError, check_for_update, download_update, get_download_candidates, start_external_update
from gui.encyclopedia import EncyclopediaWidget
from gui.fishing_record import FishingRecordWidget
from gui.theme import (
    APP_COLORS,
    add_shadow,
    panel_stylesheet,
    primary_button_stylesheet,
    rounded_pixmap,
    scroll_area_stylesheet,
    scrollbar_stylesheet,
    secondary_button_stylesheet,
    text_edit_stylesheet,
)

CONFIG_FILE = ensure_writable_file("config.json")


class BackdropFrame(QFrame):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0, 0, -1, -1), 32, 32)
        painter.fillPath(path, QColor(APP_COLORS["bg_alt"]))

        glow1 = QLinearGradient(rect.topLeft(), rect.bottomRight())
        glow1.setColorAt(0.0, QColor(17, 199, 214, 62))
        glow1.setColorAt(0.55, QColor(8, 18, 30, 0))
        glow1.setColorAt(1.0, QColor(17, 199, 214, 0))
        painter.fillPath(path, glow1)

        glow2 = QLinearGradient(rect.topRight(), rect.bottomLeft())
        glow2.setColorAt(0.0, QColor(120, 170, 255, 28))
        glow2.setColorAt(0.4, QColor(10, 18, 28, 0))
        glow2.setColorAt(1.0, QColor(10, 18, 28, 0))
        painter.fillPath(path, glow2)

        painter.setPen(QPen(QColor(74, 107, 141, 72), 1))
        painter.drawPath(path)
        super().paintEvent(event)


class NavButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(52)
        self.setFont(QFont("Microsoft YaHei UI", 11, QFont.DemiBold))
        self.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                padding-left: 18px;
                color: {APP_COLORS['text_dim']};
                border: 1px solid transparent;
                outline: none;
                border-radius: 18px;
                background-color: transparent;
            }}
            QPushButton:hover {{
                color: {APP_COLORS['text']};
                background-color: rgba(255, 255, 255, 0.04);
            }}
            QPushButton:checked {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.14);
                border: 1px solid rgba(29, 208, 214, 0.36);
            }}
            """
        )


class SettingsCategoryButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(46)
        self.setFont(QFont("Microsoft YaHei UI", 10, QFont.DemiBold))
        self.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                padding: 0 14px;
                color: {APP_COLORS['text_dim']};
                border: 1px solid rgba(111, 145, 182, 0.12);
                outline: none;
                border-radius: 15px;
                background-color: rgba(255, 255, 255, 0.025);
            }}
            QPushButton:hover {{
                color: {APP_COLORS['text']};
                background-color: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(111, 145, 182, 0.22);
            }}
            QPushButton:checked {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.14);
                border: 1px solid rgba(29, 208, 214, 0.42);
            }}
            """
        )


class TitleButton(QPushButton):
    def __init__(self, kind, hover_color, parent=None):
        super().__init__("", parent)
        self.kind = kind
        self.hover_color = hover_color
        self.setFixedSize(46, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; outline: none; }")

    def set_kind(self, kind):
        if self.kind != kind:
            self.kind = kind
            self.update()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(3, 4, -3, -4)
        if self.isDown():
            bg = QColor(255, 255, 255, 38)
        elif self.underMouse():
            bg = QColor(self.hover_color)
        else:
            bg = QColor(255, 255, 255, 0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 11, 11)

        icon_color = QColor(APP_COLORS["text"])
        if self.kind == "close" and self.underMouse():
            icon_color = QColor(255, 255, 255)
        elif not self.underMouse():
            icon_color = QColor(APP_COLORS["text_dim"])

        pen = QPen(icon_color, 1.35)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        cx = self.width() / 2
        cy = self.height() / 2
        if self.kind == "min":
            painter.drawLine(int(cx - 5), int(cy + 1), int(cx + 5), int(cy + 1))
        elif self.kind == "about":
            painter.drawEllipse(int(cx - 6), int(cy - 6), 12, 12)
            painter.drawPoint(int(cx), int(cy - 3))
            painter.drawLine(int(cx), int(cy), int(cx), int(cy + 4))
        elif self.kind == "max":
            painter.drawRect(int(cx - 5), int(cy - 5), 10, 10)
        elif self.kind == "restore":
            painter.drawRect(int(cx - 6), int(cy - 2), 8, 8)
            painter.drawRect(int(cx - 2), int(cy - 6), 8, 8)
        elif self.kind == "close":
            painter.drawLine(int(cx - 5), int(cy - 5), int(cx + 5), int(cy + 5))
            painter.drawLine(int(cx + 5), int(cy - 5), int(cx - 5), int(cy + 5))


class PulseTitleActionButton(QPushButton):
    closeRequested = Signal()

    def __init__(self, kind, text, tone, has_close=False, parent=None, auto_width=False, min_width=None):
        super().__init__("", parent)
        self.kind = kind
        self.label = text
        self.tone = tone
        self.has_close = bool(has_close)
        self.auto_width = bool(auto_width)
        self.min_width = int(min_width or (220 if self.has_close else 76))
        self.glow_value = 0.0
        self._close_pressed = False
        self.setFixedSize(self.min_width, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; outline: none; }")
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(1960 if tone == "coffee" else 1840)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._set_glow_value)
        self._animation.start()
        self._fit_to_label()

    def set_label(self, text, tone=None, tooltip=None):
        self.label = str(text or "")
        if tone:
            self.tone = tone
        if tooltip is not None:
            self.setToolTip(str(tooltip or ""))
        self._fit_to_label()
        self.update()

    def _fit_to_label(self):
        if not self.auto_width:
            return
        text_width = self.fontMetrics().horizontalAdvance(self.label)
        close_width = 25 if self.has_close else 0
        target_width = max(self.min_width, text_width + 48 + close_width)
        self.setFixedWidth(target_width)

    def _set_glow_value(self, value):
        self.glow_value = float(value)
        self.update()

    def _close_rect(self):
        size = 13
        return (self.width() - size - 8, (self.height() - size) // 2, size, size)

    def _point_in_close(self, pos):
        if not self.has_close:
            return False
        x, y, w, h = self._close_rect()
        return x <= pos.x() <= x + w and y <= pos.y() <= y + h

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._point_in_close(event.position().toPoint()):
            self._close_pressed = True
            event.accept()
            self.update()
            return
        self._close_pressed = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._close_pressed:
            should_emit = self._point_in_close(event.position().toPoint())
            self._close_pressed = False
            event.accept()
            self.update()
            if should_emit:
                self.closeRequested.emit()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._close_pressed = False
        self.update()
        super().leaveEvent(event)

    def _tone_colors(self):
        pulse = 0.35 + self.glow_value * 0.65
        if self.tone == "coffee":
            return (
                QColor(126, 78, 40, int(58 + pulse * 70)),
                QColor(230, 174, 102, int(132 + pulse * 105)),
                QColor(255, 228, 187),
            )
        if self.tone == "auth_ok":
            return (
                QColor(32, 142, 91, int(48 + pulse * 68)),
                QColor(94, 234, 154, int(120 + pulse * 110)),
                QColor(220, 255, 236),
            )
        if self.tone == "auth_pending":
            return (
                QColor(142, 106, 32, int(48 + pulse * 68)),
                QColor(250, 204, 82, int(120 + pulse * 110)),
                QColor(255, 246, 203),
            )
        if self.tone == "auth_bad":
            return (
                QColor(145, 51, 77, int(48 + pulse * 68)),
                QColor(251, 113, 133, int(120 + pulse * 110)),
                QColor(255, 228, 233),
            )
        if self.tone == "auth_offline":
            return (
                QColor(47, 98, 152, int(48 + pulse * 68)),
                QColor(125, 211, 252, int(120 + pulse * 110)),
                QColor(225, 245, 255),
            )
        return (
            QColor(118, 80, 226, int(58 + pulse * 72)),
            QColor(191, 153, 255, int(132 + pulse * 105)),
            QColor(239, 231, 255),
        )

    def _draw_icon(self, painter, color):
        pen = QPen(color, 1.45)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if self.kind == "coffee":
            painter.drawRoundedRect(12, 13, 12, 8, 3, 3)
            painter.drawArc(21, 14, 8, 7, -70 * 16, 210 * 16)
            painter.drawLine(12, 24, 25, 24)
            painter.drawArc(13, 7, 8, 8, 70 * 16, 85 * 16)
            painter.drawArc(19, 7, 8, 8, 70 * 16, 85 * 16)
        elif self.kind == "auth":
            shield = QPainterPath()
            shield.moveTo(20, 8)
            shield.lineTo(29, 12)
            shield.lineTo(27, 23)
            shield.lineTo(20, 28)
            shield.lineTo(13, 23)
            shield.lineTo(11, 12)
            shield.closeSubpath()
            painter.drawPath(shield)
            painter.drawLine(16, 18, 19, 21)
            painter.drawLine(19, 21, 25, 15)
        else:
            painter.drawEllipse(11, 11, 7, 7)
            painter.drawEllipse(21, 11, 7, 7)
            painter.drawArc(8, 17, 14, 10, 25 * 16, 130 * 16)
            painter.drawArc(18, 17, 14, 10, 25 * 16, 130 * 16)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 4, -1, -4)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        bg, border, text = self._tone_colors()
        if self.isDown():
            bg.setAlpha(min(180, bg.alpha() + 48))
        elif self.underMouse():
            bg.setAlpha(min(170, bg.alpha() + 32))
            text = QColor(255, 255, 255)

        painter.fillPath(path, bg)
        outer = QColor(border)
        outer.setAlpha(max(70, int(border.alpha() * 0.72)))
        painter.setPen(QPen(outer, 2.0))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 11, 11)
        painter.setPen(QPen(border, 1.25))
        painter.drawPath(path)
        self._draw_icon(painter, text)

        painter.setPen(text)
        painter.setFont(QFont("Microsoft YaHei UI", 9 if self.has_close else 10, QFont.DemiBold))
        text_left = 34
        text_right = 27 if self.has_close else 8
        painter.drawText(text_left, 0, self.width() - text_left - text_right, self.height(), Qt.AlignVCenter | Qt.AlignLeft, self.label)

        if self.has_close:
            x, y, w, h = self._close_rect()
            close_bg = QColor(255, 255, 255, 42 if self._close_pressed else 24)
            painter.setPen(Qt.NoPen)
            painter.setBrush(close_bg)
            painter.drawEllipse(x, y, w, h)
            close_pen = QPen(QColor(255, 238, 216, 210), 1.2)
            close_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(close_pen)
            painter.drawLine(x + 4, y + 4, x + w - 4, y + h - 4)
            painter.drawLine(x + w - 4, y + 4, x + 4, y + h - 4)


class PulseDialogShell(QFrame):
    def __init__(self, tone="coffee", parent=None):
        super().__init__(parent)
        self.tone = tone
        self.glow_value = 0.0
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(2400 if tone == "coffee" else 2260)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._set_glow_value)
        self._animation.start()

    def _set_glow_value(self, value):
        self.glow_value = float(value)
        self.update()

    def _accent_colors(self):
        pulse = 0.35 + self.glow_value * 0.65
        if self.tone == "purple":
            return QColor(188, 145, 255, int(92 + pulse * 108)), QColor(72, 48, 128, int(30 + pulse * 42))
        return QColor(232, 176, 102, int(92 + pulse * 108)), QColor(101, 66, 36, int(30 + pulse * 42))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 28, 28)
        painter.fillPath(path, QColor(15, 23, 36, 248))

        accent, soft = self._accent_colors()
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, soft)
        gradient.setColorAt(0.46, QColor(12, 22, 34, 0))
        gradient.setColorAt(1.0, QColor(14, 126, 146, 24))
        painter.fillPath(path, gradient)

        outer = QColor(accent)
        outer.setAlpha(max(48, int(accent.alpha() * 0.45)))
        painter.setPen(QPen(outer, 2.0))
        painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 25, 25)
        painter.setPen(QPen(accent, 1.15))
        painter.drawPath(path)
        super().paintEvent(event)


class LogoImageCard(QFrame):
    def __init__(self, image_path=None, parent=None):
        super().__init__(parent)
        self.image_path = image_path or resource_path("logo.jpg")
        self.source_pixmap = QPixmap(self.image_path) if os.path.exists(self.image_path) else QPixmap()
        self.setFixedSize(254, 146)
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 28, 28)

        if not self.source_pixmap.isNull():
            scaled = self.source_pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            x = int((self.width() - scaled.width()) / 2)
            y = int((self.height() - scaled.height()) / 2)
            painter.setClipPath(path)
            painter.drawPixmap(x, y, scaled)
            painter.setClipping(False)
        else:
            fallback = QLinearGradient(rect.topLeft(), rect.bottomRight())
            fallback.setColorAt(0.0, QColor(29, 208, 214, 190))
            fallback.setColorAt(1.0, QColor(10, 45, 66, 220))
            painter.fillPath(path, fallback)

        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 32))
        sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.0, QColor(0, 0, 0, 28))
        painter.fillPath(path, sheen)

        painter.setPen(QPen(QColor(136, 230, 238, 72), 1))
        painter.drawPath(path)


class VersionUpdateButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.update_info = None
        self.is_checking = False
        self.glow_value = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(28)
        self.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
        self.setStyleSheet("QPushButton { background: transparent; border: none; outline: none; }")
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(950)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._set_glow_value)
        self.set_update_info(None)

    def set_update_info(self, update_info):
        self.update_info = update_info
        self.is_checking = False
        if update_info is None:
            self.setText(f"YHo AutoFish v{APP_VERSION} 检查更新")
            self.setToolTip("点击立即检查 GitHub Release 是否有新版本")
            self._animation.stop()
            self.glow_value = 0.0
        else:
            self.setText(f"YHo AutoFish v{APP_VERSION} 发现新版 v{update_info.version} 点击更新")
            self.setToolTip(f"发现新版 v{update_info.version}，点击打开更新界面")
            if self._animation.state() != QAbstractAnimation.Running:
                self._animation.start()
        self._fit_to_text()
        self.update()

    def set_checking(self, checking):
        if self.update_info is not None:
            return
        self.is_checking = bool(checking)
        if self.is_checking:
            self.setText(f"YHo AutoFish v{APP_VERSION} 正在检查...")
            self.setToolTip("正在检查 GitHub Release 最新版本")
        else:
            self.setText(f"YHo AutoFish v{APP_VERSION} 检查更新")
            self.setToolTip("点击立即检查 GitHub Release 是否有新版本")
        self._fit_to_text()
        self.update()

    def _fit_to_text(self):
        width = self.fontMetrics().horizontalAdvance(self.text()) + 28
        self.setMinimumWidth(max(154, width))

    def _set_glow_value(self, value):
        self.glow_value = float(value)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 2, -1, -2)
        path = QPainterPath()
        path.addRoundedRect(rect, 11, 11)

        if self.update_info is None:
            if self.is_checking:
                bg = QColor(29, 208, 214, 54)
                border = QColor(99, 228, 228, 145)
                text = QColor(210, 250, 252)
            else:
                bg = QColor(29, 208, 214, 34)
                border = QColor(99, 228, 228, 108)
                text = QColor(186, 241, 245)
                if self.isDown():
                    bg = QColor(29, 208, 214, 64)
                    border = QColor(99, 228, 228, 170)
                elif self.underMouse():
                    bg = QColor(29, 208, 214, 48)
                    border = QColor(99, 228, 228, 150)
                    text = QColor(226, 255, 255)
        else:
            ratio = 0.35 + self.glow_value * 0.65
            bg = QColor(241, 190, 103, int(42 + ratio * 56))
            border = QColor(241, 190, 103, int(110 + ratio * 115))
            text = QColor(255, 239, 181)
            if self.isDown():
                bg = QColor(241, 190, 103, 140)
            elif self.underMouse():
                text = QColor(255, 251, 225)

        painter.fillPath(path, bg)
        painter.setPen(QPen(border, 1.2))
        painter.drawPath(path)
        painter.setPen(text)
        painter.drawText(rect, Qt.AlignCenter, self.text())


class TitleBrand(QFrame):
    def __init__(self, window=None, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        mark = QFrame()
        mark.setFixedSize(5, 24)
        mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        mark.setStyleSheet(
            f"""
            QFrame {{
                background-color: {APP_COLORS['accent_soft']};
                border: none;
                border-radius: 2px;
            }}
            """
        )
        layout.addWidget(mark, 0, Qt.AlignVCenter)

        title = QLabel(
            "<span style='color:#63E4E4;'>异环</span>"
            "<span style='color:#F3F8FF;'>自动钓鱼</span>"
        )
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        title.setStyleSheet(
            """
            QLabel {
                background: transparent;
                border: none;
                font-family: 'Microsoft YaHei UI';
                font-size: 15px;
                font-weight: 900;
            }
            """
        )
        layout.addWidget(title, 0, Qt.AlignVCenter)

        self.version_button = VersionUpdateButton()
        self.version_button.clicked.connect(self._handle_version_clicked)
        layout.addWidget(self.version_button, 0, Qt.AlignVCenter)

    def set_update_info(self, update_info):
        self.version_button.set_update_info(update_info)

    def set_update_checking(self, checking):
        self.version_button.set_checking(checking)

    def _handle_version_clicked(self):
        if self.window_ref is not None and hasattr(self.window_ref, "show_update_dialog"):
            self.window_ref.show_update_dialog()

    def mousePressEvent(self, event):
        if self.window_ref is not None and event.button() == Qt.LeftButton:
            title_bar = getattr(self.window_ref, "title_bar", None)
            if title_bar is not None:
                title_bar.dragging = True
                title_bar.drag_pos = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        title_bar = getattr(self.window_ref, "title_bar", None) if self.window_ref is not None else None
        if title_bar is not None and title_bar.dragging and not self.window_ref.isMaximized():
            self.window_ref.move(event.globalPosition().toPoint() - title_bar.drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        title_bar = getattr(self.window_ref, "title_bar", None) if self.window_ref is not None else None
        if title_bar is not None:
            title_bar.dragging = False
        super().mouseReleaseEvent(event)


class CustomTitleBar(QFrame):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.drag_pos = QPoint()
        self.dragging = False
        self.setFixedHeight(56)
        self.setStyleSheet(
            """
            QFrame {
                background-color: rgba(15, 27, 43, 0.86);
                border-top-left-radius: 32px;
                border-top-right-radius: 32px;
                border: 1px solid rgba(74, 107, 141, 0.22);
                border-bottom: none;
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 14, 10)
        layout.setSpacing(10)

        self.title_brand = TitleBrand(window)
        layout.addWidget(self.title_brand)
        layout.addStretch()

        self.btn_about = TitleButton("about", "rgba(29, 208, 214, 0.18)")
        self.btn_coffee = PulseTitleActionButton("coffee", "请作者喝一点咖啡吗？", "coffee", has_close=True)
        self.btn_qq = PulseTitleActionButton("qq", "Q群", "purple")
        self.btn_auth = PulseTitleActionButton("auth", "未授权", "auth_bad", auto_width=True, min_width=92)
        self.btn_min = TitleButton("min", "rgba(90, 129, 166, 0.22)")
        self.btn_max = TitleButton("max", "rgba(90, 129, 166, 0.22)")
        self.btn_close = TitleButton("close", "rgba(255, 102, 126, 0.58)")

        self.btn_about.setToolTip("关于")
        self.btn_coffee.setToolTip("请作者喝一点咖啡吗？")
        self.btn_qq.setToolTip("加入 QQ 群")
        self.btn_auth.setToolTip("查看来源验证状态")
        self.btn_min.clicked.connect(self.window_ref.showMinimized)
        self.btn_max.clicked.connect(self.window_ref.toggle_maximize_restore)
        self.btn_close.clicked.connect(self.window_ref.close)
        self.btn_about.clicked.connect(self.window_ref.show_about_dialog)
        self.btn_coffee.clicked.connect(self.window_ref.show_sponsor_dialog)
        self.btn_coffee.closeRequested.connect(self.window_ref.confirm_hide_sponsor_button)
        self.btn_qq.clicked.connect(self.window_ref.show_qq_group_dialog)
        self.btn_auth.clicked.connect(self.window_ref.show_authorization_status)

        layout.addWidget(self.btn_coffee)
        layout.addWidget(self.btn_qq)
        layout.addWidget(self.btn_auth)
        layout.addWidget(self.btn_about)
        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_max)
        layout.addWidget(self.btn_close)
        self.sync_sponsor_visibility()
        self.sync_auth_status()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.window_ref.toggle_maximize_restore()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_pos = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.dragging and not self.window_ref.isMaximized():
            self.window_ref.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.dragging = False
        super().mouseReleaseEvent(event)

    def sync_state(self):
        self.btn_max.set_kind("restore" if self.window_ref.isMaximized() else "max")
        self.sync_sponsor_visibility()
        self.sync_auth_status()

    def sync_sponsor_visibility(self):
        hidden = bool(getattr(self.window_ref, "config", {}).get("sponsor_button_hidden", False))
        self.btn_coffee.setVisible(not hidden)

    def sync_auth_status(self):
        if hasattr(self.window_ref, "_auth_title_status"):
            text, tone, tooltip = self.window_ref._auth_title_status()
        else:
            text, tone, tooltip = "未授权", "auth_bad", "查看来源验证状态"
        self.btn_auth.set_label(text, tone=tone, tooltip=tooltip)


class StatusChip(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(36)
        self.set_status("待机中", "idle")

    def set_status(self, text, tone="idle"):
        color = {
            "idle": APP_COLORS["text_dim"],
            "running": APP_COLORS["accent_soft"],
            "stopped": APP_COLORS["danger"],
        }.get(tone, APP_COLORS["text_dim"])
        self.setText(text)
        self.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 18px;
                color: {color};
                padding: 0 12px;
                font-size: 12px;
                font-weight: 800;
            }}
            """
        )


class NoWheelSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


class RecognitionInitWorker(QThread):
    completed = Signal(bool, str)
    progress = Signal(str)

    def __init__(self, state_machine, parent=None):
        super().__init__(parent)
        self.state_machine = state_machine

    def run(self):
        try:
            ok = self.state_machine.prepare_recognition_modules(
                progress_fn=lambda msg: self.progress.emit(msg)
            )
            if self.isInterruptionRequested():
                return
            if ok:
                self.completed.emit(True, "识别模块初始化完成，可以开始钓鱼。")
            else:
                self.completed.emit(False, self.state_machine.get_ocr_init_failure_message())
        except Exception as exc:
            if self.isInterruptionRequested():
                return
            detail = self.state_machine.get_ocr_init_failure_message()
            self.completed.emit(False, f"{detail} 原始异常: {exc}")


class UpdateCheckWorker(QThread):
    completed = Signal(object, str)

    def run(self):
        try:
            result = check_for_update(timeout=6)
            if not self.isInterruptionRequested():
                self.completed.emit(result, "")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(None, str(exc))


class UpdateDownloadWorker(QThread):
    progress = Signal(int)
    completed = Signal(bool, str, str)

    def __init__(self, update_info, source="github", parent=None):
        super().__init__(parent)
        self.update_info = update_info
        self.source = source
        self._user_cancel_requested = False

    def cancel_download(self):
        self._user_cancel_requested = True
        self.requestInterruption()

    def run(self):
        cancel_message = "更新下载已取消。"
        try:
            def report_progress(percent, downloaded, total):
                if self.isInterruptionRequested():
                    raise DownloadCancelled(cancel_message)
                self.progress.emit(int(percent))

            path = download_update(
                self.update_info,
                progress_callback=report_progress,
                source=self.source,
                cancel_callback=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested() or self._user_cancel_requested:
                self.completed.emit(False, "", cancel_message)
            else:
                self.completed.emit(True, path, "")
        except DownloadCancelled as exc:
            self.completed.emit(False, "", str(exc) or cancel_message)
        except Exception as exc:
            if self._user_cancel_requested or self.isInterruptionRequested():
                self.completed.emit(False, "", cancel_message)
            else:
                self.completed.emit(False, "", str(exc))


class StyledMessageDialog(QDialog):
    """Themed message dialog matching app design language."""

    def __init__(self, title, message, tone="info", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(440, 220)

        border_color = {
            "info": APP_COLORS['accent'],
            "success": APP_COLORS['success'],
            "warning": APP_COLORS['warning'],
            "danger": APP_COLORS['danger'],
        }.get(tone, APP_COLORS['accent'])

        title_color = border_color

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.98);
                border: 1px solid {border_color};
                border-radius: 28px;
            }}
        """)
        add_shadow(shell, blur=32, alpha=120, offset=(0, 10))
        outer.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"background:transparent; border:none; color:{title_color}; font-size:17px; font-weight:900;")
        layout.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"background:transparent; border:none; color:{APP_COLORS['text']}; font-size:13px;")
        layout.addWidget(msg_lbl, 1)

        ok_btn = QPushButton("确定")
        ok_btn.setStyleSheet(primary_button_stylesheet())
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn, 0, Qt.AlignRight)


class ForceUpdateDialog(QDialog):
    """Forced update dialog shown when server requires minimum version."""

    update_started = Signal()

    def __init__(self, min_version, update_url="", changelog="", parent=None):
        super().__init__(parent)
        self._min_version = min_version
        self._update_url = update_url
        self._dismissed = False
        self._update_started = False
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(520, 320)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.98);
                border: 1px solid rgba(241, 190, 103, 0.42);
                border-radius: 30px;
            }}
        """)
        add_shadow(shell, blur=38, alpha=135, offset=(0, 14))
        outer.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel(f"⚠ 版本过低，需更新至 v{min_version} 或更高")
        title.setWordWrap(True)
        title.setStyleSheet(f"background:transparent; border:none; color:{APP_COLORS['warning']}; font-size:17px; font-weight:900;")
        layout.addWidget(title)

        if changelog:
            log_text = QPlainTextEdit(changelog)
            log_text.setReadOnly(True)
            log_text.setMaximumHeight(80)
            log_text.setStyleSheet(f"""
                QPlainTextEdit {{
                    background-color: {APP_COLORS['panel']};
                    color: {APP_COLORS['text']};
                    border: 1px solid {APP_COLORS['stroke']};
                    border-radius: 12px;
                    padding: 10px;
                    font-family: Consolas, 'Microsoft YaHei UI';
                    font-size: 12px;
                }}
            """)
            layout.addWidget(log_text)

        hint = QLabel("点击「立即更新」自动下载安装，或点击「退出」关闭程序。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"background:transparent; border:none; color:{APP_COLORS['text_dim']}; font-size:13px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self.update_btn = QPushButton("立即更新")
        self.update_btn.setStyleSheet(primary_button_stylesheet())
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(self._on_update)
        btn_row.addWidget(self.update_btn)

        self.exit_btn = QPushButton("退出程序")
        self.exit_btn.setStyleSheet(secondary_button_stylesheet())
        self.exit_btn.setCursor(Qt.PointingHandCursor)
        self.exit_btn.clicked.connect(self._on_exit)
        btn_row.addWidget(self.exit_btn)
        layout.addLayout(btn_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {APP_COLORS['panel']};
                border: 1px solid {APP_COLORS['stroke']};
                border-radius: 8px; height: 16px;
                text-align: center; color: {APP_COLORS['text']};
                font-size: 11px;
            }}
            QProgressBar::chunk {{
                background-color: {APP_COLORS['accent']};
                border-radius: 7px;
            }}
        """)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        self.status_label.setStyleSheet(f"background:transparent; border:none; color:{APP_COLORS['text_dim']}; font-size:12px;")
        layout.addWidget(self.status_label)

        self._worker = None

    def dismiss_dialog(self):
        """Called when server disables force update -- close dialog gracefully."""
        self._dismissed = True
        self.done(QDialog.Accepted)

    def closeEvent(self, event):
        if self._dismissed:
            event.accept()
            return
        reply = QMessageBox.question(
            self, "确认退出",
            "不更新将无法使用本工具。确定要退出程序吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            if self.parent():
                self.parent().close()
            else:
                QApplication.instance().quit()
            event.accept()
        else:
            event.ignore()

    def _on_exit(self):
        reply = QMessageBox.question(
            self, "确认退出",
            "确定要退出程序吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            if self.parent():
                self.parent().close()
            else:
                QApplication.instance().quit()

    def _on_update(self):
        self.update_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setVisible(True)
        self.status_label.setText("正在检查更新源...")
        self.update_started.emit()
        try:
            from core.updater import UpdateInfo, check_for_update
            if self._update_url:
                info = UpdateInfo(version="", url=self._update_url, asset_name="", body="")
                source = "direct"
            else:
                info = check_for_update(timeout=10)
                if not info:
                    self.status_label.setText("未获取到更新包，请手动前往发布页下载。")
                    self.update_btn.setEnabled(True)
                    self.progress_bar.setVisible(False)
                    return
                source = "github"
            self.status_label.setText("正在下载更新...")
            self._worker = UpdateDownloadWorker(info, source=source, parent=self)
            self._worker.progress.connect(self._on_progress)
            self._worker.completed.connect(self._on_completed)
            self._worker.start()
        except Exception as exc:
            self.status_label.setText(f"下载失败: {exc}")
            self.update_btn.setEnabled(True)
            self.progress_bar.setVisible(False)

    def _on_progress(self, percent):
        self.progress_bar.setValue(percent)

    def _on_completed(self, ok, path, error):
        if ok and path:
            self.status_label.setText("下载完成，正在启动安装...")
            import subprocess
            subprocess.Popen([path])
        else:
            self.status_label.setText(f"下载失败: {error}")
            self.update_btn.setEnabled(True)
            self.progress_bar.setVisible(False)


class AuthCheckWorker(QThread):
    completed = Signal(bool, object, str, bool, object)

    def __init__(self, base_url, state, device_hash, device_hash_v2="", parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.state = state
        self.device_hash = device_hash
        self.device_hash_v2 = device_hash_v2

    def run(self):
        if not self.state.access_token:
            self.completed.emit(False, self.state, "尚未绑定授权", False)
            return
        try:
            result = AuthClient(self.base_url, timeout=8).check_entitlement(self.state.access_token, self.device_hash, device_hash_v2=self.device_hash_v2)
            if self.isInterruptionRequested():
                return
            if result.get("authorized"):
                server_time = float(result.get("server_time") or time.time())
                updated = AuthState(
                    status="authorized",
                    access_token=self.state.access_token,
                    license_id=str(result.get("license_id") or self.state.license_id or ""),
                    device_hash=self.device_hash,
                    device_hash_v2=self.device_hash_v2,
                    qq_user_id_hash=str(result.get("qq_user_id_hash") or self.state.qq_user_id_hash or ""),
                    expires_at=float(result.get("expires_at") or self.state.expires_at or 0),
                    activation_id=self.state.activation_id,
                    user_code=str(result.get("user_code") or self.state.user_code or ""),
                    message="授权有效",
                    refresh_token=self.state.refresh_token,
                    refresh_expires_at=self.state.refresh_expires_at,
                    license_data=self.state.license_data,
                    license_expires_at=float(result.get("license_expires_at") or self.state.license_expires_at or 0),
                )
                updated.apply_check_timing(server_time)
                self.completed.emit(True, updated, "授权有效", False, result)
                return
            updated = AuthState.from_dict(self.state.to_dict())
            # Try refresh_token if available and not expired
            if self.state.refresh_token and (not self.state.refresh_expires_at or time.time() < self.state.refresh_expires_at):
                try:
                    refresh_result = AuthClient(self.base_url, timeout=8).refresh_access_token(
                        self.state.refresh_token,
                        device_hash_v2=self.device_hash_v2,
                        device_hash=self.device_hash,
                    )
                    if refresh_result.get("status") == "authorized" and refresh_result.get("access_token"):
                        new_token = refresh_result["access_token"]
                        retry_result = AuthClient(self.base_url, timeout=8).check_entitlement(new_token, self.device_hash, device_hash_v2=self.device_hash_v2)
                        if retry_result.get("authorized"):
                            server_time = float(retry_result.get("server_time") or time.time())
                            updated = AuthState(
                                status="authorized",
                                access_token=new_token,
                                license_id=str(retry_result.get("license_id") or self.state.license_id or ""),
                                device_hash=self.device_hash,
                                device_hash_v2=self.device_hash_v2,
                                qq_user_id_hash=str(retry_result.get("qq_user_id_hash") or self.state.qq_user_id_hash or ""),
                                expires_at=float(retry_result.get("expires_at") or 0),
                                activation_id=self.state.activation_id,
                                user_code=str(retry_result.get("user_code") or self.state.user_code or ""),
                                message="授权有效（token 已刷新）",
                                refresh_token=str(refresh_result.get("refresh_token") or self.state.refresh_token),
                                refresh_expires_at=float(refresh_result.get("refresh_expires_at") or self.state.refresh_expires_at or 0),
                                license_data=self.state.license_data,
                                license_expires_at=float(retry_result.get("license_expires_at") or self.state.license_expires_at or 0),
                            )
                            updated.apply_check_timing(server_time)
                            # V1→V2 migration: detect JWT in refresh response
                            if refresh_result.get("jwt_token"):
                                updated.jwt_token = str(refresh_result["jwt_token"])
                                updated.jwt_expires_at = float(refresh_result.get("jwt_expires_at", 0))
                                updated.jwt_issued_monotonic = time.monotonic()
                                updated.protocol_version = 2
                            self.completed.emit(True, updated, "授权有效（token 已刷新）", False, retry_result)
                            return
                except Exception as refresh_exc:
                    print(f"[auth] refresh_token 恢复失败: {refresh_exc}")
            # Step 3: Try reactivate if we have qq_user_id_hash (30+ day gap recovery)
            if self.state.qq_user_id_hash:
                try:
                    reactivate_result = AuthClient(self.base_url, timeout=8).reactivate_entitlement(
                        self.state.qq_user_id_hash, self.device_hash, device_hash_v2=self.device_hash_v2)
                    if reactivate_result.get("status") == "authorized" and reactivate_result.get("access_token"):
                        new_token = reactivate_result["access_token"]
                        retry_result = AuthClient(self.base_url, timeout=8).check_entitlement(new_token, self.device_hash, device_hash_v2=self.device_hash_v2)
                        if retry_result.get("authorized"):
                            server_time = float(retry_result.get("server_time") or time.time())
                            updated = AuthState(
                                status="authorized",
                                access_token=new_token,
                                license_id=str(retry_result.get("license_id") or self.state.license_id or ""),
                                device_hash=self.device_hash,
                                device_hash_v2=self.device_hash_v2,
                                qq_user_id_hash=str(retry_result.get("qq_user_id_hash") or self.state.qq_user_id_hash or ""),
                                expires_at=float(retry_result.get("expires_at") or 0),
                                activation_id=self.state.activation_id,
                                user_code=str(retry_result.get("user_code") or self.state.user_code or ""),
                                message="授权有效（已自动恢复）",
                                refresh_token=str(reactivate_result.get("refresh_token") or ""),
                                refresh_expires_at=float(reactivate_result.get("refresh_expires_at") or 0),
                                license_data=self.state.license_data,
                                license_expires_at=float(retry_result.get("license_expires_at") or self.state.license_expires_at or 0),
                            )
                            updated.apply_check_timing(server_time)
                            self.completed.emit(True, updated, "授权有效（已自动恢复）", False, retry_result)
                            return
                except Exception as reactivate_exc:
                    print(f"[auth] reactivate 恢复失败: {reactivate_exc}")
            recovery = classify_auth_recovery(self.state, result, network_error=False)
            if recovery.should_persist:
                updated.status = recovery.status
            updated.message = recovery.message or str(result.get("message") or "授权校验未通过")
            self.completed.emit(False, updated, updated.message, False, result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(False, self.state, f"授权服务器连接失败: {exc}", True, {})


class AuthCheckWorkerV2(QThread):
    """V2 auth check worker with 3-step recovery using JWT-based endpoints."""

    completed = Signal(bool, object, str, bool, object)

    def __init__(self, base_url, state, device_hash, device_hash_v2="", parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.state = state
        self.device_hash = device_hash
        self.device_hash_v2 = device_hash_v2

    def _build_authorized_state(self, result, jwt_token, refresh_token="", refresh_expires_at=0.0):
        server_time = float(result.get("server_time") or time.time())
        # Use fresh JWT from server response if available (auto-renewal)
        fresh_jwt = str(result.get("jwt_token") or jwt_token or "")
        fresh_exp = float(result.get("jwt_expires_at") or result.get("expires_at") or 0)
        updated = AuthState(
            status="authorized",
            access_token=fresh_jwt,
            license_id=str(result.get("license_id") or self.state.license_id or ""),
            device_hash=self.device_hash,
            device_hash_v2=self.device_hash_v2,
            qq_user_id_hash=str(result.get("qq_user_id_hash") or self.state.qq_user_id_hash or ""),
            expires_at=fresh_exp,
            activation_id=self.state.activation_id,
            user_code=str(result.get("user_code") or self.state.user_code or ""),
            message="授权有效",
            refresh_token=str(refresh_token or self.state.refresh_token or ""),
            refresh_expires_at=float(refresh_expires_at or self.state.refresh_expires_at or 0),
            license_data=self.state.license_data,
            license_expires_at=float(result.get("license_expires_at") or self.state.license_expires_at or 0),
            jwt_token=fresh_jwt,
            jwt_expires_at=fresh_exp,
            jwt_issued_monotonic=time.monotonic(),
            protocol_version=2,
        )
        updated.apply_check_timing(server_time)
        return updated

    def run(self):
        if not self.state.jwt_token:
            self.completed.emit(False, self.state, "V2 协议缺少 JWT", False, {})
            return
        try:
            # Step 1: check_entitlement_v2
            result = AuthClient(self.base_url, timeout=8).check_entitlement_v2(
                self.state.jwt_token, self.device_hash, device_hash_v2=self.device_hash_v2)
            if self.isInterruptionRequested():
                return
            if result.get("authorized"):
                updated = self._build_authorized_state(result, self.state.jwt_token)
                self.completed.emit(True, updated, "授权有效", False, result)
                return

            # Step 2: refresh_token_v2
            if self.state.refresh_token and (not self.state.refresh_expires_at or time.time() < self.state.refresh_expires_at):
                try:
                    refresh_result = AuthClient(self.base_url, timeout=8).refresh_token_v2(
                        self.state.refresh_token, device_hash_v2=self.device_hash_v2, device_hash=self.device_hash)
                    if self.isInterruptionRequested():
                        return
                    refresh_status = refresh_result.get("status", "")
                    has_access = bool(refresh_result.get("access_token"))
                    print(f"[auth-v2] refresh result: status={refresh_status}, has_access_token={has_access}")
                    if refresh_status == "authorized" and has_access:
                        new_jwt = refresh_result["access_token"]
                        retry_result = AuthClient(self.base_url, timeout=8).check_entitlement_v2(
                            new_jwt, self.device_hash, device_hash_v2=self.device_hash_v2)
                        if retry_result.get("authorized"):
                            updated = self._build_authorized_state(
                                retry_result, new_jwt,
                                refresh_token=str(refresh_result.get("refresh_token") or ""),
                                refresh_expires_at=float(refresh_result.get("refresh_expires_at") or 0))
                            updated.message = "授权有效（token 已刷新）"
                            self.completed.emit(True, updated, "授权有效（token 已刷新）", False, retry_result)
                            return
                    else:
                        print(f"[auth-v2] refresh 失败: status={refresh_status}, msg={refresh_result.get('message', '')}")
                except Exception as refresh_exc:
                    print(f"[auth-v2] refresh_token_v2 恢复异常: {refresh_exc}")

            # Step 3: reactivate_entitlement_v2
            if self.state.qq_user_id_hash:
                try:
                    reactivate_result = AuthClient(self.base_url, timeout=8).reactivate_entitlement_v2(
                        self.state.qq_user_id_hash, self.device_hash, device_hash_v2=self.device_hash_v2)
                    if self.isInterruptionRequested():
                        return
                    if reactivate_result.get("status") == "authorized" and reactivate_result.get("access_token"):
                        new_jwt = reactivate_result["access_token"]
                        retry_result = AuthClient(self.base_url, timeout=8).check_entitlement_v2(
                            new_jwt, self.device_hash, device_hash_v2=self.device_hash_v2)
                        if retry_result.get("authorized"):
                            updated = self._build_authorized_state(
                                retry_result, new_jwt,
                                refresh_token=str(reactivate_result.get("refresh_token") or ""),
                                refresh_expires_at=float(reactivate_result.get("refresh_expires_at") or 0))
                            updated.message = "授权有效（已自动恢复）"
                            self.completed.emit(True, updated, "授权有效（已自动恢复）", False, retry_result)
                            return
                except Exception as reactivate_exc:
                    print(f"[auth-v2] reactivate_v2 恢复失败: {reactivate_exc}")

            # All recovery steps failed
            recovery = classify_auth_recovery(self.state, result, network_error=False)
            updated = AuthState.from_dict(self.state.to_dict())
            if recovery.should_persist:
                updated.status = recovery.status
            updated.message = recovery.message or str(result.get("message") or "授权校验未通过")
            self.completed.emit(False, updated, updated.message, False, result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(False, self.state, f"授权服务器连接失败: {exc}", True, {})


class AuthActivationWorker(QThread):
    completed = Signal(str, bool, object, str)

    def __init__(self, action, base_url, device_hash, install_id, app_version, activation_id="", device_hash_v2="", parent=None):
        super().__init__(parent)
        self.action = action
        self.base_url = base_url
        self.device_hash = device_hash
        self.install_id = install_id
        self.app_version = app_version
        self.activation_id = activation_id
        self.device_hash_v2 = device_hash_v2

    def run(self):
        try:
            client = AuthClient(self.base_url, timeout=8)
            if self.action == "start":
                data = client.start_activation(self.device_hash, self.install_id, self.app_version, device_hash_v2=self.device_hash_v2)
            else:
                data = client.poll_activation(self.activation_id, self.device_hash)
            if not self.isInterruptionRequested():
                self.completed.emit(self.action, True, data, "")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(self.action, False, {}, str(exc))


class AuthPublicGroupsWorker(QThread):
    completed = Signal(bool, object, str)

    def __init__(self, base_url, parent=None):
        super().__init__(parent)
        self.base_url = base_url

    def run(self):
        try:
            data = AuthClient(self.base_url, timeout=8).list_public_groups()
            if not self.isInterruptionRequested():
                self.completed.emit(True, data, "")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(False, {}, str(exc))


class AuthorizationDialog(QDialog):
    def __init__(self, app_window):
        super().__init__(app_window)
        self.app_window = app_window
        self.worker = None
        self.group_worker = None
        self.current_user_code = ""
        self._bound_mode = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2500)
        self.poll_timer.timeout.connect(self.poll_activation)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(720, 610)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.98);
                border: 1px solid rgba(89, 125, 164, 0.32);
                border-radius: 26px;
            }}
            """
        )
        add_shadow(shell, blur=34, alpha=120, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)

        title = QLabel("来源验证")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 28px; font-weight: 900;"
        )
        layout.addWidget(title)

        note = QLabel("当前程序需要验证用户来自指定 QQ 群。请生成绑定码，并在指定群内发送 /bind 绑定码。群内机器人会协助完成绑定，发送 /yho help 可查看菜单。")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; line-height: 1.6;"
        )
        layout.addWidget(note)

        self.binding_status_card = QLabel(self._binding_status_text())
        self.binding_status_card.setWordWrap(True)
        self.binding_status_card.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(29, 208, 214, 0.075);
                border: 1px solid rgba(29, 208, 214, 0.22);
                border-radius: 14px;
                color: {APP_COLORS['text_soft']};
                padding: 11px 13px;
                font-size: 12px;
                line-height: 1.5;
            }}
            """
        )
        layout.addWidget(self.binding_status_card)

        self.code_label = QLabel("尚未生成绑定码")
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(29, 208, 214, 0.32);
                border-radius: 18px;
                color: {APP_COLORS['accent_soft']};
                font-size: 26px;
                font-weight: 900;
                padding: 18px;
            }}
            """
        )
        layout.addWidget(self.code_label)

        self.instruction_label = QLabel("点击“生成绑定码”后，将显示需要发送到 QQ 群的命令。")
        self.instruction_label.setWordWrap(True)
        self.instruction_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        layout.addWidget(self.instruction_label)

        self.status_label = QLabel("等待操作")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(self.status_label)

        group_hint = QLabel("可加入的官方 QQ 群")
        group_hint.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 13px; font-weight: 900;"
        )
        layout.addWidget(group_hint)

        self.group_scroll = QScrollArea()
        self.group_scroll.setFrameShape(QFrame.NoFrame)
        self.group_scroll.setWidgetResizable(True)
        self.group_scroll.setFixedHeight(120)
        self.group_scroll.setStyleSheet(scroll_area_stylesheet())
        self.group_scroll.verticalScrollBar().setStyleSheet(scrollbar_stylesheet(compact=True))
        self.group_container = QWidget()
        self.group_layout = QVBoxLayout(self.group_container)
        self.group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_layout.setSpacing(8)
        self.group_scroll.setWidget(self.group_container)
        layout.addWidget(self.group_scroll)
        self._render_groups([], "正在加载群列表...")

        device_hint = QLabel("设备管理: 在 QQ 群发送 /yho devices 查看已绑定设备")
        device_hint.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 11px;"
        )
        layout.addWidget(device_hint)

        actions = QHBoxLayout()
        actions.addStretch()
        self.copy_button = QPushButton("复制绑定码")
        self.copy_button.setFocusPolicy(Qt.NoFocus)
        self.copy_button.setEnabled(False)
        self.copy_button.setStyleSheet(secondary_button_stylesheet())
        self.copy_button.clicked.connect(self.copy_user_code)
        actions.addWidget(self.copy_button)

        self.start_button = QPushButton("生成绑定码")
        self.start_button.setFocusPolicy(Qt.NoFocus)
        self.start_button.setStyleSheet(primary_button_stylesheet())
        self.start_button.clicked.connect(self.start_activation)
        self.generate_button = self.start_button
        actions.addWidget(self.generate_button)

        self.recheck_button = QPushButton("立即复验")
        self.recheck_button.setFocusPolicy(Qt.NoFocus)
        self.recheck_button.setStyleSheet(secondary_button_stylesheet())
        self.recheck_button.clicked.connect(self.recheck_authorization)
        actions.addWidget(self.recheck_button)

        self.close_button = QPushButton("稍后验证")
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setStyleSheet(secondary_button_stylesheet())
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.refresh_auth_state_view()
        cached = getattr(self.app_window, '_cached_groups', None)
        cached_at = getattr(self.app_window, '_cached_groups_at', 0)
        if cached and (time.time() - cached_at < 300):
            self._render_groups(cached, "")
        else:
            self.refresh_groups()

    def closeEvent(self, event):
        self.poll_timer.stop()
        self._stop_worker()
        self._stop_group_worker()
        event.ignore()
        self.hide()

    def reject(self):
        self.poll_timer.stop()
        self._stop_worker()
        self._stop_group_worker()
        if hasattr(self.app_window, "mark_authorization_dialog_dismissed"):
            self.app_window.mark_authorization_dialog_dismissed()
        self.hide()

    def _stop_worker(self):
        worker = self.worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.finished.connect(lambda w=worker: w.deleteLater())
        except RuntimeError:
            pass
        self.worker = None

    def _stop_group_worker(self):
        worker = self.group_worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.finished.connect(lambda w=worker: w.deleteLater())
        except RuntimeError:
            pass
        self.group_worker = None

    def _set_busy(self, busy):
        if getattr(self, "_bound_mode", False):
            self.generate_button.setEnabled(False)
            self.recheck_button.setEnabled(False)
            return
        self.generate_button.setEnabled(not busy and self.generate_button.isVisible())
        self.recheck_button.setEnabled(not busy and self.recheck_button.isVisible())
        self.close_button.setEnabled(True)

    def _binding_status_text(self):
        state = self.app_window.auth_state
        title_text, _tone, tooltip = self.app_window._auth_title_status()
        code = str(getattr(state, "user_code", "") or "").strip()
        if self.app_window._auth_decision().allowed:
            code_part = f"已绑定的绑定码：{code}。" if code else "正在从服务器同步绑定码。"
        else:
            code_part = f"当前绑定码：{code}。" if code else "当前没有待绑定的绑定码。"
        return f"绑定状态：{title_text}。{code_part}在线复验固定 5 分钟一次，离线宽限 14 天。{tooltip}"

    def _refresh_binding_status_card(self):
        if hasattr(self, "binding_status_card"):
            self.binding_status_card.setText(self._binding_status_text())

    def refresh_auth_state_view(self):
        state = self.app_window.auth_state
        code = str(getattr(state, "user_code", "") or "").strip()
        activation_id = str(getattr(state, "activation_id", "") or "").strip()
        recovery = classify_auth_recovery(state)
        if self.app_window._auth_decision().allowed:
            # If allowed but no access_token (license-only fallback), show rebind option
            if not state.access_token and not state.jwt_token:
                recovery = classify_auth_recovery(state)
                if recovery.mode == "can_rebind":
                    self.poll_timer.stop()
                    self._bound_mode = False
                    self.generate_button.setVisible(True)
                    self.generate_button.setText("生成新绑定码")
                    self.generate_button.setEnabled(True)
                    self.recheck_button.setVisible(False)
                    self.code_label.setText("离线授权有效，但需要联网绑定以启用完整功能。")
                    self.instruction_label.setText("请点击下方按钮生成绑定码，在 QQ 群内完成绑定。")
                    self.status_label.setText("离线授权有效")
                    self.close_button.setText("稍后处理")
                    self._refresh_binding_status_card()
                    return
            self._show_bound_code(code)
            return
        if recovery.mode == "pending_activation" and activation_id and code:
            self._show_code(code, activation_id)
            self.poll_timer.start()
            return
        if recovery.mode in ("recheck_only", "transient_error"):
            self.poll_timer.stop()
            self._bound_mode = False
            self.current_user_code = code
            self.activation_id = ""
            display_code = code or "正在从服务器同步..."
            self.code_label.setText(f"已绑定的绑定码：{display_code}")
            self.instruction_label.setText("授权缓存已超过离线宽限或服务器暂时不可达，请联网后点击“立即复验”。")
            self.status_label.setText(self.app_window._auth_recovery_display_message(recovery, state))
            self.copy_button.setEnabled(bool(code))
            self.generate_button.setVisible(False)
            self.generate_button.setEnabled(False)
            self.recheck_button.setVisible(True)
            self.recheck_button.setText("立即复验")
            self.recheck_button.setToolTip("使用本机已有授权令牌立即请求服务器复验。")
            self.recheck_button.setEnabled(bool(state.access_token))
            self.close_button.setText("稍后验证")
            self._refresh_binding_status_card()
            return
        if recovery.mode in ("can_rebind", "device_mismatch"):
            self.poll_timer.stop()
            self._bound_mode = False
            self.current_user_code = code
            self.activation_id = ""
            display_code = code or "旧授权不可用"
            self.code_label.setText(f"已绑定的绑定码：{display_code}")
            self.instruction_label.setText(recovery.message)
            self.status_label.setText(self.app_window._auth_recovery_display_message(recovery, state))
            self.copy_button.setEnabled(bool(code))
            self.generate_button.setVisible(True)
            self.generate_button.setText("生成新绑定码")
            self.generate_button.setToolTip("生成新的绑定码，在 QQ 群内重新绑定本机。")
            self.generate_button.setEnabled(True)
            self.recheck_button.setVisible(bool(state.access_token))
            self.recheck_button.setEnabled(bool(state.access_token))
            self.recheck_button.setText("立即复验")
            self.recheck_button.setToolTip("再次使用本机旧授权令牌请求服务器复验。")
            self.close_button.setText("稍后处理")
            self._refresh_binding_status_card()
            return
        if recovery.mode == "admin_blocked":
            self.poll_timer.stop()
            self._bound_mode = False
            self.current_user_code = code
            self.activation_id = ""
            self.code_label.setText("授权已停用")
            self.instruction_label.setText(recovery.message)
            self.status_label.setText(self.app_window._auth_recovery_display_message(recovery, state))
            self.copy_button.setEnabled(False)
            self.generate_button.setVisible(False)
            self.generate_button.setEnabled(False)
            self.recheck_button.setVisible(False)
            self.recheck_button.setEnabled(False)
            self.close_button.setText("关闭")
            self._refresh_binding_status_card()
            return
        if activation_id and code:
            self._show_code(code, activation_id)
            self.poll_timer.start()
            return
        self._bound_mode = False
        self.current_user_code = ""
        self.activation_id = ""
        self.code_label.setText("尚未生成绑定码")
        self.instruction_label.setText("点击“生成绑定码”后，将显示需要发送到 QQ 群的命令。")
        self.status_label.setText("等待操作")
        self.copy_button.setEnabled(False)
        self.generate_button.setVisible(True)
        self.generate_button.setText("生成绑定码")
        self.generate_button.setToolTip("")
        self.generate_button.setEnabled(True)
        self.recheck_button.setVisible(False)
        self.recheck_button.setEnabled(False)
        self.close_button.setText("稍后验证")
        self._refresh_binding_status_card()

    def _show_code(self, code, activation_id):
        self._bound_mode = False
        self.current_user_code = str(code or "")
        self.code_label.setText(code)
        self.instruction_label.setText(f"请在指定 QQ 群发送：/bind {code}。如不清楚机器人菜单，可在群里发送 /yho help。")
        self.status_label.setText("已生成绑定码，正在等待 QQ 群验证。")
        self.activation_id = activation_id
        self.generate_button.setVisible(True)
        self.generate_button.setText("重新生成绑定码")
        self.generate_button.setToolTip("放弃当前绑定码并重新生成。")
        self.generate_button.setEnabled(True)
        self.recheck_button.setVisible(False)
        self.recheck_button.setEnabled(False)
        self.close_button.setText("稍后验证")
        if hasattr(self, "copy_button"):
            self.copy_button.setEnabled(bool(self.current_user_code))
        self._refresh_binding_status_card()

    def _show_bound_code(self, code):
        self._bound_mode = True
        self.poll_timer.stop()
        self.current_user_code = str(code or "").strip()
        display_code = self.current_user_code or "正在从服务器同步..."
        warning = self.app_window._auth_clock_skew_warning()
        self.code_label.setText(f"已绑定的绑定码：{display_code}")
        self.instruction_label.setText(warning or "当前设备已完成来源验证。需要换设备时，请联系管理员释放旧设备后重新生成绑定码。")
        if self.current_user_code:
            status_text = "授权有效。你可以复制已记录的绑定码，或关闭窗口继续使用。" + (f" {warning}" if warning else "")
        else:
            status_text = "授权有效，正在从服务器同步绑定码。同步完成后可复制。" + (f" {warning}" if warning else "")
        auth_state = self.app_window.auth_state
        if getattr(auth_state, "license_expires_at", 0) > 0:
            remaining = float(auth_state.license_expires_at) - time.time()
            if remaining > 0:
                hours = int(remaining / 3600)
                if hours >= 24:
                    status_text += f"\n离线授权剩余 {hours // 24} 天。"
                else:
                    status_text += f"\n离线授权剩余 {hours} 小时。"
            else:
                grace_deadline = float(auth_state.license_expires_at) + LICENSE_GRACE_DAYS * 86400
                grace_remaining = grace_deadline - time.time()
                if grace_remaining > 0:
                    grace_days = int(grace_remaining / 86400)
                    status_text += f"\n离线授权已过期，宽限期剩余 {grace_days} 天，请联网续期。"
                else:
                    status_text += "\n离线授权已过期，请联网续期。"
        self.status_label.setText(status_text)
        self.copy_button.setEnabled(bool(self.current_user_code))
        self.generate_button.setVisible(False)
        self.generate_button.setEnabled(False)
        self.generate_button.setToolTip("当前设备已完成来源验证，无需重新生成绑定码。")
        self.recheck_button.setVisible(False)
        self.recheck_button.setEnabled(False)
        self.close_button.setText("关闭")
        self._refresh_binding_status_card()

    def copy_user_code(self):
        code = str(getattr(self, "current_user_code", "") or "").strip()
        if not code:
            self.status_label.setText("尚未生成绑定码，无法复制。")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(code)
        if getattr(self, "_bound_mode", False):
            self.status_label.setText(f"已绑定的绑定码已复制：{code}。")
        else:
            self.status_label.setText(f"绑定码已复制：{code}。请在指定 QQ 群发送 /bind {code}。")
        if hasattr(self.app_window, "show_toast"):
            self.app_window.show_toast("绑定码已复制", "success")

    def refresh_groups(self):
        self._stop_group_worker()
        self.group_worker = AuthPublicGroupsWorker(self.app_window._auth_base_url(), parent=self)
        self.group_worker.completed.connect(self._handle_groups_result)
        self.group_worker.finished.connect(self.group_worker.deleteLater)
        self.group_worker.start()

    def _handle_groups_result(self, ok, data, error):
        self.group_worker = None
        if not ok:
            fallback = [
                {
                    "group_id": "483584006",
                    "group_name": "YHoAutoFish交流群",
                    "member_count": 0,
                    "max_member_count": 0,
                    "join_url": "https://qm.qq.com/q/DR9CCFdYK4",
                }
            ]
            self._render_groups(fallback, f"群列表暂时无法刷新，已显示默认入口：{error}")
            return
        groups = data.get("groups") if isinstance(data, dict) else []
        self._render_groups(groups or [], "服务器暂未公开群入口，请稍后再试。")
        self.app_window._cached_groups = groups or []
        self.app_window._cached_groups_at = time.time()

    def _render_groups(self, groups, empty_text):
        while self.group_layout.count():
            item = self.group_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not groups:
            empty = QLabel(empty_text)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10); border-radius: 12px; color: {APP_COLORS['text_dim']}; padding: 10px; font-size: 12px;"
            )
            self.group_layout.addWidget(empty)
            return
        for group in groups:
            group_id = str(group.get("group_id") or "")
            group_name = str(group.get("group_name") or "YHoAutoFish交流群")
            member_count = int(group.get("member_count") or 0)
            max_member_count = int(group.get("max_member_count") or 0)
            join_url = str(group.get("join_url") or "")
            row = QFrame()
            row.setStyleSheet(
                "QFrame { background-color: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.10); border-radius: 12px; }"
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 10, 8)
            title = QLabel(f"{group_name}（{group_id}）")
            title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 12px; font-weight: 800;")
            row_layout.addWidget(title, 1)
            count_text = f"{member_count}/{max_member_count}" if max_member_count else (str(member_count) if member_count else "人数未知")
            count = QLabel(count_text)
            count.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 11px;")
            row_layout.addWidget(count)
            join_btn = QPushButton("加入")
            join_btn.setFocusPolicy(Qt.NoFocus)
            join_btn.setCursor(Qt.PointingHandCursor)
            join_btn.setEnabled(bool(join_url))
            join_btn.setStyleSheet(primary_button_stylesheet() if join_url else secondary_button_stylesheet())
            if join_url:
                join_btn.clicked.connect(lambda _checked=False, url=join_url: QDesktopServices.openUrl(QUrl(url)))
            row_layout.addWidget(join_btn)
            self.group_layout.addWidget(row)

    def recheck_authorization(self):
        if not self.app_window.auth_state.access_token:
            self.status_label.setText("本机没有可复验的授权令牌，请生成绑定码重新绑定。")
            return
        self._set_busy(True)
        self.status_label.setText("正在使用本机已有授权记录在线复验...")
        self.app_window.start_authorization_check(silent=False, reason="manual")
        QTimer.singleShot(1400, lambda: self._set_busy(False))

    def start_activation(self):
        recovery = classify_auth_recovery(self.app_window.auth_state)
        if self.app_window.auth_state.access_token and recovery.mode in ("recheck_only", "transient_error"):
            self._set_busy(True)
            self.status_label.setText("当前状态需要先在线复验...")
            self.app_window.start_authorization_check(silent=False, reason="manual")
            QTimer.singleShot(1400, lambda: self._set_busy(False))
            return
        if self.app_window.auth_state.access_token and recovery.mode in ("can_rebind", "device_mismatch"):
            answer = QMessageBox.question(
                self,
                "重新绑定本机",
                "将用新绑定码重新绑定本机，旧本地授权记录会保留在日志中但不再用于复验。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
        base_url = self.app_window._auth_base_url()
        self._stop_worker()
        self._set_busy(True)
        self.status_label.setText("正在向授权服务器申请绑定码...")
        self.worker = AuthActivationWorker(
            "start",
            base_url,
            self.app_window.auth_device_hash,
            self.app_window.auth_install_id,
            APP_VERSION,
            device_hash_v2=self.app_window.auth_device_hash_v2,
            parent=self,
        )
        self.worker.completed.connect(self._handle_worker_result)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def poll_activation(self):
        if getattr(self, "worker", None) is not None and self.worker.isRunning():
            return
        activation_id = getattr(self, "activation_id", "") or self.app_window.auth_state.activation_id
        if not activation_id:
            return
        base_url = self.app_window._auth_base_url()
        self.worker = AuthActivationWorker(
            "poll",
            base_url,
            self.app_window.auth_device_hash,
            self.app_window.auth_install_id,
            APP_VERSION,
            activation_id=activation_id,
            device_hash_v2=self.app_window.auth_device_hash_v2,
            parent=self,
        )
        self.worker.completed.connect(self._handle_worker_result)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def _handle_worker_result(self, action, ok, data, error):
        self.worker = None
        self._set_busy(False)
        if not ok:
            self.status_label.setText(f"请求失败：{error}")
            return
        if action == "start":
            activation_id = str(data.get("activation_id") or "")
            user_code = str(data.get("user_code") or "")
            if not activation_id or not user_code:
                self.status_label.setText("授权服务器返回异常，缺少绑定码。")
                return
            state = build_pending_activation_state(
                self.app_window.auth_state,
                activation_id,
                user_code,
                self.app_window.auth_device_hash,
                message="等待 QQ 群绑定",
            )
            self.app_window._apply_auth_state(state, persist=True)
            self._show_code(user_code, activation_id)
            self._refresh_binding_status_card()
            self.poll_timer.start()
            return

        status = str(data.get("status") or "")
        if data.get("authorized"):
            server_time = float(data.get("server_time") or time.time())
            preserved_activation_id = str(getattr(self, "activation_id", "") or self.app_window.auth_state.activation_id or "")
            preserved_user_code = str(
                data.get("user_code")
                or getattr(self, "current_user_code", "")
                or self.app_window.auth_state.user_code
                or ""
            )
            state = AuthState(
                status="authorized",
                access_token=str(data.get("access_token") or ""),
                license_id=str(data.get("license_id") or ""),
                device_hash=self.app_window.auth_device_hash,
                qq_user_id_hash=str(data.get("qq_user_id_hash") or ""),
                expires_at=float(data.get("expires_at") or 0),
                activation_id=preserved_activation_id,
                user_code=preserved_user_code,
                message="授权成功",
            )
            # Save license and refresh token from server response
            license_blob = data.get("license")
            if license_blob:
                try:
                    import json as _json
                    from core.auth_license import save_license
                    QTimer.singleShot(0, lambda lb=license_blob: save_license(lb))
                    state.license_data = _json.dumps(license_blob, ensure_ascii=False)
                    state.license_expires_at = float(
                        (license_blob.get("payload") or {}).get("expires_at")
                        or data.get("expires_at") or 0
                    )
                except Exception:
                    pass
            refresh_tok = data.get("refresh_token")
            if refresh_tok:
                state.refresh_token = str(refresh_tok)
                state.refresh_expires_at = float(data.get("refresh_expires_at") or 0)
            state.apply_check_timing(server_time)
            self.app_window._apply_auth_state(state, persist=True)
            self.app_window.auth_verified_this_session = True
            self.status_label.setText("授权成功，功能已解锁。")
            self._refresh_binding_status_card()
            self.poll_timer.stop()
            QTimer.singleShot(450, self.accept)
            return
        if status == "token_already_issued":
            self.poll_timer.stop()
            message = str(data.get("message") or "这个绑定码已经完成过授权。若当前设备仍未解锁，请重新生成绑定码并在群内绑定。")
            state = AuthState.from_dict(self.app_window.auth_state.to_dict())
            state.status = "unknown"
            state.access_token = ""
            state.activation_id = ""
            state.user_code = ""
            state.device_hash = self.app_window.auth_device_hash
            state.license_id = str(data.get("license_id") or state.license_id or "")
            state.qq_user_id_hash = str(data.get("qq_user_id_hash") or state.qq_user_id_hash or "")
            state.message = message
            self.app_window._apply_auth_state(state, persist=True)
            self._bound_mode = False
            self.current_user_code = ""
            self.activation_id = ""
            self.code_label.setText("尚未生成绑定码")
            self.instruction_label.setText("这个绑定码已经领取过授权，不能重复使用。请点击“生成绑定码”，再在指定 QQ 群发送新的 /bind 命令。")
            self.status_label.setText(message)
            self.copy_button.setEnabled(False)
            self.generate_button.setVisible(True)
            self.generate_button.setText("生成新绑定码")
            self.generate_button.setEnabled(True)
            self.generate_button.setToolTip("")
            self.recheck_button.setVisible(False)
            self.recheck_button.setEnabled(False)
            self.close_button.setText("稍后验证")
            self._refresh_binding_status_card()
            return
        if status == "pending":
            self.status_label.setText("仍在等待 QQ 群绑定，请确认命令已发送到指定群。")
        elif status == "expired":
            self.poll_timer.stop()
            self.status_label.setText("绑定码已过期，请重新生成。")
        else:
            self.status_label.setText(str(data.get("message") or f"当前状态：{status}"))


class PolicyDialog(QDialog):
    def __init__(self, title, subtitle, html, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.97);
                border: 1px solid rgba(89, 125, 164, 0.28);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=34, alpha=120, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;"
        )
        layout.addWidget(title_label)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setWordWrap(True)
        subtitle_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px;"
        )
        layout.addWidget(subtitle_label)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setFocusPolicy(Qt.NoFocus)
        body.setStyleSheet(text_edit_stylesheet())
        body.setHtml(html)
        layout.addWidget(body, 1)

        close_btn = QPushButton("已阅读，关闭")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setStyleSheet(primary_button_stylesheet())
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignRight)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(700, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.98);
                border: 1px solid rgba(99, 228, 228, 0.28);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=36, alpha=130, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(16)

        logo = QLabel()
        logo.setFixedSize(86, 86)
        logo.setStyleSheet("background: transparent; border: none;")
        logo_path = resource_path("logo.jpg")
        pixmap = rounded_pixmap(logo_path, 86, 86, radius=22, keep_full=False)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        else:
            logo.setText("YH")
            logo.setAlignment(Qt.AlignCenter)
            logo.setStyleSheet(
                f"background-color: rgba(29, 208, 214, 0.18); border: 1px solid rgba(99, 228, 228, 0.30); border-radius: 22px; color: {APP_COLORS['accent_soft']}; font-size: 24px; font-weight: 900;"
            )
        header.addWidget(logo, 0, Qt.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(6)
        title = QLabel("异环自动钓鱼")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;"
        )
        title_col.addWidget(title)

        subtitle = QLabel(f"YHo AutoFish · 版本 {APP_VERSION}")
        subtitle.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 13px; font-weight: 800;"
        )
        title_col.addWidget(subtitle)

        description = QLabel("基于屏幕截图、图像识别、OCR 与键盘模拟的自动钓鱼辅助工具。")
        description.setWordWrap(True)
        description.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px;"
        )
        title_col.addWidget(description)
        header.addLayout(title_col, 1)
        layout.addLayout(header)

        info_panel = QFrame()
        info_panel.setProperty("variant", "soft")
        info_panel.setStyleSheet(panel_stylesheet())
        info_layout = QVBoxLayout(info_panel)
        info_layout.setContentsMargins(18, 16, 18, 16)
        info_layout.setSpacing(10)

        rows = (
            ("作者", APP_AUTHOR),
            ("开源地址", APP_REPOSITORY_URL),
            ("使用范围", "个人学习、研究与本地非商业使用"),
            ("实现方式", "不读取或修改游戏内存，不注入 DLL，不修改游戏资源文件"),
            ("风险提示", "自动化行为仍可能违反平台规则，账号与使用风险由使用者自行承担"),
        )
        for label, value in rows:
            row = QLabel(f"<span style='color:{APP_COLORS['text']}; font-weight:900;'>{label}</span>"
                         f"<span style='color:{APP_COLORS['text_dim']};'>　{value}</span>")
            row.setWordWrap(True)
            row.setStyleSheet("background: transparent; border: none; font-size: 13px;")
            info_layout.addWidget(row)
        layout.addWidget(info_panel, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        github_btn = QPushButton("打开 GitHub")
        github_btn.setFocusPolicy(Qt.NoFocus)
        github_btn.setCursor(Qt.PointingHandCursor)
        github_btn.setStyleSheet(secondary_button_stylesheet())
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(APP_REPOSITORY_URL)))
        action_row.addWidget(github_btn)

        license_btn = QPushButton("查看用户协议")
        license_btn.setFocusPolicy(Qt.NoFocus)
        license_btn.setCursor(Qt.PointingHandCursor)
        license_btn.setStyleSheet(secondary_button_stylesheet())
        if parent is not None and hasattr(parent, "show_usage_policy"):
            license_btn.clicked.connect(parent.show_usage_policy)
        action_row.addWidget(license_btn)

        action_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(primary_button_stylesheet())
        close_btn.clicked.connect(self.accept)
        action_row.addWidget(close_btn)
        layout.addLayout(action_row)


class SponsorHideDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 330)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = PulseDialogShell("coffee")
        add_shadow(shell, blur=30, alpha=120, offset=(0, 12))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("关闭请喝咖啡入口")
        title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 24px; font-weight: 900;")
        layout.addWidget(title)

        body = QLabel(
            "确认关闭后，标题栏里的“请作者喝一点咖啡吗？”入口会从后续启动中隐藏，不会再自动显示。\n\n"
            "这个设置只影响赞助入口的展示，不会影响自动钓鱼、钓鱼记录、图鉴记录、更新检查和其他功能。\n\n"
            "后续如需恢复显示，可以在配置文件中恢复对应显示设置。"
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; line-height: 1.5;")
        layout.addWidget(body, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setFocusPolicy(Qt.NoFocus)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(secondary_button_stylesheet())
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)

        confirm_btn = QPushButton("确认，不再显示")
        confirm_btn.setFocusPolicy(Qt.NoFocus)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setStyleSheet(primary_button_stylesheet())
        confirm_btn.clicked.connect(self.accept)
        actions.addWidget(confirm_btn)
        layout.addLayout(actions)


class QQGroupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.app_window = parent
        self.group_worker = None
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 315)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = PulseDialogShell("purple")
        add_shadow(shell, blur=32, alpha=130, offset=(0, 12))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel("QQ群")
        title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 24px; font-weight: 900;")
        layout.addWidget(title)

        note = QLabel("当前可加入的交流群如下。进群后可发送 /yho help 查看绑定、群规和入口菜单。")
        note.setWordWrap(True)
        note.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px;")
        layout.addWidget(note)

        self.group_scroll = QScrollArea()
        self.group_scroll.setFrameShape(QFrame.NoFrame)
        self.group_scroll.setWidgetResizable(True)
        self.group_scroll.setFixedHeight(150)
        self.group_scroll.setStyleSheet(scroll_area_stylesheet())
        self.group_scroll.verticalScrollBar().setStyleSheet(scrollbar_stylesheet(compact=True))
        self.group_container = QWidget()
        self.group_layout = QVBoxLayout(self.group_container)
        self.group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_layout.setSpacing(8)
        self.group_scroll.setWidget(self.group_container)
        layout.addWidget(self.group_scroll)
        self._render_groups([], "正在从服务器加载群入口...")

        close_btn = QPushButton("关闭")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(secondary_button_stylesheet())
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignRight)
        self.refresh_groups()

    def closeEvent(self, event):
        self._stop_group_worker()
        super().closeEvent(event)

    def reject(self):
        self._stop_group_worker()
        super().reject()

    def _stop_group_worker(self):
        worker = self.group_worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.finished.connect(lambda w=worker: w.deleteLater())
        except RuntimeError:
            pass
        self.group_worker = None

    def refresh_groups(self):
        self._stop_group_worker()
        if self.app_window is None or not hasattr(self.app_window, "_auth_base_url"):
            self._handle_groups_result(False, {}, "授权服务地址不可用")
            return
        self.group_worker = AuthPublicGroupsWorker(self.app_window._auth_base_url(), parent=self)
        self.group_worker.completed.connect(self._handle_groups_result)
        self.group_worker.finished.connect(self.group_worker.deleteLater)
        self.group_worker.start()

    def _handle_groups_result(self, ok, data, error):
        self.group_worker = None
        if not ok:
            fallback = [
                {
                    "group_id": "483584006",
                    "group_name": "YHoAutoFish交流群",
                    "member_count": 0,
                    "max_member_count": 0,
                    "join_url": "https://qm.qq.com/q/DR9CCFdYK4",
                }
            ]
            self._render_groups(fallback, f"服务器群入口暂时无法刷新，已显示默认入口：{error}")
            return
        groups = data.get("groups") if isinstance(data, dict) else []
        self._render_groups(groups or [], "服务器暂未公开群入口，请稍后再试。")

    def _render_groups(self, groups, empty_text):
        while self.group_layout.count():
            item = self.group_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not groups:
            empty = QLabel(empty_text)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10); border-radius: 14px; color: {APP_COLORS['text_dim']}; padding: 12px; font-size: 12px;"
            )
            self.group_layout.addWidget(empty)
            self.group_layout.addStretch(1)
            return
        for group in groups:
            group_id = str(group.get("group_id") or "")
            group_name = str(group.get("group_name") or "YHoAutoFish交流群")
            member_count = int(group.get("member_count") or 0)
            max_member_count = int(group.get("max_member_count") or 0)
            join_url = str(group.get("join_url") or "")
            row = QFrame()
            row.setFixedHeight(68)
            row.setStyleSheet(
                "QFrame { background-color: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.10); border-radius: 14px; }"
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 7, 10, 7)
            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            title = QLabel(f"{group_name}（{group_id}）")
            title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 13px; font-weight: 900;")
            text_col.addWidget(title)
            count_text = f"{member_count}/{max_member_count}" if max_member_count else (str(member_count) if member_count else "人数未知")
            count = QLabel(count_text)
            count.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 11px;")
            text_col.addWidget(count)
            row_layout.addLayout(text_col, 1)
            join_btn = QPushButton("加入")
            join_btn.setFocusPolicy(Qt.NoFocus)
            join_btn.setCursor(Qt.PointingHandCursor)
            join_btn.setEnabled(bool(join_url))
            join_btn.setStyleSheet(primary_button_stylesheet() if join_url else secondary_button_stylesheet())
            if join_url:
                join_btn.clicked.connect(lambda _checked=False, url=join_url: QDesktopServices.openUrl(QUrl(url)))
            row_layout.addWidget(join_btn)
            self.group_layout.addWidget(row)
        self.group_layout.addStretch(1)


class SponsorQrImageCard(QFrame):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.source_pixmap = QPixmap(image_path) if image_path and os.path.exists(image_path) else QPixmap()
        if self.source_pixmap.isNull():
            self.rendered_pixmap = QPixmap()
            self.setFixedSize(300, 300)
        else:
            self.rendered_pixmap = self.source_pixmap.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.setFixedSize(self.rendered_pixmap.width(), self.rendered_pixmap.height())
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        image_rect = self.rect().adjusted(1, 1, -1, -1)
        image_radius = min(42, max(24, int(min(image_rect.width(), image_rect.height()) * 0.16)))
        image_path = QPainterPath()
        image_path.addRoundedRect(image_rect, image_radius, image_radius)

        if not self.rendered_pixmap.isNull():
            painter.setClipPath(image_path)
            painter.drawPixmap(0, 0, self.rendered_pixmap)
            painter.setClipping(False)
            painter.setPen(QPen(QColor(232, 176, 102, 96), 1.05))
            painter.drawPath(image_path)
        else:
            painter.fillPath(image_path, QColor(8, 18, 30, 202))
            painter.setPen(QPen(QColor(255, 255, 255, 30), 1.1))
            painter.drawPath(image_path)
            painter.setPen(QColor(APP_COLORS["text_dim"]))
            painter.setFont(QFont("Microsoft YaHei UI", 12, QFont.DemiBold))
            painter.drawText(self.rect(), Qt.AlignCenter, "图片读取失败")

        painter.end()
        super().paintEvent(event)


class SponsorDialog(QDialog):
    def __init__(self, image_paths, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(720, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = PulseDialogShell("coffee")
        add_shadow(shell, blur=36, alpha=135, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("请喝咖啡")
        title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;")
        layout.addWidget(title)

        note = QLabel(
            "如果这个工具帮你节省了重复操作的时间，可以用这种方式支持后续维护。"
            "收到的支持会优先用于识别模板更新、不同分辨率适配、异常日志排查和发布前测试。"
            "完全自愿，不影响任何功能使用。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px;")
        layout.addWidget(note)

        qr_row = QHBoxLayout()
        qr_row.setContentsMargins(0, 8, 0, 4)
        qr_row.setSpacing(92)
        if image_paths:
            qr_row.addStretch(1)
            for _label, image_path in image_paths[:2]:
                qr_row.addWidget(SponsorQrImageCard(image_path), 0, Qt.AlignCenter)
            qr_row.addStretch(1)
        else:
            missing = QLabel("未找到收款码图片，请检查发布目录中的 sponsor_qr 资源。")
            missing.setWordWrap(True)
            missing.setAlignment(Qt.AlignCenter)
            missing.setStyleSheet(f"background: rgba(255,255,255,0.055); border: 1px solid rgba(255,255,255,0.14); border-radius: 18px; color: {APP_COLORS['text_dim']}; padding: 28px; font-size: 13px;")
            qr_row.addWidget(missing)
        layout.addLayout(qr_row, 1)

        close_btn = QPushButton("关闭")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(primary_button_stylesheet())
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignRight)


class UpdatePolicyConfirmDialog(QDialog):
    def __init__(self, update_info, app_window, parent=None):
        super().__init__(parent)
        self.update_info = update_info
        self.app_window = app_window
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(720, 430)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(14, 22, 34, 0.98);
                border: 1px solid rgba(241, 190, 103, 0.48);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=36, alpha=130, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("更新前确认")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;"
        )
        layout.addWidget(title)

        subtitle = QLabel(f"即将查看 v{update_info.version} 更新内容。继续前请再次确认你已知晓并接受用户协议和反侵权协议。")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 13px; font-weight: 800;"
        )
        layout.addWidget(subtitle)

        notice = QTextEdit()
        notice.setReadOnly(True)
        notice.setFocusPolicy(Qt.NoFocus)
        notice.setStyleSheet(text_edit_stylesheet())
        notice.setHtml(
            """
            <div style="font-family:'Microsoft YaHei UI'; line-height:1.7;">
              <p style="margin:0 0 10px 0; color:#F3F8FF; font-size:14px; font-weight:800;">
                点击“确定，继续更新”即表示你已经重新确认以下事项：
              </p>
              <p style="margin:0 0 8px 0; color:#9AB0CA; font-size:13px;">
                1. 本程序仅用于图像识别、自动化流程学习与个人技术研究，不得用于商业牟利、代练代刷、批量传播或破坏公平性的用途。
              </p>
              <p style="margin:0 0 8px 0; color:#9AB0CA; font-size:13px;">
                2. 自动化行为可能违反平台规则并带来账号、设备、收益或其他风险，后果由实际使用者自行承担。
              </p>
              <p style="margin:0 0 8px 0; color:#9AB0CA; font-size:13px;">
                3. 本程序开源免费发布，任何付费出售、卡密售卖、二次打包收费或冒充官方工具的行为均非作者授权。
              </p>
            </div>
            """
        )
        layout.addWidget(notice, 1)

        link_row = QHBoxLayout()
        link_row.setSpacing(10)
        usage_btn = QPushButton("查看用户协议")
        usage_btn.setFocusPolicy(Qt.NoFocus)
        usage_btn.setCursor(Qt.PointingHandCursor)
        usage_btn.setStyleSheet(secondary_button_stylesheet())
        usage_btn.clicked.connect(app_window.show_usage_policy)
        link_row.addWidget(usage_btn)

        infringement_btn = QPushButton("查看反侵权协议")
        infringement_btn.setFocusPolicy(Qt.NoFocus)
        infringement_btn.setCursor(Qt.PointingHandCursor)
        infringement_btn.setStyleSheet(secondary_button_stylesheet())
        infringement_btn.clicked.connect(app_window.show_anti_infringement_policy)
        link_row.addWidget(infringement_btn)

        link_row.addStretch()
        layout.addLayout(link_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFocusPolicy(Qt.NoFocus)
        cancel_btn.setStyleSheet(secondary_button_stylesheet())
        cancel_btn.clicked.connect(self.reject)
        action_row.addWidget(cancel_btn)

        accept_btn = QPushButton("确定，继续更新")
        accept_btn.setFocusPolicy(Qt.NoFocus)
        accept_btn.setCursor(Qt.PointingHandCursor)
        accept_btn.setStyleSheet(primary_button_stylesheet())
        accept_btn.clicked.connect(self.accept)
        action_row.addWidget(accept_btn)
        layout.addLayout(action_row)


class UpdateDialog(QDialog):
    def __init__(self, update_info, app_window, parent=None):
        super().__init__(parent)
        self.update_info = update_info
        self.app_window = app_window
        self.selected_update_source = "github"
        self.is_downloading = False
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(760, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.98);
                border: 1px solid rgba(241, 190, 103, 0.42);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=38, alpha=135, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel(f"发现新版 v{update_info.version}")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;"
        )
        layout.addWidget(title)

        subtitle = QLabel(f"当前版本 v{APP_VERSION}，发布包：{update_info.asset_name}")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 13px; font-weight: 800;"
        )
        layout.addWidget(subtitle)

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setFocusPolicy(Qt.NoFocus)
        notes.setStyleSheet(text_edit_stylesheet())
        release_body = html.escape(update_info.body or "此版本未填写更新说明。").replace("\n", "<br>")
        notes.setHtml(f"<div style='font-family:Microsoft YaHei UI; color:#9AB0CA; font-size:13px;'>{release_body}</div>")
        layout.addWidget(notes, 1)

        source_label = QLabel("选择下载源")
        source_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 13px; font-weight: 900;"
        )
        layout.addWidget(source_label)

        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        self.github_source_btn = QPushButton("GitHub 官方源（默认）\n发布源权威，全球访问更稳；国内网络可能较慢")
        self.gitee_source_btn = QPushButton("Gitee 国内源\n国内访问通常更快；依赖 Gitee 发行版同步状态")
        self.source_buttons = {
            "github": self.github_source_btn,
            "gitee": self.gitee_source_btn,
        }
        for source_name, button in self.source_buttons.items():
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(58)
            button.clicked.connect(lambda _checked=False, name=source_name: self.set_update_source(name))
            source_row.addWidget(button, 1)
        layout.addLayout(source_row)
        self.source_hint = QLabel("")
        self.source_hint.setWordWrap(True)
        self.source_hint.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(self.source_hint)
        self.set_update_source("github")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("等待开始")
        self.progress.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 12px;
                color: {APP_COLORS['text_dim']};
                font-size: 12px;
                font-weight: 800;
                text-align: center;
                min-height: 24px;
            }}
            QProgressBar::chunk {{
                border-radius: 11px;
                background-color: {APP_COLORS['accent']};
            }}
            """
        )
        layout.addWidget(self.progress)

        self.status_label = QLabel("自动更新会先下载新版压缩包，再启动独立更新器覆盖程序文件；用户数据不会被覆盖。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(self.status_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self.manual_btn = QPushButton("手动下载压缩包")
        self.manual_btn.setFocusPolicy(Qt.NoFocus)
        self.manual_btn.setCursor(Qt.PointingHandCursor)
        self.manual_btn.setStyleSheet(secondary_button_stylesheet())
        self.manual_btn.clicked.connect(self.open_selected_source_download)
        action_row.addWidget(self.manual_btn)

        action_row.addStretch()

        self.close_btn = QPushButton("稍后再说")
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setStyleSheet(secondary_button_stylesheet())
        self.close_btn.clicked.connect(self.handle_close_or_cancel)
        action_row.addWidget(self.close_btn)

        self.auto_btn = QPushButton("一键全自动更新")
        self.auto_btn.setFocusPolicy(Qt.NoFocus)
        self.auto_btn.setCursor(Qt.PointingHandCursor)
        self.auto_btn.setStyleSheet(primary_button_stylesheet())
        self.auto_btn.clicked.connect(lambda: app_window.start_auto_update(self))
        action_row.addWidget(self.auto_btn)
        layout.addLayout(action_row)

    def set_busy(self, busy):
        self.is_downloading = bool(busy)
        self.auto_btn.setEnabled(not busy)
        self.manual_btn.setEnabled(not busy)
        self.close_btn.setEnabled(True)
        self.close_btn.setText("取消下载" if busy else "稍后再说")
        for button in self.source_buttons.values():
            button.setEnabled(not busy)
        if busy:
            self.progress.setFormat("正在下载 %p%")
            self.status_label.setText(f"正在通过{self.source_display_name()}下载更新包，请不要关闭程序。下载完成后会打开独立安装器显示安装进度。")

    def handle_close_or_cancel(self):
        if self.is_downloading:
            self.app_window.cancel_auto_update(self)
            return
        self.reject()

    def set_canceling(self):
        self.close_btn.setEnabled(False)
        self.progress.setFormat("正在取消")
        self.status_label.setText("正在取消下载并清理未完成的更新文件...")

    def set_cancelled(self, message="更新下载已取消。"):
        self.set_busy(False)
        self.progress.setFormat("已取消")
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )

    def set_progress(self, value):
        self.progress.setValue(max(0, min(100, int(value))))

    def source_button_stylesheet(self):
        return f"""
        QPushButton {{
            background-color: rgba(255, 255, 255, 0.045);
            border: 1px solid rgba(111, 145, 182, 0.22);
            border-radius: 12px;
            color: {APP_COLORS['text_dim']};
            font-size: 12px;
            font-weight: 800;
            text-align: left;
            padding: 9px 12px;
        }}
        QPushButton:checked {{
            background-color: rgba(34, 211, 214, 0.15);
            border: 1px solid rgba(99, 228, 228, 0.72);
            color: {APP_COLORS['text']};
        }}
        QPushButton:disabled {{
            color: rgba(154, 176, 202, 0.45);
            border-color: rgba(111, 145, 182, 0.10);
        }}
        """

    def set_update_source(self, source_name):
        if source_name not in self.source_buttons:
            source_name = "github"
        self.selected_update_source = source_name
        style = self.source_button_stylesheet()
        for name, button in self.source_buttons.items():
            button.setChecked(name == source_name)
            button.setStyleSheet(style)
        if source_name == "gitee":
            self.source_hint.setText("Gitee 国内源适合国内网络环境；如果 Gitee 发行版附件未同步、文件名不一致或下载失败，可切回 GitHub 官方源。")
        else:
            self.source_hint.setText("GitHub 官方源为默认源，优先使用项目正式 Release；国内网络不稳定时可以改选 Gitee 国内源。")

    def source_display_name(self):
        return "Gitee 国内源" if self.selected_update_source == "gitee" else "GitHub 官方源"

    def open_selected_source_download(self):
        if self.selected_update_source == "gitee" and getattr(self.update_info, "gitee_asset_parts", ()):
            QDesktopServices.openUrl(QUrl(getattr(self.update_info, "gitee_html_url", "") or self.update_info.html_url))
            return
        candidates = get_download_candidates(self.update_info, source=self.selected_update_source)
        if not candidates:
            self.set_error(f"{self.source_display_name()}没有可用下载地址。")
            return
        QDesktopServices.openUrl(QUrl(candidates[0]))

    def set_error(self, message):
        self.set_busy(False)
        self.progress.setFormat("更新失败")
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['danger']}; font-size: 12px; font-weight: 800;"
        )

    def set_installing_started(self):
        self.progress.setValue(100)
        self.progress.setFormat("下载完成，正在切换到安装器")
        self.status_label.setText("独立安装器已启动。当前版本即将关闭，后续安装进度、安装结果和启动新版按钮会在安装器窗口中显示。")


class TakeoverPauseDialog(QDialog):
    def __init__(self, detail, parent=None):
        super().__init__(None)
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(620, 330)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(19, 7, 11, 0.98);
                border: 2px solid rgba(255, 87, 104, 0.92);
                border-radius: 28px;
            }
            """
        )
        add_shadow(shell, blur=42, alpha=180, offset=(0, 12))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)

        badge = QLabel("自动钓鱼已暂停")
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            """
            QLabel {
                background-color: rgba(255, 87, 104, 0.20);
                border: 1px solid rgba(255, 151, 90, 0.55);
                border-radius: 18px;
                color: #FFE7B0;
                font-size: 28px;
                font-weight: 900;
                padding: 12px 18px;
            }
            """
        )
        layout.addWidget(badge)

        detail_label = QLabel(f"检测到用户接管：{detail or '游戏窗口内输入'}")
        detail_label.setWordWrap(True)
        detail_label.setAlignment(Qt.AlignCenter)
        detail_label.setStyleSheet(
            "background: transparent; border: none; color: #FFF7E8; font-size: 15px; font-weight: 800;"
        )
        layout.addWidget(detail_label)

        body = QLabel("程序已释放全部按键并停止后续操作。需要继续时，请重新点击“开始钓鱼”，并保持挂机状态不要操作游戏。")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setStyleSheet(
            "background: transparent; border: none; color: #FFD3D7; font-size: 13px; line-height: 1.6;"
        )
        layout.addWidget(body)

        close_btn = QPushButton("我知道了")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setMinimumHeight(42)
        close_btn.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 207, 102, 0.96);
                color: #261307;
                border: none;
                border-radius: 18px;
                padding: 8px 24px;
                font-size: 14px;
                font-weight: 900;
            }
            QPushButton:hover {
                background-color: rgba(255, 226, 143, 1.0);
            }
            """
        )
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignCenter)


class LowResolutionWarningDialog(QDialog):
    def __init__(self, width, height, min_width=1600, min_height=900, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(660, 390)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(17, 21, 30, 0.98);
                border: 1px solid rgba(241, 190, 103, 0.56);
                border-radius: 30px;
            }
            """
        )
        add_shadow(shell, blur=36, alpha=140, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(16)

        title = QLabel("游戏分辨率偏低")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 30px; font-weight: 900;"
        )
        layout.addWidget(title)

        subtitle = QLabel(f"当前识别到的游戏客户区分辨率为 {width} × {height}，低于建议的 {min_width} × {min_height}。")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 14px; font-weight: 800;"
        )
        layout.addWidget(subtitle)

        body = QLabel(
            "分辨率过低时，右下角交互图标、上钩文字、溜鱼耐力条和结算文字会占用更少像素，"
            "识别容错会明显下降，可能出现抛竿识别慢、溜鱼跟随不稳、结算识别失败或失败后恢复变慢。"
        )
        body.setWordWrap(True)
        body.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; line-height: 1.7;"
        )
        layout.addWidget(body)

        advice = QLabel("建议先把游戏窗口或全屏分辨率调高到 1600 × 900 或以上，再开始自动钓鱼。")
        advice.setWordWrap(True)
        advice.setStyleSheet(
            """
            QLabel {
                background-color: rgba(241, 190, 103, 0.12);
                border: 1px solid rgba(241, 190, 103, 0.32);
                border-radius: 16px;
                color: #FFE7B0;
                font-size: 13px;
                font-weight: 800;
                padding: 12px 14px;
            }
            """
        )
        layout.addWidget(advice)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.addStretch()

        cancel_btn = QPushButton("先去调整")
        cancel_btn.setFocusPolicy(Qt.NoFocus)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(secondary_button_stylesheet())
        cancel_btn.clicked.connect(self.reject)
        action_row.addWidget(cancel_btn)

        continue_btn = QPushButton("仍然继续")
        continue_btn.setFocusPolicy(Qt.NoFocus)
        continue_btn.setCursor(Qt.PointingHandCursor)
        continue_btn.setStyleSheet(primary_button_stylesheet())
        continue_btn.clicked.connect(self.accept)
        action_row.addWidget(continue_btn)

        layout.addLayout(action_row)


class ToastPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(9, 20, 34, 0.94);
                border: 1px solid rgba(99, 228, 228, 0.36);
                border-radius: 18px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {APP_COLORS['text']};
                font-size: 13px;
                font-weight: 800;
            }}
            """
        )
        add_shadow(self, blur=24, alpha=110, offset=(0, 8))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        self.label = QLabel("")
        layout.addWidget(self.label)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text, tone="info"):
        color = {
            "success": APP_COLORS["success"],
            "warning": APP_COLORS["warning"],
            "danger": APP_COLORS["danger"],
        }.get(tone, APP_COLORS["accent_soft"])
        self.label.setText(f"<span style='color:{color};'>●</span> {text}")
        self.adjustSize()
        self.reposition()
        self.raise_()
        self.show()
        self.hide_timer.start(2200)

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            x = parent.width() - self.width() - 34
            y = 78
            self.move(max(24, x), y)


class FloatingControlWindow(QFrame):
    _EVENT_OBJECT_LOCATIONCHANGE = 0x800B
    _OBJID_WINDOW = 0
    _WINEVENT_OUTOFCONTEXT = 0x0000
    _WINEVENT_SKIPOWNPROCESS = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_ASYNCWINDOWPOS = 0x4000
    _WinEventProc = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    def __init__(self, app_window):
        super().__init__(None)
        self.app_window = app_window
        self._last_window_find = 0.0
        self._last_target_pos = None
        self._event_hook = None
        self._event_callback = None
        self._pending_hook_update = False
        self._user_visible_requested = False
        self._temporarily_hidden_by_game = False
        self._hidden_for_capture = False
        self._collapsed = False
        self._active_page_index = 0
        self._last_log_version = -1
        self._last_log_text = None
        self.setWindowTitle("异环自动钓鱼悬浮控制")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(0)

        self.shell = QFrame()
        self.shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(10, 20, 34, 0.92);
                border: 1px solid rgba(103, 234, 236, 0.28);
                border-radius: 22px;
            }
            """
        )
        add_shadow(self.shell, blur=28, alpha=120, offset=(0, 10))
        root.addWidget(self.shell)

        layout = QVBoxLayout(self.shell)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(11)

        header = QHBoxLayout()
        title = QLabel("钓鱼悬浮窗")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 14px; font-weight: 900;"
        )
        header.addWidget(title)
        header.addStretch()

        self.collapse_btn = QPushButton("−")
        self.collapse_btn.setFixedSize(30, 30)
        self.collapse_btn.setFocusPolicy(Qt.NoFocus)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setToolTip("折叠悬浮窗")
        self.collapse_btn.setStyleSheet(self._icon_button_stylesheet())
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("关闭悬浮窗")
        self.close_btn.setStyleSheet(self._icon_button_stylesheet(danger=True))
        self.close_btn.clicked.connect(self.hide_by_user)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFixedHeight(34)
        layout.addWidget(self.status_label)

        self.mode_bar = QFrame()
        self.mode_bar.setStyleSheet(
            """
            QFrame {
                background-color: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(111, 145, 182, 0.16);
                border-radius: 17px;
            }
            """
        )
        mode_layout = QHBoxLayout(self.mode_bar)
        mode_layout.setContentsMargins(4, 4, 4, 4)
        mode_layout.setSpacing(4)

        self.control_page_btn = QPushButton("控制")
        self.control_page_btn.setCheckable(True)
        self.control_page_btn.setFocusPolicy(Qt.NoFocus)
        self.control_page_btn.setCursor(Qt.PointingHandCursor)
        self.control_page_btn.clicked.connect(lambda: self.switch_floating_page(0))
        mode_layout.addWidget(self.control_page_btn)

        self.log_page_btn = QPushButton("日志")
        self.log_page_btn.setCheckable(True)
        self.log_page_btn.setFocusPolicy(Qt.NoFocus)
        self.log_page_btn.setCursor(Qt.PointingHandCursor)
        self.log_page_btn.clicked.connect(lambda: self.switch_floating_page(1))
        mode_layout.addWidget(self.log_page_btn)
        layout.addWidget(self.mode_bar)

        self.body_stack = QStackedWidget()
        self.body_stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")
        layout.addWidget(self.body_stack)

        self.control_page = QWidget()
        self.control_page.setStyleSheet("background: transparent; border: none;")
        control_layout = QVBoxLayout(self.control_page)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(11)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.start_btn = QPushButton("▶ 开始")
        self.start_btn.setFixedHeight(42)
        self.start_btn.setFocusPolicy(Qt.NoFocus)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.app_window.handle_primary_action)
        actions.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setFixedHeight(42)
        self.stop_btn.setFocusPolicy(Qt.NoFocus)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.app_window.stop_bot)
        actions.addWidget(self.stop_btn)
        control_layout.addLayout(actions)

        self.debug_panel = QFrame()
        self.debug_panel.setProperty("variant", "soft")
        self.debug_panel.setStyleSheet(panel_stylesheet())
        self.debug_panel.setMinimumHeight(198)
        debug_layout = QVBoxLayout(self.debug_panel)
        debug_layout.setContentsMargins(12, 12, 12, 12)
        debug_layout.setSpacing(10)

        debug_title = QLabel("调试溜鱼视图")
        debug_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 12px; font-weight: 800;"
        )
        debug_layout.addWidget(debug_title)

        self.debug_preview = QLabel("等待画面...")
        self.debug_preview.setAlignment(Qt.AlignCenter)
        self.debug_preview.setFixedSize(236, 118)
        self.debug_preview.setStyleSheet(
            """
            background-color: rgba(5, 12, 20, 0.78);
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 16px;
            color: #9AB0CA;
            font-size: 12px;
            font-weight: 700;
            """
        )
        debug_layout.addWidget(self.debug_preview)
        control_layout.addWidget(self.debug_panel)
        self.body_stack.addWidget(self.control_page)

        self.log_page = QWidget()
        self.log_page.setStyleSheet("background: transparent; border: none;")
        log_layout = QVBoxLayout(self.log_page)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        self.floating_log_view = QPlainTextEdit()
        self.floating_log_view.setReadOnly(True)
        self.floating_log_view.setUndoRedoEnabled(False)
        self.floating_log_view.setMaximumBlockCount(int(self.app_window.config.get("log_line_limit", 320)))
        self.floating_log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.floating_log_view.setStyleSheet(self._floating_log_stylesheet())
        log_layout.addWidget(self.floating_log_view)
        self.body_stack.addWidget(self.log_page)
        self.switch_floating_page(0)

        self.position_timer = QTimer(self)
        self.position_timer.setTimerType(Qt.PreciseTimer)
        self.position_timer.timeout.connect(self.position_near_game)
        self.position_timer.setInterval(16)

        self.visibility_timer = QTimer(self)
        self.visibility_timer.setTimerType(Qt.CoarseTimer)
        self.visibility_timer.timeout.connect(self.sync_game_visibility)
        self.visibility_timer.setInterval(250)
        self.refresh_state()

    def _icon_button_stylesheet(self, danger=False):
        hover_bg = "rgba(255, 102, 126, 0.24)" if danger else "rgba(29, 208, 214, 0.16)"
        hover_border = "rgba(255, 102, 126, 0.34)" if danger else "rgba(29, 208, 214, 0.32)"
        return f"""
        QPushButton {{
            background-color: rgba(255, 255, 255, 0.05);
            color: {APP_COLORS['text_dim']};
            border: 1px solid rgba(111, 145, 182, 0.18);
            border-radius: 15px;
            font-size: 16px;
            font-weight: 900;
        }}
        QPushButton:hover {{
            background-color: {hover_bg};
            border: 1px solid {hover_border};
            color: {APP_COLORS['text']};
        }}
        """

    def _mode_button_stylesheet(self, active=False):
        if active:
            return f"""
            QPushButton {{
                background-color: rgba(29, 208, 214, 0.22);
                color: {APP_COLORS['accent_soft']};
                border: 1px solid rgba(29, 208, 214, 0.35);
                border-radius: 13px;
                min-height: 28px;
                font-size: 12px;
                font-weight: 900;
            }}
            """
        return f"""
        QPushButton {{
            background-color: transparent;
            color: {APP_COLORS['text_dim']};
            border: 1px solid transparent;
            border-radius: 13px;
            min-height: 28px;
            font-size: 12px;
            font-weight: 800;
        }}
        QPushButton:hover {{
            background-color: rgba(255, 255, 255, 0.055);
            color: {APP_COLORS['text']};
        }}
        """

    def _floating_log_stylesheet(self):
        return f"""
        QPlainTextEdit {{
            background-color: rgba(5, 12, 20, 0.82);
            color: {APP_COLORS['text_dim']};
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 16px;
            padding: 10px;
            font-family: Consolas, 'Microsoft YaHei UI';
            font-size: 12px;
            selection-background-color: rgba(29, 208, 214, 0.24);
        }}
        {scrollbar_stylesheet(compact=True)}
        """

    def switch_floating_page(self, index):
        self._active_page_index = 1 if index == 1 else 0
        self.body_stack.setCurrentIndex(self._active_page_index)
        self.control_page_btn.setChecked(self._active_page_index == 0)
        self.log_page_btn.setChecked(self._active_page_index == 1)
        self.control_page_btn.setStyleSheet(self._mode_button_stylesheet(self._active_page_index == 0))
        self.log_page_btn.setStyleSheet(self._mode_button_stylesheet(self._active_page_index == 1))
        if self._active_page_index == 1:
            self.refresh_log_view(force=True)
        self.refresh_panel_size()

    def toggle_collapsed(self):
        self._collapsed = not self._collapsed
        self.collapse_btn.setText("□" if self._collapsed else "−")
        self.collapse_btn.setToolTip("展开悬浮窗" if self._collapsed else "折叠悬浮窗")
        if not self._collapsed and self._active_page_index == 1:
            self.refresh_log_view(force=True)
        self.refresh_panel_size()
        self.position_near_game()

    def refresh_panel_size(self):
        debug_enabled = bool(self.app_window.config.get("debug_mode", False))
        self.debug_panel.setVisible(debug_enabled and not self._collapsed and self._active_page_index == 0)
        self.mode_bar.setVisible(not self._collapsed)
        self.body_stack.setVisible(not self._collapsed)

        if self._collapsed:
            width, height = 304, 112
        elif self._active_page_index == 1:
            width, height = 340, 410
        else:
            width = 304
            height = 452 if debug_enabled else 226

        self.setFixedSize(width, height)
        self.adjustSize()

    def refresh_log_view(self, force=False):
        if not hasattr(self, "floating_log_view"):
            return
        if not force and (not self.isVisible() or self._collapsed or self._active_page_index != 1):
            return

        version = getattr(self.app_window, "_log_version", 0)
        if not force and version == self._last_log_version:
            return

        self.floating_log_view.setMaximumBlockCount(int(self.app_window.config.get("log_line_limit", 320)))
        text = "\n".join(self.app_window.log_deque)
        if not text:
            text = "--- 异环自动钓鱼初始化完成 ---\n请确保游戏窗口处于可操作状态。"
        if force or text != self._last_log_text:
            self.floating_log_view.setPlainText(text)
            scrollbar = self.floating_log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            self._last_log_text = text
        self._last_log_version = version

    @property
    def user_visible_requested(self):
        return self._user_visible_requested

    def set_user_visible_requested(self, requested):
        self._user_visible_requested = bool(requested)
        if not self._user_visible_requested:
            self._temporarily_hidden_by_game = False
            self._release_event_hook()
            if self.position_timer.isActive():
                self.position_timer.stop()
            if self.visibility_timer.isActive():
                self.visibility_timer.stop()
            self.hide()
            if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
                self.app_window._sync_user_takeover_exclude_rects()
            self._update_toggle_button()
            return

        if not self.visibility_timer.isActive():
            self.visibility_timer.start()
        self.sync_game_visibility()

    def hide_by_user(self):
        self.set_user_visible_requested(False)

    def _update_toggle_button(self):
        if hasattr(self.app_window, "float_toggle_btn"):
            self.app_window.float_toggle_btn.setText("隐藏" if self._user_visible_requested else "悬浮窗")

    def sync_game_visibility(self):
        if not self._user_visible_requested:
            return
        if self._hidden_for_capture:
            if self.isVisible():
                self.hide()
            return

        rect = self._current_game_rect(allow_find=True)
        if rect is None:
            self._hide_until_game_visible()
            return

        self._temporarily_hidden_by_game = False
        self._update_toggle_button()
        if not self.isVisible():
            self.show()
            self.raise_()
        self.position_near_game(rect)

    def _hide_until_game_visible(self):
        self._temporarily_hidden_by_game = True
        self._last_target_pos = None
        self._release_event_hook()
        if self.isVisible():
            self.hide()
            if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
                self.app_window._sync_user_takeover_exclude_rects()
        self._update_toggle_button()

    def set_capture_hidden(self, hidden):
        hidden = bool(hidden)
        if self._hidden_for_capture == hidden:
            return
        self._hidden_for_capture = hidden
        if hidden:
            self._last_target_pos = None
            self._release_event_hook()
            if self.isVisible():
                self.hide()
        elif self._user_visible_requested:
            if not self.visibility_timer.isActive():
                self.visibility_timer.start()
            self.sync_game_visibility()
        if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
            self.app_window._sync_user_takeover_exclude_rects()

    def _current_game_rect(self, allow_find=False):
        wm = self.app_window.sm.wm
        rect = wm.get_client_rect()
        now = time.monotonic()
        if rect is None and allow_find and now - self._last_window_find > 1.2:
            self._last_window_find = now
            wm.find_window()
            rect = wm.get_client_rect()
        return rect

    def refresh_state(self):
        running = self.app_window.sm.is_running
        modules_ready = self.app_window.modules_ready
        modules_initializing = self.app_window.modules_initializing
        status_text = "运行中" if running else "待机中"
        status_color = APP_COLORS["accent_soft"] if running else APP_COLORS["text_dim"]
        dot_color = APP_COLORS["success"] if running else APP_COLORS["warning"]
        self.status_label.setText(f"<span style='color:{dot_color};'>●</span> {status_text}")
        self.status_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 16px;
                color: {status_color};
                font-size: 12px;
                font-weight: 900;
            }}
            """
        )
        if modules_initializing:
            self.start_btn.setText(self.app_window.init_button_text("▶ 初始化"))
        elif modules_ready:
            self.start_btn.setText("▶ 开始")
        else:
            self.start_btn.setText("▶ 初始化")
        self.start_btn.setEnabled(not running and not modules_initializing)
        self.stop_btn.setEnabled(running)
        self.start_btn.setStyleSheet(primary_button_stylesheet())
        self.stop_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(255, 82, 117, 0.16);
                color: {APP_COLORS['danger']};
                border: 1px solid rgba(255, 82, 117, 0.34);
                border-radius: 18px;
                min-height: 40px;
                padding: 0 14px;
                font-size: 13px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 82, 117, 0.26);
                color: {APP_COLORS['text']};
            }}
            QPushButton:disabled {{
                background-color: rgba(255, 255, 255, 0.035);
                color: {APP_COLORS['text_soft']};
                border: 1px solid rgba(111, 145, 182, 0.12);
            }}
            """
        )
        self.refresh_debug_visibility()

    def refresh_debug_visibility(self):
        self.refresh_panel_size()

    def _ensure_event_hook(self):
        if self._event_hook:
            return
        hwnd = self.app_window.sm.wm.hwnd
        if not hwnd:
            return

        def _callback(_hook, event, hwnd_event, id_object, _id_child, _event_thread, _event_time):
            if event != self._EVENT_OBJECT_LOCATIONCHANGE:
                return
            target_hwnd = self.app_window.sm.wm.hwnd
            if not hwnd_event or not target_hwnd:
                return
            if int(hwnd_event) != int(target_hwnd):
                return
            if id_object != self._OBJID_WINDOW:
                return
            if self._pending_hook_update:
                return
            self._pending_hook_update = True
            QTimer.singleShot(0, self._position_from_hook)

        self._event_callback = self._WinEventProc(_callback)
        user32 = ctypes.windll.user32
        try:
            user32.SetWinEventHook.restype = wintypes.HANDLE
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
                self._WinEventProc,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        except Exception:
            pass
        self._event_hook = user32.SetWinEventHook(
            self._EVENT_OBJECT_LOCATIONCHANGE,
            self._EVENT_OBJECT_LOCATIONCHANGE,
            0,
            self._event_callback,
            0,
            0,
            self._WINEVENT_OUTOFCONTEXT | self._WINEVENT_SKIPOWNPROCESS,
        )

    def _release_event_hook(self):
        if self._event_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._event_hook)
            except Exception:
                pass
        self._event_hook = None
        self._event_callback = None
        self._pending_hook_update = False

    def _position_from_hook(self):
        self._pending_hook_update = False
        self.position_near_game()

    def _move_to_target(self, target):
        if self._last_target_pos == target:
            return
        self._last_target_pos = target
        if self.pos() == target:
            return
        try:
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()),
                0,
                target.x(),
                target.y(),
                0,
                0,
                self._SWP_NOSIZE | self._SWP_NOZORDER | self._SWP_NOACTIVATE | self._SWP_ASYNCWINDOWPOS,
            )
        except Exception:
            self.move(target)

    def position_near_game(self, rect=None):
        if not self.isVisible():
            return

        if rect is None:
            rect = self._current_game_rect(allow_find=True)

        if rect:
            self._ensure_event_hook()
            left, top, _width, _height = rect
            target = QPoint(left + 16, top + 16)
        else:
            self._hide_until_game_visible()
            return

        self._move_to_target(target)
        if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
            self.app_window._sync_user_takeover_exclude_rects()

    def showEvent(self, event):
        super().showEvent(event)
        if self._user_visible_requested and not self.position_timer.isActive():
            self.position_timer.start()
        self.refresh_state()
        self.refresh_log_view()
        self.position_near_game()

    def set_debug_frame(self, frame):
        if frame is None or not self.app_window.config.get("debug_mode", False):
            return
        if self._collapsed or self._active_page_index != 0:
            return
        rgb_frame = frame[:, :, ::-1].copy()
        height, width, channel = rgb_frame.shape
        image = QImage(rgb_frame.data, width, height, channel * width, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image).scaled(
            self.debug_preview.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.debug_preview.setPixmap(pixmap)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._release_event_hook()
        if self.position_timer.isActive():
            self.position_timer.stop()
        self._update_toggle_button()

    def closeEvent(self, event):
        self._user_visible_requested = False
        if self.position_timer.isActive():
            self.position_timer.stop()
        if self.visibility_timer.isActive():
            self.visibility_timer.stop()
        self._release_event_hook()
        super().closeEvent(event)


class UsageAgreementDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.seconds_left = 3
        self._allow_reject = False

        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(860, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(11, 22, 36, 0.96);
                border: 1px solid rgba(89, 125, 164, 0.26);
                border-radius: 30px;
            }}
            """
        )
        add_shadow(shell, blur=34, alpha=120, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(6)

        title = QLabel("使用协议")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 32px; font-weight: 900;"
        )
        title_col.addWidget(title)

        subtitle = QLabel("请先阅读以下说明，再决定是否继续使用本程序。")
        subtitle.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px;"
        )
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        badge = QLabel("启动前确认")
        badge.setProperty("role", "accent-chip")
        badge.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(22, 209, 214, 0.12);
                color: {APP_COLORS['accent_soft']};
                border: 1px solid rgba(22, 209, 214, 0.28);
                border-radius: 15px;
                padding: 7px 14px;
                font-size: 12px;
                font-weight: 800;
            }}
            """
        )
        header.addWidget(badge, 0, Qt.AlignTop)
        layout.addLayout(header)

        agreement_box = QTextEdit()
        agreement_box.setReadOnly(True)
        agreement_box.setFocusPolicy(Qt.NoFocus)
        agreement_box.setStyleSheet(text_edit_stylesheet())
        agreement_box.setHtml(
            """
            <div style="font-family:'Microsoft YaHei UI'; line-height:1.65;">
              <p style="margin:0 0 12px 0; color:#F3F8FF; font-size:14px;">
                在继续使用本程序前，请你确认已经完整阅读并理解以下条款。点击“同意协议并开始”即视为你自愿接受全部内容。
              </p>
              <p style="margin:10px 0 6px 0; color:#FFFFFF; font-size:15px; font-weight:700;">1. 用途说明</p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                本程序仅用于图像识别、自动化控制流程学习与个人技术研究，不提供任何官方授权。请勿用于商业牟利、批量传播、代练代刷或其他破坏游戏公平性的用途。若你仅为测试或学习，请在下载、复制或接触本程序后的 24 小时内自行删除全部文件与副本。
              </p>
              <p style="margin:10px 0 6px 0; color:#FFFFFF; font-size:15px; font-weight:700;">2. 实现逻辑说明</p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                本程序当前采用屏幕截图、模板识别、窗口前台控制和键盘按键模拟等方式工作，用于识别钓鱼界面并触发对应操作。程序设计目标是不直接访问游戏内存、不注入 DLL、不加载驱动，也不修改游戏资源文件。
              </p>
              <p style="margin:10px 0 6px 0; color:#FFFFFF; font-size:15px; font-weight:700;">3. 风险提示</p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                即使本程序未主动访问游戏内存，也不能保证不会被游戏、平台或安全系统识别为异常自动化行为。使用本程序可能导致包括但不限于警告、限制、收益回收、临时封禁、永久封禁、账号异常、设备环境标记等风险。该类风险始终由使用者自行判断并承担。
              </p>
              <p style="margin:10px 0 6px 0; color:#FFFFFF; font-size:15px; font-weight:700;">4. 法律与协议责任</p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                你应自行确认所在地区法律法规、平台规则、游戏用户协议及社区规范是否允许此类工具存在或使用。若因安装、传播、改造、二次分发或实际运行本程序而引发任何法律纠纷、平台处罚、账号损失、设备损害或第三方索赔，责任均由实际使用者承担。
              </p>
              <p style="margin:10px 0 6px 0; color:#FFFFFF; font-size:15px; font-weight:700;">5. 使用者承诺</p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                你承诺仅在知情、自愿、可承担后果的前提下使用本程序；不会将其包装为收费产品、不会冒充官方工具、不会将其用于任何违法违规或侵害他人权益的行为；若你不同意本协议中的任一条款，请立即退出程序并停止使用。
              </p>
            </div>
            """
        )
        layout.addWidget(agreement_box, 1)

        footer = QHBoxLayout()
        footer.setSpacing(12)

        self.countdown_label = QLabel("请阅读协议后继续")
        self.countdown_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        footer.addWidget(self.countdown_label, 1, Qt.AlignVCenter)

        self.exit_button = QPushButton("退出程序")
        self.exit_button.setFocusPolicy(Qt.NoFocus)
        self.exit_button.setStyleSheet(secondary_button_stylesheet())
        self.exit_button.clicked.connect(self._reject_dialog)
        footer.addWidget(self.exit_button)

        self.accept_button = QPushButton()
        self.accept_button.setFocusPolicy(Qt.NoFocus)
        self.accept_button.setEnabled(False)
        self.accept_button.setStyleSheet(primary_button_stylesheet())
        self.accept_button.clicked.connect(self.accept)
        footer.addWidget(self.accept_button)
        layout.addLayout(footer)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._update_accept_text()

    def _update_accept_text(self):
        if self.seconds_left > 0:
            self.accept_button.setText(f"同意协议并开始（{self.seconds_left}s）")
            self.countdown_label.setText("请先阅读风险说明，按钮将在倒计时结束后启用。")
        else:
            self.accept_button.setText("同意协议并开始")
            self.countdown_label.setText("点击按钮即表示你已阅读并同意以上全部内容。")

    def _tick(self):
        self.seconds_left -= 1
        if self.seconds_left <= 0:
            self.seconds_left = 0
            self.timer.stop()
            self.accept_button.setEnabled(True)
        self._update_accept_text()

    def _reject_dialog(self):
        self._allow_reject = True
        super().reject()

    def reject(self):
        if self._allow_reject:
            super().reject()


class OpenSourceWarningDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._allow_reject = False

        self.setModal(True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(820, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(17, 21, 30, 0.98);
                border: 1px solid rgba(255, 112, 112, 0.32);
                border-radius: 30px;
            }
            """
        )
        add_shadow(shell, blur=34, alpha=140, offset=(0, 14))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("严正提醒")
        title.setStyleSheet(
            "background: transparent; border: none; color: #FFFFFF; font-size: 32px; font-weight: 900;"
        )
        layout.addWidget(title)

        warning_chip = QLabel("本程序开源免费发布")
        warning_chip.setStyleSheet(
            """
            QLabel {
                background-color: rgba(255, 102, 126, 0.14);
                color: #FF99A7;
                border: 1px solid rgba(255, 102, 126, 0.36);
                border-radius: 15px;
                padding: 7px 14px;
                font-size: 12px;
                font-weight: 900;
            }
            """
        )
        layout.addWidget(warning_chip, 0, Qt.AlignLeft)

        content = QTextEdit()
        content.setReadOnly(True)
        content.setFocusPolicy(Qt.NoFocus)
        content.setStyleSheet(text_edit_stylesheet())
        content.setHtml(
            """
            <div style="font-family:'Microsoft YaHei UI'; line-height:1.7;">
              <p style="margin:0 0 12px 0; color:#FFFFFF; font-size:15px; font-weight:800;">
                本程序为开源项目，永久免费发布。
              </p>
              <p style="margin:0 0 12px 0; color:#FFB4BC; font-size:14px; font-weight:700;">
                任何通过付费渠道、卡密渠道、代下渠道、网盘贩卖、二手转卖、打包收费等方式向你提供本程序的行为，均属于非法传播或恶意牟利。
              </p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                如果你是付费获得本程序，请立即停止继续向对方付款，并尽快申请退款、投诉或维权。你的权益已经受到侵害，出售者并不具备合法收费授权。
              </p>
              <p style="margin:0 0 10px 0; color:#9AB0CA; font-size:13px;">
                请仅从下方开源地址获取最新版本，避免下载被二次打包、植入风险代码或篡改内容的文件：
              </p>
              <p style="margin:6px 0 0 0; color:#67EAEC; font-size:13px; font-weight:800;">
                https://github.com/FADEDTUMI/YHoAutoFish
              </p>
            </div>
            """
        )
        layout.addWidget(content, 1)

        footer = QHBoxLayout()
        footer.setSpacing(12)

        link_button = QPushButton("打开开源地址")
        link_button.setFocusPolicy(Qt.NoFocus)
        link_button.setStyleSheet(secondary_button_stylesheet())
        link_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/FADEDTUMI/YHoAutoFish"))
        )
        footer.addWidget(link_button)

        footer.addStretch()

        exit_button = QPushButton("退出程序")
        exit_button.setFocusPolicy(Qt.NoFocus)
        exit_button.setStyleSheet(secondary_button_stylesheet())
        exit_button.clicked.connect(self._reject_dialog)
        footer.addWidget(exit_button)

        accept_button = QPushButton("我已知晓，继续使用")
        accept_button.setFocusPolicy(Qt.NoFocus)
        accept_button.setStyleSheet(primary_button_stylesheet())
        accept_button.clicked.connect(self.accept)
        footer.addWidget(accept_button)

        layout.addLayout(footer)

    def _reject_dialog(self):
        self._allow_reject = True
        super().reject()

    def reject(self):
        if self._allow_reject:
            super().reject()


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{APP_VERSION}")
        self.resize(1420, 920)
        self.setMinimumSize(1200, 760)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.default_config = {
            "tracking_strength": 180,
            "hold_threshold": 5,
            "deadzone_threshold": 1,
            "fishing_timeout": 180,
            "hook_wait_timeout": 90,
            "cast_animation_delay": 2,
            "settlement_close_delay": 1,
            "bar_missing_timeout": 3,
            "pre_control_timeout": 14,
            "recovery_timeout": 8,
            "fishing_result_check_interval": 0.65,
            "fishing_failed_check_interval": 1.25,
            "empty_ready_confirm_delay": 0.45,
            "bar_confidence_threshold": 0.45,
            "feed_forward_gain": 0.18,
            "safe_zone_ratio": 0.08,
            "control_release_cross_ratio": 0.012,
            "control_reengage_ratio": 0.018,
            "control_switch_ratio": 0.08,
            "control_min_hold_time": 0.14,
            "user_takeover_protection": True,
            "user_takeover_mouse_threshold": 12,
            "user_takeover_start_grace": 1.20,
            "update_startup_jitter_seconds": 20,
            "update_check_interval_minutes": 30,
            "log_line_limit": 320,
            "auto_switch_to_log": True,
            "debug_mode": False,
            "auto_sell_catch_threshold": 0,
            **MONTHLY_CARD_RESET_DEFAULT_CONFIG,
            "sponsor_button_hidden": False,
            "sponsor_qr_dir": "sponsor_qr",
        }
        self.config = dict(self.default_config)
        self.load_config()

        self.log_queue = queue.Queue()
        self.debug_queue = queue.Queue()
        self.log_deque = deque(maxlen=int(self.config.get("log_line_limit", 320)))
        self._log_version = 0
        self.sm = StateMachine(log_queue=self.log_queue, debug_queue=self.debug_queue)
        self._agreement_shown = False
        self.floating_window = None
        self._main_hidden_for_capture = False
        self._main_capture_was_visible = False
        self._main_capture_geometry = None
        self._main_capture_window_state = None
        self.modules_ready = False
        self.modules_initializing = False
        self.init_animation_step = 0
        self.ocr_init_worker = None
        self.update_info = None
        self.update_check_worker = None
        self.update_download_worker = None
        self.update_dialog = None
        self._update_check_manual_pending = False
        self._shutting_down = False
        self.update_poll_timer = QTimer(self)
        self.update_poll_timer.setSingleShot(True)
        self.update_poll_timer.timeout.connect(self._run_scheduled_update_check)
        self._settings_building = False
        self._settings_dirty = False
        self._settings_category_buttons = []
        self._settings_category_keys = {}
        self._setting_widgets = {}
        self._settings_saved_snapshot = {}
        self.monthly_card_reset_scheduler = MonthlyCardDailyResetScheduler()
        self._monthly_card_reset_in_progress = False
        self.auth_install_id = get_or_create_install_id()
        self.auth_device_hash = build_device_hash(self.auth_install_id)
        self.auth_device_hash_v2 = build_device_hash_v2()
        self.auth_state = load_auth_state()
        if self.auth_state.device_hash and self.auth_state.device_hash != self.auth_device_hash:
            self.auth_state = AuthState(status="unknown", device_hash=self.auth_device_hash, message="设备指纹已变化")
            save_auth_state(self.auth_state)
        elif not self.auth_state.device_hash:
            self.auth_state.device_hash = self.auth_device_hash
        # Restore cached remote hosts from previous session.
        # Note: cached_hosts was ECDSA-verified when first received from the server.
        # Re-restoration skips re-verification — acceptable because auth_state.dat is
        # DPAPI-protected (machine-bound), default hosts always take priority, and
        # TLS certificate validation provides an additional safety net.
        if self.auth_state.cached_hosts:
            try:
                cached = json.loads(self.auth_state.cached_hosts)
                if isinstance(cached, list) and cached:
                    update_remote_hosts(cached)
            except (json.JSONDecodeError, TypeError):
                pass
        # Populate license_expires_at from license file if not already set
        if not self.auth_state.license_expires_at:
            try:
                from core.auth_license import load_license
                license_blob = load_license()
                if license_blob:
                    payload = license_blob.get("payload") or {}
                    exp = float(payload.get("expires_at") or 0)
                    if exp > 0:
                        self.auth_state.license_expires_at = exp
                        self.write_log(f"[授权] 从 license 文件加载过期时间: {exp} (剩余 {int((exp - time.time()) / 86400)} 天)")
                    else:
                        self.write_log(f"[授权] license 文件 payload 无 expires_at: {list(payload.keys())}")
                else:
                    self.write_log("[授权] license 文件不存在或无法加载")
            except Exception as exc:
                self.write_log(f"[授权] 加载 license 文件异常: {exc}")
        self.auth_verified_this_session = False
        self.auth_manual_check_in_progress = False
        self._last_auth_check_reason = "scheduled"
        self._auth_dialog_dismissed_until = 0.0
        self._auth_failure_toast_at = {}
        self._last_auth_clock_skew_log_at = 0.0
        self.auth_check_worker = None
        self.auth_dialog = None
        self._cached_groups = None
        self._cached_groups_at = 0
        self.auth_ws_worker = None
        self._ws_status = "disconnected"

        self.init_ui()
        self._sync_runtime_preferences()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_queue)
        self.timer.start(100)

        self.monthly_card_reset_timer = QTimer(self)
        self.monthly_card_reset_timer.setTimerType(Qt.CoarseTimer)
        self.monthly_card_reset_timer.timeout.connect(self._check_monthly_card_daily_reset)
        self.monthly_card_reset_timer.start(15000)
        QTimer.singleShot(1000, self._check_monthly_card_daily_reset)

        self.auth_check_timer = QTimer(self)
        self.auth_check_timer.setTimerType(Qt.CoarseTimer)
        self._auth_check_initial_jitter_applied = False
        self.auth_check_timer.timeout.connect(self._run_scheduled_authorization_check)
        self._restart_auth_check_timer()

        self.init_animation_timer = QTimer(self)
        self.init_animation_timer.timeout.connect(self._tick_init_animation)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown_background_tasks)

        # Start WebSocket worker for V2 protocol real-time revocation push
        if getattr(self.auth_state, 'protocol_version', 1) >= 2 and self.auth_state.jwt_token:
            QTimer.singleShot(2000, self._start_websocket_worker)

        # Preload OCR dependencies in background (takes ~13s due to torch/rapidocr import)
        # so that recognition module initialization is near-instant when user starts fishing.
        threading.Thread(target=self._preload_ocr_deps, daemon=True).start()

    @staticmethod
    def _preload_ocr_deps():
        try:
            from cnocr import CnOcr  # noqa: F401
        except Exception:
            pass

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                self.config.update(json.load(file))
            self.config.pop("auth_api_base_url", None)
            self.config.pop("auth_required", None)
            self.config.pop("auth_check_interval_minutes", None)
            self.config.pop("auth_offline_grace_hours", None)
        except Exception as exc:
            print(f"Config load error: {exc}")

    def save_config(self):
        try:
            self.config.pop("auth_api_base_url", None)
            self.config.pop("auth_required", None)
            self.config.pop("auth_check_interval_minutes", None)
            self.config.pop("auth_offline_grace_hours", None)
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=4)
            self._sync_runtime_preferences()
            self.write_log("[配置] 高级设置已保存。")
            return True
        except Exception as exc:
            self.write_log(f"[配置] 保存失败: {exc}")
            return False

    def _save_config_silent(self):
        try:
            self.config.pop("auth_api_base_url", None)
            self.config.pop("auth_required", None)
            self.config.pop("auth_check_interval_minutes", None)
            self.config.pop("auth_offline_grace_hours", None)
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=4)
            return True
        except Exception as exc:
            print(f"Config save error: {exc}")
            return False

    def _initial_update_check_delay_ms(self):
        try:
            jitter = float(self.config.get("update_startup_jitter_seconds", 20))
        except (TypeError, ValueError):
            jitter = 20
        return int((0.9 + random.uniform(0, max(0.0, jitter))) * 1000)

    def _update_check_interval_ms(self):
        try:
            minutes = float(self.config.get("update_check_interval_minutes", 30))
        except (TypeError, ValueError):
            minutes = 30
        if minutes <= 0:
            return 0
        minutes = max(5.0, min(720.0, minutes))
        return int(minutes * 60 * 1000)

    def _next_update_poll_delay_ms(self):
        interval_ms = self._update_check_interval_ms()
        if interval_ms <= 0:
            return 0
        return int(interval_ms * random.uniform(0.85, 1.15))

    def _schedule_update_check(self, initial=False):
        if getattr(self, "_shutting_down", False):
            return
        if not hasattr(self, "update_poll_timer"):
            return
        if self.update_info is not None:
            self.update_poll_timer.stop()
            return
        delay_ms = self._initial_update_check_delay_ms() if initial else self._next_update_poll_delay_ms()
        if delay_ms <= 0:
            self.update_poll_timer.stop()
            return
        self.update_poll_timer.start(delay_ms)

    def _run_scheduled_update_check(self):
        if getattr(self, "_shutting_down", False):
            return
        if self.update_info is not None:
            return
        self.start_update_check(manual=False)

    def _auth_required(self):
        return auth_config_required(self.config)

    def _auth_base_url(self):
        return get_auth_base_url()

    def _auth_offline_grace_seconds(self):
        return auth_offline_grace_seconds(self.config)

    def _auth_decision(self):
        return decide_cached_authorization(self.config, self.auth_state)

    def _auth_is_allowed(self):
        return self._auth_decision().allowed

    def _auth_clock_skew_warning(self, state=None):
        state = state or self.auth_state
        try:
            skew = float(getattr(state, "clock_skew_seconds", 0) or 0)
        except (TypeError, ValueError):
            skew = 0.0
        if abs(skew) <= AUTH_CLOCK_SKEW_WARNING_SECONDS:
            return ""
        direction = "快" if skew > 0 else "慢"
        minutes = abs(skew) / 60.0
        return f"检测到本机时间比服务器时间{direction}约 {minutes:.1f} 分钟，已按服务器时间基准判断授权；建议同步系统时间。"

    def _auth_recovery_display_message(self, recovery, state=None):
        state = state or self.auth_state
        message = str(getattr(state, "message", "") or "").strip()
        if message in ("授权有效", "授权成功") and getattr(recovery, "mode", "") != "authorized":
            message = ""
        warning = self._auth_clock_skew_warning(state)
        if warning and getattr(recovery, "mode", "") in ("recheck_only", "transient_error"):
            return warning
        return message or str(getattr(recovery, "message", "") or "")

    def _log_auth_clock_skew_if_needed(self, state=None):
        state = state or self.auth_state
        if str(getattr(state, "status", "") or "") != "authorized":
            return
        warning = self._auth_clock_skew_warning(state)
        if not warning:
            return
        now = time.time()
        if now - float(getattr(self, "_last_auth_clock_skew_log_at", 0) or 0) < 600:
            return
        self._last_auth_clock_skew_log_at = now
        self.write_log(f"[来源验证] {warning}")

    def _auth_title_status(self):
        if not self._auth_required():
            return "验证关闭", "auth_offline", "来源验证未启用"
        decision = self._auth_decision()
        state = self.auth_state
        recovery = classify_auth_recovery(state)
        detail = self._auth_recovery_display_message(recovery, state) or str(decision.message or "")
        fixed_policy = "在线复验固定 5 分钟一次；离线宽限 12 小时。"
        warning = self._auth_clock_skew_warning(state)
        warning_part = f"{warning}" if warning else ""
        if getattr(self, "auth_manual_check_in_progress", False):
            return "复验中", "auth_offline", f"正在向授权服务器复验。{fixed_policy}"
        if decision.allowed:
            if self.auth_verified_this_session:
                license_info = ""
                if getattr(state, "license_expires_at", 0) > 0:
                    remaining = float(state.license_expires_at) - time.time()
                    if remaining > 0:
                        hours = int(remaining / 3600)
                        if hours >= 24:
                            license_info = f"离线授权剩余 {hours // 24} 天。"
                        else:
                            license_info = f"离线授权剩余 {hours} 小时。"
                    else:
                        grace_deadline = float(state.license_expires_at) + LICENSE_GRACE_DAYS * 86400
                        grace_remaining = grace_deadline - time.time()
                        if grace_remaining > 0:
                            license_info = f"离线授权已过期，宽限期剩余 {int(grace_remaining / 86400)} 天。"
                        else:
                            license_info = "离线授权已过期。"
                return "授权有效", "auth_ok", f"来源验证已通过。{license_info}{fixed_policy}{warning_part}"
            license_info = ""
            if getattr(state, "license_expires_at", 0) > 0:
                remaining = float(state.license_expires_at) - time.time()
                if remaining > 0:
                    license_info = f"离线授权剩余 {int(remaining / 86400)} 天。"
                else:
                    grace_deadline = float(state.license_expires_at) + LICENSE_GRACE_DAYS * 86400
                    grace_remaining = grace_deadline - time.time()
                    if grace_remaining > 0:
                        license_info = f"离线授权已过期，宽限期剩余 {int(grace_remaining / 86400)} 天。"
                    else:
                        license_info = "离线授权已过期。"
            return "待复验", "auth_offline", f"{license_info}本地授权缓存可用，正在等待在线复验。{fixed_policy}{warning_part}"
        if str(getattr(state, "status", "") or "") == "pending":
            return "待绑定", "auth_pending", f"绑定码已生成，请在指定 QQ 群完成 /bind。{fixed_policy}"
        return "未授权", "auth_bad", f"{detail or '需要完成来源验证'}。{fixed_policy}"

    # ---- WebSocket integration for V2 real-time revocation ----

    def _build_ws_url(self):
        """Build WSS URL from effective auth hosts. Prefer domain names over IPs."""
        from core.auth_policy import get_effective_hosts, is_ip_host
        hosts = get_effective_hosts()
        # Prefer non-IP hosts (domains work through Cloudflare Tunnel)
        for host in hosts:
            if not is_ip_host(host):
                return f"wss://{host}/ws/auth"
        # Fallback to first host
        if hosts:
            return f"wss://{hosts[0]}/ws/auth"
        return ""

    def _start_websocket_worker(self):
        """Start WebSocket worker for real-time revocation push."""
        if getattr(self, "_shutting_down", False):
            return
        if self.auth_ws_worker is not None:
            try:
                if self.auth_ws_worker.isRunning():
                    return
            except RuntimeError:
                self.auth_ws_worker = None
        if not self.auth_state.jwt_token:
            return
        license_id = str(getattr(self.auth_state, "license_id", "") or "")
        if not license_id:
            return
        ws_url = self._build_ws_url()
        if not ws_url:
            return
        self.auth_ws_worker = AuthWSWorker(ws_url, self.auth_state.jwt_token, license_id, parent=self)
        self.auth_ws_worker.revoked.connect(self._handle_ws_revocation)
        self.auth_ws_worker.status_changed.connect(self._handle_ws_status_change)
        self.auth_ws_worker.finished.connect(self.auth_ws_worker.deleteLater)
        self.auth_ws_worker.trigger_upgrade_received.connect(self._show_force_update_dialog)
        self.auth_ws_worker.cancel_force_update_received.connect(self._dismiss_force_update_dialog)
        self.auth_ws_worker.notification_received.connect(self._handle_ws_notification)
        self.auth_ws_worker.force_disconnect_received.connect(self._handle_ws_force_disconnect)
        self.auth_ws_worker.start()
        self.write_log("[auth-ws] WebSocket 工作线程已启动")

    def _handle_ws_revocation(self, license_id, reason):
        """Handle real-time revocation push from server."""
        self.write_log(f"[auth-ws] 收到吊销推送: license={license_id}, reason={reason}")
        state = AuthState.from_dict(self.auth_state.to_dict())
        state.status = "revoked"
        state.message = f"授权已被吊销: {reason}"
        self._apply_auth_state(state, persist=True)
        self._stop_websocket_worker()
        self._show_auth_failure_toast(f"授权已被吊销: {reason}", "danger")
        if self.sm.is_running:
            self.sm.stop()
            self.update_ui_on_stop()

    def _handle_ws_status_change(self, status):
        """Handle WebSocket connection status change."""
        self._ws_status = str(status or "disconnected")
        self._refresh_auth_title_button()

    def _stop_websocket_worker(self):
        """Stop WebSocket worker gracefully."""
        worker = getattr(self, "auth_ws_worker", None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.request_stop()
                worker.quit()
                if not worker.wait(2000):
                    print("[AppWindow] WebSocket 工作线程关闭超时", flush=True)
                    worker.terminate()
                    worker.wait(1000)
        except RuntimeError:
            pass
        finally:
            self.auth_ws_worker = None
            self._ws_status = "disconnected"

    def _refresh_auth_title_button(self):
        title_bar = getattr(self, "title_bar", None)
        if title_bar is not None and hasattr(title_bar, "sync_auth_status"):
            title_bar.sync_auth_status()

    def _restart_auth_check_timer(self):
        timer = getattr(self, "auth_check_timer", None)
        if timer is None:
            return
        if not self._auth_required():
            timer.stop()
            return
        interval_ms = auth_check_interval_seconds(self.config) * 1000
        if not getattr(self, "_auth_check_initial_jitter_applied", False):
            self._auth_check_initial_jitter_applied = True
            interval_ms += random.randint(0, 10000)
        timer.start(interval_ms)

    def _run_scheduled_authorization_check(self):
        timer = getattr(self, "auth_check_timer", None)
        interval_ms = auth_check_interval_seconds(self.config) * 1000
        if timer is not None and timer.interval() != interval_ms:
            timer.setInterval(interval_ms)
        self.start_authorization_check(silent=True, reason="scheduled")

    def _apply_auth_state(self, state, persist=False):
        self.auth_state = state if isinstance(state, AuthState) else AuthState.from_dict(state)
        if not self.auth_state.device_hash:
            self.auth_state.device_hash = self.auth_device_hash
        if persist:
            QTimer.singleShot(0, lambda s=self.auth_state: save_auth_state(s))
        self._log_auth_clock_skew_if_needed(self.auth_state)
        self.update_primary_buttons()
        self._refresh_auth_title_button()
        dialog = getattr(self, "auth_dialog", None)
        if dialog is not None and dialog.isVisible() and hasattr(dialog, "refresh_auth_state_view"):
            dialog.refresh_auth_state_view()
        # V2 WebSocket: update JWT or start WS if needed
        if getattr(self.auth_state, 'protocol_version', 1) >= 2 and self.auth_state.jwt_token:
            ws_worker = getattr(self, "auth_ws_worker", None)
            if ws_worker is not None:
                try:
                    running = ws_worker.isRunning()
                except RuntimeError:
                    ws_worker = None
                    running = False
                if running:
                    ws_worker.update_jwt(self.auth_state.jwt_token)
                elif not getattr(self, "_shutting_down", False):
                    QTimer.singleShot(1000, self._start_websocket_worker)
            elif not getattr(self, "_shutting_down", False):
                QTimer.singleShot(1000, self._start_websocket_worker)

    def mark_authorization_dialog_dismissed(self):
        self._auth_dialog_dismissed_until = time.time() + 60

    def _show_authorization_dialog(self, force=False, start_recheck=True):
        if not self._auth_required():
            return
        if not force and time.time() < float(getattr(self, "_auth_dialog_dismissed_until", 0) or 0):
            return
        if start_recheck and self.auth_state.access_token and (not self.auth_verified_this_session or not self._auth_decision().allowed):
            self.start_authorization_check(silent=True, reason="dialog")
        if self.auth_dialog is not None and self.auth_dialog.isVisible():
            self.auth_dialog.raise_()
            self.auth_dialog.activateWindow()
            return
        if self.auth_dialog is None:
            self.auth_dialog = AuthorizationDialog(self)
            self.auth_dialog.finished.connect(lambda _result: self.update_primary_buttons())
        self.auth_dialog.refresh_auth_state_view()
        if start_recheck:
            self.auth_dialog.recheck_authorization()
        self.auth_dialog.show()
        self.auth_dialog.raise_()
        self.auth_dialog.activateWindow()

    def ensure_authorized_for_action(self, action_label):
        if not self._auth_required():
            return True
        # Explicit status gate: reject revoked/released/deleted regardless of cache
        if self.auth_state.status in ("revoked", "released", "deleted", "suspended", "group_leave"):
            self.write_log(f"[来源验证] {action_label} 已拦截：status={self.auth_state.status}")
            self.show_toast("授权已停用，请联系管理员", "warning")
            self._show_authorization_dialog(force=True, start_recheck=False)
            return False
        decision = self._auth_decision()
        if decision.allowed and (self.auth_verified_this_session or self._verify_authorization_now_for_action(action_label)):
            return True
        self.write_log(f"[来源验证] {action_label} 已拦截：{decision.message}")
        self.show_toast("请先完成来源验证", "warning")
        self._show_authorization_dialog(force=True, start_recheck=False)
        return False

    def _verify_authorization_now_for_action(self, action_label):
        if not self.auth_state.access_token and not self.auth_state.jwt_token:
            return False
        try:
            client = AuthClient(self._auth_base_url(), timeout=8)
            if getattr(self.auth_state, 'protocol_version', 1) >= 2 and self.auth_state.jwt_token:
                result = client.check_entitlement_v2(
                    self.auth_state.jwt_token, self.auth_device_hash,
                    device_hash_v2=self.auth_device_hash_v2)
            else:
                result = client.check_entitlement(
                    self.auth_state.access_token, self.auth_device_hash)
        except Exception as exc:
            self.write_log(f"[来源验证] {action_label} 首次在线复验失败：{exc}")
            return False
        if not result.get("authorized"):
            state = AuthState.from_dict(self.auth_state.to_dict())
            recovery = classify_auth_recovery(self.auth_state, result, network_error=False)
            if recovery.should_persist:
                state.status = recovery.status
            state.message = recovery.message or str(result.get("message") or "授权校验未通过")
            self._apply_auth_state(state, persist=bool(recovery.should_persist))
            return False
        server_time = float(result.get("server_time") or time.time())
        state = AuthState(
            status="authorized",
            access_token=self.auth_state.access_token,
            license_id=str(result.get("license_id") or self.auth_state.license_id or ""),
            device_hash=self.auth_device_hash,
            qq_user_id_hash=str(result.get("qq_user_id_hash") or self.auth_state.qq_user_id_hash or ""),
            expires_at=float(result.get("expires_at") or self.auth_state.expires_at or 0),
            activation_id=self.auth_state.activation_id,
            user_code=str(result.get("user_code") or self.auth_state.user_code or ""),
            message="授权有效",
            refresh_token=self.auth_state.refresh_token,
            refresh_expires_at=self.auth_state.refresh_expires_at,
            license_data=self.auth_state.license_data,
            license_expires_at=self.auth_state.license_expires_at,
        )
        state.apply_check_timing(server_time)
        self.auth_verified_this_session = True
        if _process_hosts_update(result):
            state.cached_hosts = json.dumps(result.get("hosts", []))
        elif self.auth_state.cached_hosts:
            state.cached_hosts = self.auth_state.cached_hosts
        self._apply_auth_state(state, persist=True)
        return True

    def start_authorization_check(self, silent=True, reason="scheduled"):
        if getattr(self, "_shutting_down", False):
            return
        if not self._auth_required():
            return
        if self.auth_check_worker is not None and self.auth_check_worker.isRunning():
            return
        self._last_auth_check_reason = str(reason or "scheduled")
        if self._last_auth_check_reason == "manual":
            self.auth_manual_check_in_progress = True
            self._refresh_auth_title_button()
        base_url = self._auth_base_url()
        if not self.auth_state.access_token:
            self.auth_manual_check_in_progress = False
            self._refresh_auth_title_button()
            if not silent:
                self._show_authorization_dialog(force=reason in ("manual", "action_gate"), start_recheck=False)
            return
        # V2 protocol: use JWT-based check if protocol_version >= 2 and jwt_token exists
        if getattr(self.auth_state, 'protocol_version', 1) >= 2 and self.auth_state.jwt_token:
            self.auth_check_worker = AuthCheckWorkerV2(base_url, self.auth_state, self.auth_device_hash, device_hash_v2=self.auth_device_hash_v2, parent=self)
        else:
            self.auth_check_worker = AuthCheckWorker(base_url, self.auth_state, self.auth_device_hash, device_hash_v2=self.auth_device_hash_v2, parent=self)
        self.auth_check_worker.completed.connect(self._handle_authorization_check_result)
        self.auth_check_worker.finished.connect(self.auth_check_worker.deleteLater)
        self.auth_check_worker.start()

    def _show_force_update_dialog(self, min_version, update_url="", changelog=""):
        if getattr(self, "_force_update_dialog_shown", False):
            return
        self._force_update_dialog_shown = True
        self.write_log(f"[强制升级] 服务端要求最低版本 v{min_version}，当前版本 v{APP_VERSION}")
        dlg = ForceUpdateDialog(min_version, update_url=update_url, changelog=changelog, parent=self)
        self._force_update_dialog = dlg
        def _cleanup():
            self._force_update_dialog_shown = False
            self._force_update_dialog = None
        dlg.finished.connect(_cleanup)
        dlg.show()

    def _dismiss_force_update_dialog(self):
        """Dismiss force update dialog when server disables force upgrade."""
        dlg = getattr(self, "_force_update_dialog", None)
        if dlg:
            try:
                dlg.dismiss_dialog()
            except Exception as exc:
                self.write_log(f"[强制升级] 关闭对话框异常: {exc}")

    def _handle_ws_notification(self, message):
        self.write_log(f"[通知] {message}")
        StyledMessageDialog("服务端通知", message, tone="info", parent=self).exec()

    def _handle_ws_force_disconnect(self, reason):
        self.write_log(f"[强制下线] 原因: {reason}")
        if hasattr(self, 'auth_ws_worker') and self.auth_ws_worker:
            try:
                self.auth_ws_worker.request_stop()
            except RuntimeError:
                pass
        StyledMessageDialog("强制下线", f"管理员已将您强制下线。\n原因: {reason}", tone="danger", parent=self).exec()
        self.close()

    def _show_auth_failure_toast(self, message, tone="danger"):
        key = str(message or "auth_failure")[:120]
        now = time.time()
        last = float(self._auth_failure_toast_at.get(key, 0) or 0)
        if now - last < 60:
            return
        self._auth_failure_toast_at[key] = now
        self.show_toast(message, tone)

    def _handle_authorization_check_result(self, ok, state, message, network_error, result=None):
        if getattr(self, "_shutting_down", False):
            return
        reason = str(getattr(self, "_last_auth_check_reason", "scheduled") or "scheduled")
        self.auth_manual_check_in_progress = False
        self.auth_check_worker = None
        # V2 uses JWT for auth, not access_token -- skip stale-token check for V2
        if getattr(self.auth_state, 'protocol_version', 1) < 2:
            result_token = str(getattr(state, "access_token", "") or "")
            current_token = str(getattr(self.auth_state, "access_token", "") or "")
            if result_token and result_token != current_token:
                self.write_log("[来源验证] 已忽略过期授权复验结果。")
                self._refresh_auth_title_button()
                return
        if ok:
            self.auth_verified_this_session = True
            # Initialize EventTracker after successful auth
            try:
                EventTracker.get().init(
                    license_id=str(getattr(state, "license_id", "") or ""),
                    app_version=APP_VERSION,
                )
                # Attach WS send function if already connected
                ws_worker = getattr(self, "auth_ws_worker", None)
                if ws_worker is not None:
                    try:
                        ws_fn = getattr(ws_worker, "_ws_send_fn", None)
                        if ws_fn is not None:
                            EventTracker.get().update_ws_fn(ws_fn)
                    except Exception:
                        pass
                EventTracker.get().session_start()
            except Exception:
                pass
            if isinstance(result, dict) and _process_hosts_update(result):
                state.cached_hosts = json.dumps(result.get("hosts", []))
            elif self.auth_state.cached_hosts:
                state.cached_hosts = self.auth_state.cached_hosts
            # Save license from server response if present
            if isinstance(result, dict) and result.get("license"):
                self.write_log(f"[来源验证] 服务端返回 license 数据，正在保存...")
                try:
                    from core.auth_license import save_license
                    QTimer.singleShot(0, lambda lb=result["license"]: save_license(lb))
                    state.license_data = json.dumps(result["license"], ensure_ascii=False)
                    state.license_expires_at = float(
                        (result["license"].get("payload") or {}).get("expires_at")
                        or result.get("license_expires_at") or 0
                    )
                except Exception:
                    pass
            elif isinstance(result, dict):
                if not state.license_expires_at and self.auth_state.license_expires_at:
                    state.license_expires_at = self.auth_state.license_expires_at
            self._apply_auth_state(state, persist=True)
            self._refresh_auth_title_button()
            # V1→V2 migration: check if server returned JWT directly
            if isinstance(result, dict) and result.get("jwt_token") and getattr(state, 'protocol_version', 1) < 2:
                from core.auth_client import decode_jwt_payload
                jwt_payload = decode_jwt_payload(result["jwt_token"])
                state.jwt_token = str(result["jwt_token"])
                state.jwt_expires_at = float(jwt_payload.get("exp", 0)) if jwt_payload else float(result.get("jwt_expires_at", 0))
                state.jwt_issued_monotonic = time.monotonic()
                state.protocol_version = 2
                from core.auth_store import save_auth_state
                save_auth_state(state)
                self.write_log("[来源验证] V1→V2 迁移成功，已获取 JWT。")
                self.auth_state = state
                QTimer.singleShot(1000, self._start_websocket_worker)
            # Force update check
            if isinstance(result, dict) and result.get("force_update"):
                fu = result["force_update"]
                self._show_force_update_dialog(
                    fu.get("min_version", ""),
                    fu.get("update_url", ""),
                    fu.get("changelog", ""),
                )
            return
        recovery = classify_auth_recovery(self.auth_state, result or {}, network_error=network_error)
        is_v2 = getattr(self.auth_state, 'protocol_version', 1) >= 2
        if network_error and not is_v2 and self.auth_state.is_usable(time.time(), self._auth_offline_grace_seconds()):
            self.write_log(f"[来源验证] 在线复验失败，暂按离线宽限继续：{message}")
            self._refresh_auth_title_button()
            return
        if recovery.should_persist:
            updated = AuthState.from_dict(state.to_dict())
            updated.status = recovery.status
            updated.message = recovery.message or message
            self._apply_auth_state(updated, persist=True)
        else:
            preserved = AuthState.from_dict(self.auth_state.to_dict())
            preserved.message = recovery.message or message
            self._apply_auth_state(preserved, persist=not network_error)
        self.auth_verified_this_session = False
        diagnostic = result.get("diagnostic") if isinstance(result, dict) else {}
        device_records = diagnostic.get("device_records") if isinstance(diagnostic, dict) else []
        record_statuses = ",".join(str(item.get("status", "")) for item in device_records if isinstance(item, dict))
        self.write_log(f"[来源验证] 授权不可用：status={recovery.status}, recovery={recovery.mode}, records={record_statuses}, message={recovery.message or message}")
        if self.sm.is_running:
            self.sm.stop()
            self.update_ui_on_stop()
            self._show_auth_failure_toast("来源验证失败，功能已锁定", "danger")
        elif reason in ("manual", "action_gate"):
            self._show_auth_failure_toast(recovery.message or "来源验证失败", "warning")
        if should_show_authorization_dialog_after_check(
            reason,
            recovery.mode,
            getattr(self, "_auth_dialog_dismissed_until", 0),
            time.time(),
        ):
            self._show_authorization_dialog(force=reason in ("manual", "action_gate"), start_recheck=False)

    def show_authorization_status(self):
        if self.auth_state.access_token and (not self.auth_verified_this_session or not self.auth_state.user_code):
            self.start_authorization_check(silent=False, reason="manual")
        self._show_authorization_dialog(force=True)

    def _monthly_card_reset_enabled(self):
        snapshot = getattr(self, "_settings_saved_snapshot", {}) or {}
        if MONTHLY_CARD_RESET_ENABLED_KEY in snapshot:
            return bool(snapshot.get(MONTHLY_CARD_RESET_ENABLED_KEY))
        return bool(self.config.get(MONTHLY_CARD_RESET_ENABLED_KEY, False))

    def _save_monthly_card_reset_last_date(self, date_key):
        self.config[MONTHLY_CARD_RESET_LAST_DATE_KEY] = str(date_key or "")
        try:
            data = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                    if isinstance(loaded, dict):
                        data = loaded
            data[MONTHLY_CARD_RESET_LAST_DATE_KEY] = self.config[MONTHLY_CARD_RESET_LAST_DATE_KEY]
            if MONTHLY_CARD_RESET_ENABLED_KEY not in data:
                data[MONTHLY_CARD_RESET_ENABLED_KEY] = self._monthly_card_reset_enabled()
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=4)
            return True
        except Exception as exc:
            self.log_queue.put(f"[月卡复位] 记录每日触发日期失败: {exc}")
            return False

    def _check_monthly_card_daily_reset(self):
        if getattr(self, "_shutting_down", False):
            return
        if getattr(self, "_monthly_card_reset_in_progress", False):
            return
        if not self._monthly_card_reset_enabled():
            return

        last_date = self.config.get(MONTHLY_CARD_RESET_LAST_DATE_KEY, "")
        scheduler = self.monthly_card_reset_scheduler
        if not scheduler.should_trigger(True, last_date):
            return

        trigger_date = scheduler.date_key()
        self._monthly_card_reset_in_progress = True
        self.write_log("[月卡复位] 已到北京时间 05:02，准备执行 ESC、等待 2 秒、再 ESC。")
        thread = threading.Thread(
            target=self._run_monthly_card_daily_reset_sequence,
            args=(trigger_date,),
            daemon=True,
        )
        thread.start()

    def _run_monthly_card_daily_reset_sequence(self, trigger_date):
        try:
            ok = perform_double_escape_reset(
                self.sm.ctrl,
                window_manager=self.sm.wm,
                user_activity=self.sm.user_activity,
                input_lock=getattr(self.sm, "_input_lock", None),
                delay_seconds=2.0,
                tap_duration=0.12,
            )
            if ok:
                self._save_monthly_card_reset_last_date(trigger_date)
                self.log_queue.put("[月卡复位] 双 ESC 复位指令已完成。")
            else:
                self.log_queue.put("[月卡复位] 未找到或无法聚焦游戏窗口，本次未发送 ESC。")
        except Exception as exc:
            self.log_queue.put(f"[月卡复位] 执行失败: {exc}")
        finally:
            self._monthly_card_reset_in_progress = False

    def _sync_runtime_preferences(self):
        self.config["log_line_limit"] = int(self.config.get("log_line_limit", 320))
        self.log_deque = deque(self.log_deque, maxlen=self.config["log_line_limit"])
        self._log_version += 1
        if hasattr(self, "log_textbox"):
            self.log_textbox.setText("\n".join(self.log_deque))
        self._apply_state_machine_config()
        self._refresh_debug_view_state()
        self._restart_auth_check_timer()
        if self.floating_window is not None:
            self.floating_window.refresh_state()
            self.floating_window.refresh_log_view(force=True)

    def _apply_state_machine_config(self):
        self.sm.update_config("tracking_strength", self.config.get("tracking_strength", 180))
        self.sm.update_config("t_hold", self.config.get("hold_threshold", 5))
        self.sm.update_config("t_deadzone", self.config.get("deadzone_threshold", 1))
        self.sm.update_config("fishing_timeout", self.config.get("fishing_timeout", 180))
        self.sm.update_config("hook_wait_timeout", self.config.get("hook_wait_timeout", 90))
        self.sm.update_config("cast_animation_delay", self.config.get("cast_animation_delay", 2))
        self.sm.update_config("settlement_close_delay", self.config.get("settlement_close_delay", 1))
        self.sm.update_config("bar_missing_timeout", self.config.get("bar_missing_timeout", 3))
        self.sm.update_config("pre_control_timeout", self.config.get("pre_control_timeout", 14))
        self.sm.update_config("recovery_timeout", self.config.get("recovery_timeout", 8))
        self.sm.update_config("fishing_result_check_interval", self.config.get("fishing_result_check_interval", 0.65))
        self.sm.update_config("fishing_failed_check_interval", self.config.get("fishing_failed_check_interval", 1.25))
        self.sm.update_config("empty_ready_confirm_delay", self.config.get("empty_ready_confirm_delay", 0.45))
        self.sm.update_config("bar_confidence_threshold", self.config.get("bar_confidence_threshold", 0.45))
        self.sm.update_config("feed_forward_gain", self.config.get("feed_forward_gain", 0.18))
        self.sm.update_config("safe_zone_ratio", self.config.get("safe_zone_ratio", 0.08))
        self.sm.update_config("control_release_cross_ratio", self.config.get("control_release_cross_ratio", 0.012))
        self.sm.update_config("control_reengage_ratio", self.config.get("control_reengage_ratio", 0.018))
        self.sm.update_config("control_switch_ratio", self.config.get("control_switch_ratio", 0.08))
        self.sm.update_config("control_min_hold_time", self.config.get("control_min_hold_time", 0.14))
        self.sm.update_config("user_takeover_protection", self.config.get("user_takeover_protection", True))
        self.sm.update_config("user_takeover_mouse_threshold", self.config.get("user_takeover_mouse_threshold", 12))
        self.sm.update_config("user_takeover_start_grace", self.config.get("user_takeover_start_grace", 1.20))
        self.sm.update_config("debug_mode", self.config.get("debug_mode", False))
        self.sm.update_config("auto_sell_catch_threshold", self.config.get("auto_sell_catch_threshold", 0))

    def _refresh_debug_view_state(self):
        if not hasattr(self, "debug_preview"):
            return

        if self.config.get("debug_mode", False):
            self.debug_state_label.setText("调试溜鱼视图已开启")
            if self.debug_preview.pixmap() is None:
                self.debug_preview.setText("等待溜鱼画面...")
            self.debug_help_label.setText("开始钓鱼后将实时显示识别到的绿条、黄条与中心位置。")
        else:
            self.debug_state_label.setText("调试溜鱼视图未开启")
            self.debug_preview.clear()
            self.debug_preview.setText("当前未开启")
            self.debug_help_label.setText("如需反馈识别问题，请在高级设置中开启调试溜鱼视图后再复现问题。")

    def _set_debug_frame(self, frame):
        if not hasattr(self, "debug_preview"):
            return
        if frame is None or not self.config.get("debug_mode", False):
            return

        rgb_frame = frame[:, :, ::-1].copy()
        height, width, channel = rgb_frame.shape
        image = QImage(rgb_frame.data, width, height, channel * width, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image).scaled(
            self.debug_preview.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.debug_preview.setPixmap(pixmap)
        if self.floating_window is not None and self.floating_window.isVisible():
            self.floating_window.set_debug_frame(frame)

    def init_ui(self):
        central = QWidget()
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(0)

        self.shell = BackdropFrame()
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        root.addWidget(self.shell)

        self.title_bar = CustomTitleBar(self)
        shell_layout.addWidget(self.title_bar)

        content = QWidget()
        content.setStyleSheet(
            """
            QWidget {
                background: transparent;
                border-bottom-left-radius: 32px;
                border-bottom-right-radius: 32px;
            }
            """
        )
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 18)
        content_layout.setSpacing(18)
        shell_layout.addWidget(content, 1)

        self.sidebar = self._build_sidebar()
        content_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")
        content_layout.addWidget(self.stack, 1)

        self.page_record = FishingRecordWidget(self.sm.record_mgr)
        self.page_encyclopedia = None
        self.page_encyclopedia_placeholder = QWidget()
        self.page_encyclopedia_placeholder.setStyleSheet("background: transparent;")
        self.page_log = self._build_log_page()
        self.page_settings = self._build_settings_page()

        self.stack.addWidget(self.page_record)
        self.stack.addWidget(self.page_encyclopedia_placeholder)
        self.stack.addWidget(self.page_log)
        self.stack.addWidget(self.page_settings)
        self.switch_page(0, self.nav_record)

        self.toast = ToastPopup(self)
        self.update_primary_buttons()

    def _ensure_encyclopedia_page(self):
        if self.page_encyclopedia is not None:
            return self.page_encyclopedia

        page = EncyclopediaWidget(self.sm.record_mgr)
        placeholder_index = self.stack.indexOf(self.page_encyclopedia_placeholder)
        if placeholder_index < 0:
            placeholder_index = 1
        self.stack.removeWidget(self.page_encyclopedia_placeholder)
        self.page_encyclopedia_placeholder.deleteLater()
        self.stack.insertWidget(placeholder_index, page)
        self.page_encyclopedia = page
        return page

    def toggle_maximize_restore(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        if hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._agreement_shown:
            self._agreement_shown = True
            QTimer.singleShot(120, self.show_usage_agreement)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.reposition()

    def closeEvent(self, event):
        try:
            EventTracker.get().session_end("app_close")
        except Exception:
            pass
        self.shutdown_background_tasks()
        super().closeEvent(event)

    def shutdown_background_tasks(self):
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True

        for timer_name in ("update_poll_timer", "timer", "monthly_card_reset_timer", "auth_check_timer", "init_animation_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None and timer.isActive():
                timer.stop()

        if self.sm.is_running:
            self.sm.stop()

        if self.floating_window is not None:
            self.floating_window.close()

        self._stop_worker_thread("update_download_worker", "更新下载线程", wait_ms=1200)
        self._stop_worker_thread("update_check_worker", "更新检查线程", wait_ms=1200)
        self._stop_worker_thread("auth_check_worker", "授权校验线程", wait_ms=1200)
        self._stop_worker_thread("ocr_init_worker", "识别初始化线程", wait_ms=1800)
        self._stop_websocket_worker()

    def _stop_worker_thread(self, attr_name, label, wait_ms=1200, terminate_wait_ms=800):
        worker = getattr(self, attr_name, None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                if not worker.wait(wait_ms):
                    print(f"[AppWindow] {label}关闭超时，正在强制结束。", flush=True)
                    worker.terminate()
                    worker.wait(terminate_wait_ms)
        except RuntimeError:
            pass
        finally:
            setattr(self, attr_name, None)

    def show_usage_agreement(self):
        self.agreement_dialog = UsageAgreementDialog(self)
        dialog = self.agreement_dialog
        dialog.finished.connect(self._handle_agreement_result)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def _handle_agreement_result(self, result):
        if result != QDialog.Accepted:
            self.close()
            return
        self.show_open_source_warning()

    def show_open_source_warning(self):
        self.source_warning_dialog = OpenSourceWarningDialog(self)
        dialog = self.source_warning_dialog
        dialog.finished.connect(self._handle_source_warning_result)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def _handle_source_warning_result(self, result):
        if result != QDialog.Accepted:
            self.close()
            return
        if self._auth_required():
            if self._auth_is_allowed():
                self.start_authorization_check(silent=True, reason="startup")
            else:
                self._show_authorization_dialog()
        self._schedule_update_check(initial=True)

    def show_about_dialog(self):
        self.about_dialog = AboutDialog(self)
        dialog = self.about_dialog
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def show_qq_group_dialog(self):
        self.qq_group_dialog = QQGroupDialog(self)
        dialog = self.qq_group_dialog
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def _sponsor_qr_directories(self):
        base_dir = os.path.dirname(CONFIG_FILE)
        candidates = []
        env_dir = os.environ.get("YHO_SPONSOR_QR_DIR", "").strip()
        if env_dir:
            candidates.append(env_dir)
        configured = str(self.config.get("sponsor_qr_dir", "") or "").strip()
        if configured:
            candidates.append(configured if os.path.isabs(configured) else os.path.join(base_dir, configured))
        candidates.append(os.path.join(base_dir, "sponsor_qr"))
        candidates.append(resource_path("sponsor_qr"))
        seen = set()
        result = []
        for item in candidates:
            normalized = os.path.abspath(item)
            key = os.path.normcase(normalized)
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result

    def _load_sponsor_qr_images(self):
        preferred = (
            ("微信", ("微信.jpg", "微信.png", "wechat.jpg", "wechat.png")),
            ("支付宝", ("支付宝.jpg", "支付宝.png", "alipay.jpg", "alipay.png")),
        )
        found = []
        for directory in self._sponsor_qr_directories():
            if not os.path.isdir(directory):
                continue
            for label, names in preferred:
                for name in names:
                    path = os.path.join(directory, name)
                    if os.path.exists(path):
                        found.append((label, path))
                        break
            if found:
                return found
        return found

    def show_sponsor_dialog(self):
        self.sponsor_dialog = SponsorDialog(self._load_sponsor_qr_images(), self)
        dialog = self.sponsor_dialog
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def confirm_hide_sponsor_button(self):
        self.sponsor_hide_dialog = SponsorHideDialog(self)
        dialog = self.sponsor_hide_dialog

        def apply_choice(result):
            if result == QDialog.Accepted:
                self.config["sponsor_button_hidden"] = True
                self._save_config_silent()
                title_bar = getattr(self, "title_bar", None)
                if title_bar is not None:
                    title_bar.sync_sponsor_visibility()
                self._sync_sponsor_visibility_setting_button()
                if hasattr(self, "_settings_saved_snapshot"):
                    self._refresh_settings_saved_snapshot()
                    self._set_settings_dirty(False)

        dialog.finished.connect(apply_choice)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def _set_update_checking(self, checking):
        title_brand = getattr(getattr(self, "title_bar", None), "title_brand", None)
        if title_brand is not None:
            title_brand.set_update_checking(checking)

    def start_update_check(self, manual=False):
        if getattr(self, "_shutting_down", False):
            return
        if self.update_info is not None and not manual:
            return
        if manual:
            self._update_check_manual_pending = True
            self._set_update_checking(True)
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            return
        if not manual and hasattr(self, "update_poll_timer"):
            self.update_poll_timer.stop()
        if self.update_info is None:
            self._set_update_checking(True)
        self.update_check_worker = UpdateCheckWorker(self)
        self.update_check_worker.completed.connect(self._handle_update_check_result)
        self.update_check_worker.finished.connect(self.update_check_worker.deleteLater)
        self.update_check_worker.start()

    def _handle_update_check_result(self, update_info, error):
        if getattr(self, "_shutting_down", False):
            return
        self.update_check_worker = None
        manual = self._update_check_manual_pending
        self._update_check_manual_pending = False
        if update_info is None:
            self.update_info = None
            self._set_update_checking(False)
            if error:
                if manual:
                    self.write_log(f"[更新] 检查更新失败: {error}")
                    self.show_toast("检查更新失败，请稍后重试", "danger")
            elif manual:
                self.show_toast("当前已是最新版本", "success")
            self._schedule_update_check(initial=False)
            return
        self.update_info = update_info
        if hasattr(self, "update_poll_timer"):
            self.update_poll_timer.stop()
        title_brand = getattr(getattr(self, "title_bar", None), "title_brand", None)
        if title_brand is not None:
            title_brand.set_update_info(update_info)
        self.write_log(f"[更新] 发现新版 v{update_info.version}，可点击标题栏版本号更新。")
        self.show_toast(f"发现新版 v{update_info.version}", "success")

    def show_update_dialog(self):
        if getattr(self, "_shutting_down", False):
            return
        if self.update_info is None:
            self.show_toast("正在检查更新", "info")
            self.start_update_check(manual=True)
            return
        if self.update_dialog is not None and self.update_dialog.isVisible():
            self.update_dialog.raise_()
            self.update_dialog.activateWindow()
            return
        confirm_dialog = UpdatePolicyConfirmDialog(self.update_info, self, self)
        confirm_dialog.move(self.geometry().center() - confirm_dialog.rect().center())
        if confirm_dialog.exec() != QDialog.Accepted:
            return
        self.update_dialog = UpdateDialog(self.update_info, self, self)
        dialog = self.update_dialog
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.open()

    def start_auto_update(self, dialog):
        if getattr(self, "_shutting_down", False):
            return
        if not self.ensure_authorized_for_action("安装更新"):
            dialog.set_error("请先完成来源验证，再安装更新。")
            return
        if self.update_info is None:
            dialog.set_error("当前没有可安装的更新信息。")
            return
        if self.update_download_worker is not None and self.update_download_worker.isRunning():
            return
        dialog.set_busy(True)
        self.update_download_worker = UpdateDownloadWorker(self.update_info, source=dialog.selected_update_source, parent=self)
        self.update_download_worker.progress.connect(dialog.set_progress)
        self.update_download_worker.completed.connect(lambda ok, path, error: self._handle_update_download_result(ok, path, error, dialog))
        self.update_download_worker.finished.connect(self.update_download_worker.deleteLater)
        self.update_download_worker.start()

    def cancel_auto_update(self, dialog=None):
        worker = self.update_download_worker
        if worker is None or not worker.isRunning():
            if dialog is not None:
                dialog.set_cancelled()
            return
        if dialog is not None:
            dialog.set_canceling()
        try:
            if hasattr(worker, "cancel_download"):
                worker.cancel_download()
            else:
                worker.requestInterruption()
        except RuntimeError:
            if dialog is not None:
                dialog.set_cancelled()

    def _handle_update_download_result(self, ok, path, error, dialog):
        if getattr(self, "_shutting_down", False):
            return
        self.update_download_worker = None
        if not ok:
            if "取消" in (error or ""):
                dialog.set_cancelled(error or "更新下载已取消。")
                self.write_log("[更新] 下载已取消，未完成的更新文件已清理。")
                return
            dialog.set_error(error or "更新包下载失败。")
            return
        try:
            start_external_update(path, main_pid=os.getpid(), version=self.update_info.version)
        except Exception as exc:
            dialog.set_error(str(exc))
            return

        dialog.set_installing_started()
        self.write_log("[更新] 更新包已下载，正在退出并交由独立更新器安装。")
        if self.sm.is_running:
            self.sm.stop()
        if self.floating_window is not None:
            self.floating_window.close()
        QTimer.singleShot(500, self._quit_for_update_install)

    def _quit_for_update_install(self):
        if self.sm.is_running:
            self.sm.stop()
        if self.floating_window is not None:
            self.floating_window.close()
        QApplication.closeAllWindows()
        QApplication.quit()

    def show_toast(self, text, tone="info"):
        if hasattr(self, "toast"):
            self.toast.show_message(text, tone)

    def init_button_text(self, prefix="初始化模块"):
        dots = "." * ((self.init_animation_step % 3) + 1)
        return f"{prefix}{dots}"

    def update_primary_buttons(self):
        running = self.sm.is_running
        if self._auth_required() and not self._auth_is_allowed():
            self.btn_start.setText("完成来源验证")
            self.btn_start.setEnabled(not running and not self.modules_initializing)
        elif self.modules_initializing:
            self.btn_start.setText(self.init_button_text("初始化模块"))
            self.btn_start.setEnabled(False)
        elif self.modules_ready:
            self.btn_start.setText("开始钓鱼")
            self.btn_start.setEnabled(not running)
        else:
            self.btn_start.setText("初始化模块")
            self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if self.floating_window is not None:
            self.floating_window.refresh_state()
        self._refresh_auth_title_button()

    def _tick_init_animation(self):
        self.init_animation_step += 1
        self.update_primary_buttons()

    def handle_primary_action(self):
        if self.sm.is_running:
            return
        if not self.ensure_authorized_for_action("启动自动钓鱼"):
            return
        if not self.modules_ready:
            self.start_module_initialization()
            return
        self.start_bot()

    def start_module_initialization(self):
        if getattr(self, "_shutting_down", False):
            return
        if not self.ensure_authorized_for_action("初始化识别模块"):
            return
        if self.modules_ready:
            self.update_primary_buttons()
            return
        if self.modules_initializing:
            return
        self.modules_initializing = True
        self.init_animation_step = 0
        self.init_animation_timer.start(360)
        self.update_primary_buttons()
        self.write_log("[系统] 开始初始化识别模块与视觉模板预热...")
        self.show_toast("正在初始化识别模块", "info")

        self.ocr_init_worker = RecognitionInitWorker(self.sm, self)
        self.ocr_init_worker.completed.connect(self._handle_module_init_result)
        self.ocr_init_worker.progress.connect(self._handle_module_init_progress)
        self.ocr_init_worker.finished.connect(self.ocr_init_worker.deleteLater)
        self.ocr_init_worker.start()

    def _handle_module_init_result(self, ok, message):
        if getattr(self, "_shutting_down", False):
            return
        self.init_animation_timer.stop()
        self.modules_initializing = False
        self.modules_ready = bool(ok)
        self.update_primary_buttons()
        self.write_log(f"[系统] {message}")
        toast_message = message if ok else "识别模块初始化失败，详情已写入运行日志。"
        self.show_toast(toast_message, "success" if ok else "danger")
        self.ocr_init_worker = None

    def _handle_module_init_progress(self, message):
        """显示初始化子步骤的进度日志，预热失败时显示警告。"""
        self.write_log(f"[初始化] {message}")
        if "预热步骤失败" in message or "失败" in message:
            self.show_toast("部分预热步骤失败，首帧处理可能较慢", "warning")

    def show_usage_policy(self):
        html = """
        <div style="font-family:'Microsoft YaHei UI'; line-height:1.72;">
          <p style="color:#F3F8FF; font-size:14px; font-weight:800;">使用范围</p>
          <p style="color:#9AB0CA; font-size:13px;">本程序仅用于图像识别、自动化控制流程学习与个人技术研究。请勿用于商业牟利、代练代刷、批量传播或破坏游戏公平性的用途。</p>
          <p style="color:#F3F8FF; font-size:14px; font-weight:800;">实现方式</p>
          <p style="color:#9AB0CA; font-size:13px;">程序通过屏幕截图、模板识别、OCR 与键盘模拟完成自动钓鱼流程，不主动读取或修改游戏内存，不注入 DLL，不修改游戏资源文件。</p>
          <p style="color:#F3F8FF; font-size:14px; font-weight:800;">风险说明</p>
          <p style="color:#9AB0CA; font-size:13px;">即使未访问游戏内存，自动化行为仍可能被平台风控识别。由此产生的警告、限制、封禁、账号异常或其他损失，均由使用者自行承担。</p>
          <p style="color:#F3F8FF; font-size:14px; font-weight:800;">学习声明</p>
          <p style="color:#9AB0CA; font-size:13px;">如仅为测试或学习，请在下载、复制或接触本程序后的 24 小时内自行删除全部文件与副本。</p>
        </div>
        """
        dialog = PolicyDialog("用户协议", "查看程序使用范围、实现方式与风险提示。", html, self)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.exec()

    def show_anti_infringement_policy(self):
        html = """
        <div style="font-family:'Microsoft YaHei UI'; line-height:1.72;">
          <p style="color:#FFB4BC; font-size:15px; font-weight:900;">本程序开源免费发布，任何付费出售、卡密售卖、网盘倒卖、二次打包收费均不是作者授权行为。</p>
          <p style="color:#9AB0CA; font-size:13px;">如果你从付费渠道获得本程序，说明你的权益可能已经受到侵犯。请停止继续付款，尽快申请退款、投诉或维权。</p>
          <p style="color:#F3F8FF; font-size:14px; font-weight:800;">唯一建议来源</p>
          <p style="color:#67EAEC; font-size:13px; font-weight:800;">https://github.com/FADEDTUMI/YHoAutoFish</p>
          <p style="color:#9AB0CA; font-size:13px;">从非开源渠道下载的文件可能被植入风险代码、篡改配置或夹带无关内容。请优先从开源仓库获取并核对项目说明。</p>
        </div>
        """
        dialog = PolicyDialog("反侵权协议", "提醒用户识别非法付费传播，保护自己的下载与使用权益。", html, self)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.exec()

    def toggle_floating_window(self):
        if self.floating_window is None:
            self.floating_window = FloatingControlWindow(self)

        if self.floating_window.user_visible_requested:
            self.floating_window.set_user_visible_requested(False)
            return

        self.floating_window.refresh_state()
        self.floating_window.set_user_visible_requested(True)

    def _build_sidebar(self):
        panel = QFrame()
        panel.setFixedWidth(294)
        panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(10, 20, 35, 0.70);
                border: 1px solid rgba(62, 92, 123, 0.22);
                border-radius: 32px;
            }
            """
        )
        add_shadow(panel, blur=34, alpha=110, offset=(0, 12))

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        logo_card = LogoImageCard()
        add_shadow(logo_card, blur=24, alpha=96, offset=(0, 10))
        layout.addWidget(logo_card, 0, Qt.AlignHCenter)

        nav_panel = QFrame()
        nav_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.58);
                border: 1px solid rgba(111, 145, 182, 0.14);
                border-radius: 26px;
            }
            """
        )
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(12, 12, 12, 12)
        nav_layout.setSpacing(8)

        self.nav_record = NavButton("钓鱼记录")
        self.nav_encyclopedia = NavButton("图鉴记录")
        self.nav_log = NavButton("运行日志")
        self.nav_settings = NavButton("高级设置")

        self.nav_record.clicked.connect(lambda: self.switch_page(0, self.nav_record))
        self.nav_encyclopedia.clicked.connect(lambda: self.switch_page(1, self.nav_encyclopedia))
        self.nav_log.clicked.connect(lambda: self.switch_page(2, self.nav_log))
        self.nav_settings.clicked.connect(lambda: self.switch_page(3, self.nav_settings))

        for button in [self.nav_record, self.nav_encyclopedia, self.nav_log, self.nav_settings]:
            nav_layout.addWidget(button)
        layout.addWidget(nav_panel)

        control_panel = QFrame()
        control_panel.setMinimumHeight(238)
        control_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.66);
                border: 1px solid rgba(111, 145, 182, 0.16);
                border-radius: 26px;
            }
            """
        )
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(18, 18, 18, 18)
        control_layout.setSpacing(12)

        status_title = QLabel("运行状态")
        status_title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;")
        control_layout.addWidget(status_title)

        self.status_chip = StatusChip()
        control_layout.addWidget(self.status_chip)

        control_hint = QLabel("可在主程序或游戏内悬浮窗控制开始与停止。")
        control_hint.setWordWrap(True)
        control_hint.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;")
        control_layout.addWidget(control_hint)

        self.btn_start = QPushButton("开始钓鱼")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setFocusPolicy(Qt.NoFocus)
        self.btn_start.setStyleSheet(primary_button_stylesheet())
        self.btn_start.clicked.connect(self.handle_primary_action)
        control_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止运行")
        self.btn_stop.setMinimumHeight(42)
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setStyleSheet(secondary_button_stylesheet())
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_bot)
        control_layout.addWidget(self.btn_stop)
        layout.addWidget(control_panel)

        layout.addStretch()

        author_panel = QFrame()
        author_panel.setMinimumHeight(96)
        author_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.52);
                border: 1px solid rgba(111, 145, 182, 0.14);
                border-radius: 20px;
            }
            """
        )
        author_layout = QHBoxLayout(author_panel)
        author_layout.setContentsMargins(12, 12, 12, 12)
        author_layout.setSpacing(8)

        author_text_col = QVBoxLayout()
        author_text_col.setSpacing(5)
        author_text_col.setContentsMargins(0, 0, 0, 0)

        author = QLabel("作者：FADEDTUMI")
        author.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px; font-weight: 700;")
        author_text_col.addWidget(author)

        author_note = QLabel("开源免费 · 学习研究用途")
        author_note.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 11px;")
        author_text_col.addWidget(author_note)

        link_row = QHBoxLayout()
        link_row.setSpacing(4)

        github = QPushButton("GitHub")
        github.setCursor(Qt.PointingHandCursor)
        github.setFocusPolicy(Qt.NoFocus)
        github.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: {APP_COLORS['accent_soft']};
                border: none;
                text-align: left;
                padding: 0;
                font-size: 12px;
                font-weight: 800;
            }}
            QPushButton:hover {{
                color: {APP_COLORS['text']};
            }}
            """
        )
        github.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/FADEDTUMI")))
        github.setFixedWidth(50)
        link_row.addWidget(github, 0, Qt.AlignLeft)

        agreement = QPushButton("用户协议")
        agreement.setCursor(Qt.PointingHandCursor)
        agreement.setFocusPolicy(Qt.NoFocus)
        agreement.setStyleSheet(github.styleSheet())
        agreement.setFixedWidth(52)
        agreement.clicked.connect(self.show_usage_policy)
        link_row.addWidget(agreement, 0, Qt.AlignLeft)

        anti_abuse = QPushButton("反侵权")
        anti_abuse.setCursor(Qt.PointingHandCursor)
        anti_abuse.setFocusPolicy(Qt.NoFocus)
        anti_abuse.setStyleSheet(github.styleSheet())
        anti_abuse.setFixedWidth(42)
        anti_abuse.clicked.connect(self.show_anti_infringement_policy)
        link_row.addWidget(anti_abuse, 0, Qt.AlignLeft)
        link_row.addStretch()
        author_text_col.addLayout(link_row)
        author_layout.addLayout(author_text_col, 1)

        self.float_toggle_btn = QPushButton("悬浮窗")
        self.float_toggle_btn.setFixedSize(62, 56)
        self.float_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.float_toggle_btn.setFocusPolicy(Qt.NoFocus)
        self.float_toggle_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(22, 209, 214, 0.10);
                color: {APP_COLORS['accent_soft']};
                border: 1px solid rgba(22, 209, 214, 0.34);
                border-radius: 18px;
                font-size: 12px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(22, 209, 214, 0.20);
                color: {APP_COLORS['text']};
            }}
            """
        )
        self.float_toggle_btn.clicked.connect(self.toggle_floating_window)
        author_layout.addWidget(self.float_toggle_btn, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(author_panel)
        return panel

    def _build_log_page(self):
        page = QFrame()
        page.setProperty("variant", "elevated")
        page.setStyleSheet(panel_stylesheet())

        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("运行日志")
        title.setProperty("role", "headline")
        layout.addWidget(title)

        subtitle = QLabel("查看自动钓鱼的实时状态、异常提示与关键步骤输出。")
        subtitle.setProperty("role", "subtle")
        layout.addWidget(subtitle)

        content_row = QHBoxLayout()
        content_row.setSpacing(18)

        self.log_textbox = QTextEdit()
        self.log_textbox.setReadOnly(True)
        self.log_textbox.setStyleSheet(text_edit_stylesheet())
        self.log_textbox.append("--- 异环自动钓鱼初始化完成 ---\n请确保游戏窗口处于可操作状态。")
        content_row.addWidget(self.log_textbox, 5)

        debug_panel = QFrame()
        debug_panel.setProperty("variant", "soft")
        debug_panel.setStyleSheet(panel_stylesheet())
        add_shadow(debug_panel, blur=20, alpha=85, offset=(0, 8))
        debug_panel.setFixedWidth(340)

        debug_layout = QVBoxLayout(debug_panel)
        debug_layout.setContentsMargins(16, 16, 16, 16)
        debug_layout.setSpacing(10)

        debug_title = QLabel("调试溜鱼视图")
        debug_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        debug_layout.addWidget(debug_title)

        debug_note = QLabel("用于回看溜鱼阶段识别到的绿条、黄条位置，方便定位识别问题。")
        debug_note.setWordWrap(True)
        debug_note.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        debug_layout.addWidget(debug_note)

        self.debug_state_label = QLabel()
        self.debug_state_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 12px; font-weight: 700;"
        )
        debug_layout.addWidget(self.debug_state_label)

        self.debug_preview = QLabel()
        self.debug_preview.setAlignment(Qt.AlignCenter)
        self.debug_preview.setMinimumSize(300, 118)
        self.debug_preview.setStyleSheet(
            """
            background-color: rgba(8, 15, 24, 0.78);
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 18px;
            """
        )
        debug_layout.addWidget(self.debug_preview)

        self.debug_help_label = QLabel()
        self.debug_help_label.setWordWrap(True)
        self.debug_help_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        debug_layout.addWidget(self.debug_help_label)
        debug_layout.addStretch()

        content_row.addWidget(debug_panel, 2)
        layout.addLayout(content_row, 1)
        self._refresh_debug_view_state()
        return page

    def _build_settings_page(self):
        self._settings_building = True
        self._setting_widgets = {}
        self._settings_category_buttons = []
        self._settings_category_keys = {}

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        content = QFrame()
        content.setProperty("variant", "elevated")
        content.setStyleSheet(panel_stylesheet())
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 22)
        content_layout.setSpacing(16)

        title = QLabel("高级设置")
        title.setProperty("role", "headline")
        content_layout.addWidget(title)

        subtitle = QLabel("按用途查看和调整自动钓鱼参数。建议只修改当前遇到问题对应的分类，保存后立即应用。")
        subtitle.setProperty("role", "subtle")
        content_layout.addWidget(subtitle)

        body_row = QHBoxLayout()
        body_row.setSpacing(18)

        category_panel = QFrame()
        category_panel.setProperty("variant", "soft")
        category_panel.setFixedWidth(220)
        category_panel.setStyleSheet(panel_stylesheet())
        category_layout = QVBoxLayout(category_panel)
        category_layout.setContentsMargins(14, 14, 14, 14)
        category_layout.setSpacing(8)

        category_title = QLabel("设置分类")
        category_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 15px; font-weight: 900;"
        )
        category_layout.addWidget(category_title)

        category_note = QLabel("选择左侧分类后，只显示对应参数。")
        category_note.setWordWrap(True)
        category_note.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 11px;"
        )
        category_layout.addWidget(category_note)

        self.settings_category_layout = category_layout
        self.settings_stack = QStackedWidget()
        self.settings_stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")

        fishing_page, fishing_layout = self._build_settings_category_page(
            "溜鱼控制",
            "控制游标追随耐力条的力度、死区、前馈和按键切换逻辑。这里的参数会直接影响溜鱼手感。",
        )
        fishing_keys = []
        self.slider_hold = self._settings_block(
            fishing_layout,
            "跟鱼力度",
            "数值越大，PID 修正和前馈追赶越积极；程序会保持按键到接近越过中心，适配较慢的 A/D 移速。",
            self.config.get("tracking_strength", 180),
            70,
            240,
            "tracking_strength",
        )
        fishing_keys.append("tracking_strength")
        self.slider_hold_threshold = self._settings_block(
            fishing_layout,
            "稳定释放阈值",
            "游标接近绿条中心时重新触发按键的阈值；默认偏低，减少在中心一侧提前滑行。",
            self.config.get("hold_threshold", 5),
            2,
            30,
            "hold_threshold",
        )
        fishing_keys.append("hold_threshold")
        self.slider_deadzone = self._settings_block(
            fishing_layout,
            "跟鱼死区",
            "数值越小，游标出现约 1 像素偏离就会更快按键追赶；最低档会更频繁左右修正。",
            self.config.get("deadzone_threshold", 1),
            1,
            15,
            "deadzone_threshold",
        )
        fishing_keys.append("deadzone_threshold")
        self.slider_feed_forward = self._settings_block(
            fishing_layout,
            "预测追赶前馈",
            "根据耐力条移动趋势提前补偿 A/D 按键。值越大越主动，过大可能造成越过中心后的摆动。",
            self.config.get("feed_forward_gain", 0.18),
            0,
            45,
            "feed_forward_gain",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("feed_forward_gain")
        self.slider_safe_zone = self._settings_block(
            fishing_layout,
            "中心安全区宽度",
            "按耐力条宽度计算的安全区。值越小越贴近中心追鱼，值越大越稳但反应会更保守。",
            self.config.get("safe_zone_ratio", 0.08),
            4,
            28,
            "safe_zone_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("safe_zone_ratio")
        self.slider_release_cross = self._settings_block(
            fishing_layout,
            "过中心释放比例",
            "游标越过目标中心后释放当前方向的比例阈值。较低值会更快停手，适合现在较慢的官方移速。",
            self.config.get("control_release_cross_ratio", 0.012),
            1,
            12,
            "control_release_cross_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_release_cross_ratio")
        self.slider_reengage = self._settings_block(
            fishing_layout,
            "重新接管比例",
            "释放后再次按键追赶的比例阈值。数值越低，左右修正越频繁，响应越快。",
            self.config.get("control_reengage_ratio", 0.018),
            1,
            18,
            "control_reengage_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_reengage_ratio")
        self.slider_switch = self._settings_block(
            fishing_layout,
            "反向切换比例",
            "误差超过该比例时允许直接换方向追赶。值越小越灵敏，值越大越少来回切换。",
            self.config.get("control_switch_ratio", 0.08),
            4,
            25,
            "control_switch_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_switch_ratio")
        self.slider_min_hold = self._settings_block(
            fishing_layout,
            "最短按键保持",
            "每次 A/D 按下后的最短保持时间。值越小越灵敏，值过小可能增加高频抖动。",
            self.config.get("control_min_hold_time", 0.14),
            3,
            35,
            "control_min_hold_time",
            value_scale=0.01,
            display_scale=0.01,
            display_suffix="s",
            display_decimals=2,
            config_decimals=3,
        )
        fishing_keys.append("control_min_hold_time")
        fishing_layout.addStretch()
        self._add_settings_category("溜鱼控制", fishing_page, fishing_keys)

        timing_page, timing_layout = self._build_settings_category_page(
            "流程与超时",
            "控制抛竿、等待、恢复和结算阶段的等待时间。用于适配机器性能、网络延迟和游戏动画速度。",
        )
        timing_keys = []
        self.slider_timeout = self._settings_block(
            timing_layout,
            "防卡死超时",
            "单次钓鱼等待超过该时长时自动重置，避免界面停在异常状态。",
            self.config.get("fishing_timeout", 180),
            60,
            300,
            "fishing_timeout",
        )
        timing_keys.append("fishing_timeout")
        self.slider_hook_wait_timeout = self._settings_block(
            timing_layout,
            "上钩等待超时",
            "抛竿后超过该时长仍未识别到上钩提示时，回到待机重新检测，避免永久卡住。",
            self.config.get("hook_wait_timeout", 90),
            30,
            180,
            "hook_wait_timeout",
        )
        timing_keys.append("hook_wait_timeout")
        self.slider_bar_missing = self._settings_block(
            timing_layout,
            "耐力条丢失容忍",
            "耐力条短暂识别不到时等待的秒数；画面抖动或帧率低可适当调大。",
            self.config.get("bar_missing_timeout", 3),
            1,
            5,
            "bar_missing_timeout",
        )
        timing_keys.append("bar_missing_timeout")
        self.slider_cast_delay = self._settings_block(
            timing_layout,
            "抛竿动画等待",
            "按下抛竿键后等待动画完成的秒数；机器或网络较慢时可适当调大。",
            self.config.get("cast_animation_delay", 2),
            1,
            5,
            "cast_animation_delay",
        )
        timing_keys.append("cast_animation_delay")
        self.slider_close_delay = self._settings_block(
            timing_layout,
            "结算关闭等待",
            "捕获后按 ESC 关闭结算界面，再等待回到可抛竿状态的秒数。",
            self.config.get("settlement_close_delay", 1),
            1,
            5,
            "settlement_close_delay",
        )
        timing_keys.append("settlement_close_delay")
        self.slider_pre_control_timeout = self._settings_block(
            timing_layout,
            "上钩后进入溜鱼超时",
            "识别上钩并按 F 后，超过该秒数仍未进入有效溜鱼控制时自动恢复。",
            self.config.get("pre_control_timeout", 14),
            10,
            30,
            "pre_control_timeout",
        )
        timing_keys.append("pre_control_timeout")
        self.slider_recovery_timeout = self._settings_block(
            timing_layout,
            "异常恢复超时",
            "失败或异常后等待回到可抛竿界面的最长秒数，超过后会尝试轻量恢复。",
            self.config.get("recovery_timeout", 8),
            4,
            20,
            "recovery_timeout",
        )
        timing_keys.append("recovery_timeout")
        timing_layout.addStretch()
        self._add_settings_category("流程与超时", timing_page, timing_keys)

        auto_sell_page, auto_sell_layout = self._build_settings_category_page(
            "自动售鱼",
            "达到设定钓获数量后，程序会在下一次回到可抛竿界面时进入鱼获出售界面并执行一键出售。设为 0 表示关闭。",
        )
        auto_sell_keys = []
        self.slider_auto_sell_threshold = self._settings_block(
            auto_sell_layout,
            "累计钓获自动售鱼",
            "从点击开始钓鱼后开始计数；达到该数量后只会在确认钓鱼初始界面时触发售鱼，避免在结算、溜鱼或异常恢复中误操作。",
            self.config.get("auto_sell_catch_threshold", 0),
            0,
            999,
            "auto_sell_catch_threshold",
            display_suffix="条",
        )
        auto_sell_keys.append("auto_sell_catch_threshold")
        auto_sell_layout.addStretch()
        self._add_settings_category("自动售鱼", auto_sell_page, auto_sell_keys)

        monthly_card_page, monthly_card_layout = self._build_settings_category_page(
            "月卡复位",
            "仅供已开通游戏内月卡的用户手动开启。开启后，程序会按北京时间每天 05:02 执行一次双 ESC 复位。",
        )
        monthly_card_keys = []
        self.monthly_card_reset_button = self._settings_toggle_block(
            monthly_card_layout,
            "每日 05:02 双 ESC 复位",
            "确认自己是游戏内月卡用户后再开启。开启后，每个北京时间日期最多触发一次：先按 ESC，等待 2 秒，再按 ESC，帮助回到钓鱼初始界面。",
            self.config.get(MONTHLY_CARD_RESET_ENABLED_KEY, False),
            MONTHLY_CARD_RESET_ENABLED_KEY,
        )
        monthly_card_keys.append(MONTHLY_CARD_RESET_ENABLED_KEY)
        monthly_card_layout.addStretch()
        self._add_settings_category("月卡复位", monthly_card_page, monthly_card_keys)

        auth_page, auth_layout = self._build_settings_category_page(
            "来源验证",
            "服务器授权地址由客户端代码固定，在线复验与离线宽限由程序内置策略控制，用户无法通过设置或配置文件改写。",
        )
        self._settings_readonly_block(
            auth_layout,
            "在线复验间隔",
            "程序运行期间固定每 5 分钟向服务器静默复验一次。用户退群、授权吊销或设备超限后，会在下一次复验时锁定功能。",
            "5min",
        )
        self._settings_readonly_block(
            auth_layout,
            "离线宽限时间",
            "服务器不可达时，已授权用户可在最近一次成功复验后继续使用最多 12 小时；超过后必须联网恢复。",
            "12小时",
        )
        self._settings_action_buttons_block(
            auth_layout,
            "绑定与群入口",
            "查看当前来源验证状态，或打开包含机器人提示和官方群入口的来源验证窗口。",
            (
                ("查看绑定状态", self.show_authorization_status),
                ("打开群列表", self._show_authorization_dialog),
            ),
        )
        auth_layout.addStretch()
        self._add_settings_category("来源验证", auth_page, [])

        recognition_page, recognition_layout = self._build_settings_category_page(
            "识别与判定",
            "控制耐力条置信度和结算/失败检测频率。数值越激进，响应越快；数值越保守，误判风险越低。",
        )
        recognition_keys = []
        self.slider_bar_confidence = self._settings_block(
            recognition_layout,
            "耐力条最低置信度",
            "低于该置信度时会复用短时间历史位置或忽略结果，降低树林等背景色误参与的风险。",
            self.config.get("bar_confidence_threshold", 0.45),
            25,
            85,
            "bar_confidence_threshold",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        recognition_keys.append("bar_confidence_threshold")
        self.slider_result_interval = self._settings_block(
            recognition_layout,
            "成功结算检测间隔",
            "溜鱼阶段检测成功结算特征的间隔。越小越快识别结算，但截图匹配频率会更高。",
            self.config.get("fishing_result_check_interval", 0.65),
            7,
            30,
            "fishing_result_check_interval",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("fishing_result_check_interval")
        self.slider_failed_interval = self._settings_block(
            recognition_layout,
            "失败横幅检测间隔",
            "检测鱼儿溜走提示的间隔。越小越快恢复，但会增加失败模板匹配频率。",
            self.config.get("fishing_failed_check_interval", 1.25),
            14,
            60,
            "fishing_failed_check_interval",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("fishing_failed_check_interval")
        self.slider_empty_ready_delay = self._settings_block(
            recognition_layout,
            "空杆回到初始确认",
            "看到初始界面后继续确认的时长，用于区分空杆和短暂结算切换。",
            self.config.get("empty_ready_confirm_delay", 0.45),
            5,
            60,
            "empty_ready_confirm_delay",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("empty_ready_confirm_delay")
        recognition_layout.addStretch()
        self._add_settings_category("识别与判定", recognition_page, recognition_keys)

        safety_page, safety_layout = self._build_settings_category_page(
            "安全接管",
            "当用户点击游戏窗口或在游戏内按键时，程序会释放按键并暂停，避免和用户操作冲突。",
        )
        safety_keys = []
        self.takeover_protection_button = self._settings_toggle_block(
            safety_layout,
            "用户接管自动暂停",
            "开启后，只检测游戏窗口内点击和游戏内键盘操作；移动鼠标不会触发暂停，悬浮窗点击也会被排除。",
            self.config.get("user_takeover_protection", True),
            "user_takeover_protection",
        )
        safety_keys.append("user_takeover_protection")
        self.slider_takeover_start_grace = self._settings_block(
            safety_layout,
            "启动接管宽限",
            "点击开始后短暂忽略用户输入，避免启动瞬间的鼠标点击被误认为接管。",
            self.config.get("user_takeover_start_grace", 1.20),
            0,
            50,
            "user_takeover_start_grace",
            value_scale=0.1,
            display_scale=0.1,
            display_suffix="s",
            display_decimals=1,
            config_decimals=2,
        )
        safety_keys.append("user_takeover_start_grace")
        safety_layout.addStretch()
        self._add_settings_category("安全接管", safety_page, safety_keys)

        display_page, display_layout = self._build_settings_category_page(
            "界面与日志",
            "控制运行日志保留量、启动后页面跳转、更新检查频率和调试溜鱼视图。调试视图会增加少量界面刷新开销。",
        )
        display_keys = []
        self.slider_log_limit = self._settings_block(
            display_layout,
            "日志保留条数",
            "控制运行日志页保留的最近输出数量，数值越大可回看更多记录。",
            self.config.get("log_line_limit", 320),
            120,
            800,
            "log_line_limit",
        )
        display_keys.append("log_line_limit")
        self.auto_log_button = self._settings_toggle_block(
            display_layout,
            "启动后跳转运行日志",
            "开启后，点击开始钓鱼会自动切换到运行日志页，方便观察实时状态。",
            self.config.get("auto_switch_to_log", True),
            "auto_switch_to_log",
        )
        display_keys.append("auto_switch_to_log")
        self.sponsor_button_visible_button = self._sponsor_visibility_settings_block(
            display_layout,
            "显示请喝咖啡入口",
            "关闭标题栏赞助入口后，可在这里重新显示。该开关只影响标题栏入口，不影响自动钓鱼、记录和图鉴功能。",
            not bool(self.config.get("sponsor_button_hidden", False)),
            "sponsor_button_hidden",
        )
        display_keys.append("sponsor_button_hidden")
        self.debug_view_button = self._settings_toggle_block(
            display_layout,
            "调试溜鱼视图",
            "开启后，运行日志页会显示溜鱼阶段的实时识别画面，便于排查识别异常与反馈问题。",
            self.config.get("debug_mode", False),
            "debug_mode",
        )
        display_keys.append("debug_mode")
        self.slider_update_interval = self._settings_block(
            display_layout,
            "自动检查更新间隔",
            "程序启动后会检查一次，运行期间按此间隔轮询静态 latest.json。手动检查始终立即执行，发现新版后会停止后台轮询。",
            self.config.get("update_check_interval_minutes", 30),
            10,
            180,
            "update_check_interval_minutes",
            display_suffix="min",
        )
        display_keys.append("update_check_interval_minutes")
        display_layout.addStretch()
        self._add_settings_category("界面与日志", display_page, display_keys)

        category_layout.addStretch()
        body_row.addWidget(category_panel)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_scroll.setStyleSheet(scroll_area_stylesheet())
        self.settings_scroll.viewport().setStyleSheet("background: transparent;")
        self.settings_scroll.viewport().setAutoFillBackground(False)
        self.settings_scroll.setWidget(self.settings_stack)
        body_row.addWidget(self.settings_scroll, 1)
        content_layout.addLayout(body_row, 1)

        action_bar = QFrame()
        self.settings_action_bar = action_bar
        action_bar.setObjectName("settingsActionBar")
        action_bar.setMinimumHeight(74)
        self._apply_settings_action_bar_style(False)
        add_shadow(action_bar, blur=22, alpha=90, offset=(0, 6))
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(18, 12, 18, 12)
        action_layout.setSpacing(12)

        self.settings_status_label = QLabel("当前设置已保存")
        self.settings_status_label.setWordWrap(True)
        self.settings_status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        action_layout.addWidget(self.settings_status_label, 1)

        self.reset_settings_btn = QPushButton("恢复当前分类推荐值")
        self.reset_settings_btn.setFocusPolicy(Qt.NoFocus)
        self.reset_settings_btn.setStyleSheet(secondary_button_stylesheet())
        self.reset_settings_btn.clicked.connect(self._reset_current_settings_category)
        action_layout.addWidget(self.reset_settings_btn)

        self.save_settings_btn = QPushButton("保存并应用")
        self.save_settings_btn.setFocusPolicy(Qt.NoFocus)
        self.save_settings_btn.setStyleSheet(primary_button_stylesheet())
        self.save_settings_btn.clicked.connect(self._save_settings)
        action_layout.addWidget(self.save_settings_btn)

        content_layout.addWidget(action_bar)

        layout.addWidget(content, 1)
        if self._settings_category_buttons:
            self._switch_settings_category(0)
        self._settings_building = False
        self._refresh_settings_saved_snapshot()
        self._set_settings_dirty(False)
        return page

    def _build_settings_category_page(self, title, description):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 20px; font-weight: 900;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(description)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)
        return page, layout

    def _add_settings_category(self, title, page, keys):
        index = self.settings_stack.addWidget(page)
        self._settings_category_keys[index] = list(keys)
        button = SettingsCategoryButton(title)
        button.clicked.connect(lambda _checked=False, category_index=index: self._switch_settings_category(category_index))
        self.settings_category_layout.addWidget(button)
        self._settings_category_buttons.append(button)
        return button

    def _switch_settings_category(self, index):
        if not hasattr(self, "settings_stack"):
            return
        self.settings_stack.setCurrentIndex(index)
        for button_index, button in enumerate(self._settings_category_buttons):
            button.setChecked(button_index == index)
        if hasattr(self, "settings_scroll"):
            QTimer.singleShot(0, lambda: self.settings_scroll.verticalScrollBar().setValue(0))

    def _apply_settings_action_bar_style(self, dirty):
        if not hasattr(self, "settings_action_bar"):
            return
        if dirty:
            bg = "rgba(43, 33, 15, 0.96)"
            border = "rgba(241, 190, 103, 0.74)"
        else:
            bg = "rgba(13, 31, 45, 0.96)"
            border = "rgba(99, 228, 228, 0.34)"
        self.settings_action_bar.setStyleSheet(
            f"""
            QFrame#settingsActionBar {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 20px;
            }}
            """
        )

    def _settings_panel(self):
        block = QFrame()
        block.setProperty("variant", "soft")
        block.setStyleSheet(panel_stylesheet())
        add_shadow(block, blur=20, alpha=85, offset=(0, 8))
        return block

    def _settings_readonly_block(self, parent_layout, title, note, value_text):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        top.addWidget(title_label)
        top.addStretch()
        value_label = QLabel(value_text)
        value_label.setAlignment(Qt.AlignCenter)
        value_label.setMinimumWidth(74)
        value_label.setStyleSheet(
            f"""
            QLabel {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.10);
                border: 1px solid rgba(29, 208, 214, 0.22);
                border-radius: 14px;
                padding: 6px 10px;
                font-size: 18px;
                font-weight: 900;
            }}
            """
        )
        top.addWidget(value_label)
        layout.addLayout(top)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)
        parent_layout.addWidget(block)
        return block

    def _settings_action_buttons_block(self, parent_layout, title, note, actions):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)

        row = QHBoxLayout()
        row.setSpacing(10)
        for index, (text, callback) in enumerate(actions or ()):
            button = QPushButton(str(text))
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(primary_button_stylesheet() if index == 0 else secondary_button_stylesheet())
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        parent_layout.addWidget(block)
        return block

    def _settings_block(
        self,
        parent_layout,
        title,
        note,
        value,
        minimum,
        maximum,
        key,
        value_scale=1.0,
        display_scale=None,
        display_suffix="",
        display_decimals=0,
        config_decimals=None,
    ):
        block = self._settings_panel()

        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;")
        top.addWidget(title_label)
        top.addStretch()

        display_scale = value_scale if display_scale is None else display_scale
        try:
            slider_value = int(round(float(value) / float(value_scale)))
        except (TypeError, ValueError, ZeroDivisionError):
            slider_value = int(minimum)
        slider_value = max(int(minimum), min(int(maximum), slider_value))

        value_label = QLabel(self._format_slider_label(slider_value, display_scale, display_suffix, display_decimals))
        value_label.setStyleSheet(
            f"""
            QLabel {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.10);
                border: 1px solid rgba(29, 208, 214, 0.22);
                border-radius: 14px;
                padding: 6px 10px;
                font-size: 18px;
                font-weight: 900;
            }}
            """
        )
        value_label.setMinimumWidth(74)
        value_label.setAlignment(Qt.AlignCenter)
        top.addWidget(value_label)
        layout.addLayout(top)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;")
        layout.addWidget(note_label)

        slider = NoWheelSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(slider_value)
        slider.setFocusPolicy(Qt.NoFocus)
        slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                background-color: rgba(255, 255, 255, 0.08);
                border-radius: 5px;
                height: 10px;
            }}
            QSlider::sub-page:horizontal {{
                background-color: rgba(29, 208, 214, 0.82);
                border-radius: 5px;
                height: 10px;
            }}
            QSlider::handle:horizontal {{
                background-color: #B8FFFF;
                border: 2px solid rgba(29, 208, 214, 0.92);
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }}
            """
        )
        slider.valueChanged.connect(
            lambda new_value,
            label=value_label,
            config_key=key,
            cfg_scale=value_scale,
            disp_scale=display_scale,
            suffix=display_suffix,
            disp_decimals=display_decimals,
            cfg_decimals=config_decimals: self._update_slider_value(
                label,
                config_key,
                new_value,
                cfg_scale,
                disp_scale,
                suffix,
                disp_decimals,
                cfg_decimals,
            )
        )
        layout.addWidget(slider)
        parent_layout.addWidget(block)
        self._setting_widgets[key] = {
            "type": "slider",
            "widget": slider,
            "value_scale": value_scale,
            "display_scale": display_scale,
            "display_suffix": display_suffix,
            "display_decimals": display_decimals,
            "config_decimals": config_decimals,
        }
        self.config[key] = self._config_from_slider_value(slider_value, value_scale, config_decimals)
        return slider

    def _settings_toggle_block(self, parent_layout, title, note, checked, key):
        block = self._settings_panel()
        layout = QHBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        text_col = QVBoxLayout()
        text_col.setSpacing(6)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        text_col.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        text_col.addWidget(note_label)
        layout.addLayout(text_col, 1)

        button = QPushButton()
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setChecked(bool(checked))
        button.setStyleSheet(secondary_button_stylesheet())
        button.setMinimumWidth(86)
        button.toggled.connect(
            lambda is_checked, cfg_key=key, btn=button: self._update_toggle_value(btn, cfg_key, is_checked)
        )
        self._update_toggle_value(button, key, bool(checked))
        layout.addWidget(button)

        parent_layout.addWidget(block)
        self._setting_widgets[key] = {"type": "toggle", "widget": button}
        return button

    def _settings_text_block(self, parent_layout, title, note, value, key, placeholder=""):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)

        line_edit = QLineEdit(str(value or ""))
        line_edit.setPlaceholderText(placeholder)
        line_edit.setMinimumHeight(42)
        line_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(111, 145, 182, 0.22);
                border-radius: 14px;
                color: {APP_COLORS['text']};
                padding: 0 14px;
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 1px solid rgba(29, 208, 214, 0.62);
                background-color: rgba(255, 255, 255, 0.07);
            }}
            """
        )
        line_edit.textChanged.connect(lambda text, cfg_key=key: self._update_text_value(cfg_key, text))
        layout.addWidget(line_edit)

        parent_layout.addWidget(block)
        self._setting_widgets[key] = {"type": "text", "widget": line_edit}
        return line_edit

    def _sponsor_visibility_settings_block(self, parent_layout, title, note, visible, key):
        block = self._settings_panel()
        layout = QHBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        text_col = QVBoxLayout()
        text_col.setSpacing(6)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        text_col.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        text_col.addWidget(note_label)
        layout.addLayout(text_col, 1)

        button = QPushButton()
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setChecked(bool(visible))
        button.setStyleSheet(secondary_button_stylesheet())
        button.setMinimumWidth(86)
        button.toggled.connect(
            lambda is_checked, cfg_key=key, btn=button: self._update_sponsor_visibility_value(btn, cfg_key, is_checked)
        )
        self._update_sponsor_visibility_value(button, key, bool(visible))
        layout.addWidget(button)

        parent_layout.addWidget(block)
        self._setting_widgets[key] = {"type": "inverse_toggle", "widget": button}
        return button

    def _format_slider_label(self, value, display_scale=1.0, display_suffix="", display_decimals=0):
        display_value = float(value) * float(display_scale)
        if int(display_decimals) <= 0:
            text = str(int(round(display_value)))
        else:
            text = f"{display_value:.{int(display_decimals)}f}".rstrip("0").rstrip(".")
        return f"{text}{display_suffix}"

    def _config_from_slider_value(self, value, value_scale=1.0, config_decimals=None):
        scaled_value = float(value) * float(value_scale)
        if config_decimals is not None:
            return round(scaled_value, int(config_decimals))
        if abs(float(value_scale) - 1.0) < 0.000001:
            return int(round(scaled_value))
        return scaled_value

    def _update_slider_value(
        self,
        label,
        key,
        value,
        value_scale=1.0,
        display_scale=1.0,
        display_suffix="",
        display_decimals=0,
        config_decimals=None,
    ):
        label.setText(self._format_slider_label(value, display_scale, display_suffix, display_decimals))
        self.config[key] = self._config_from_slider_value(value, value_scale, config_decimals)
        self._mark_settings_dirty()

    def _update_toggle_value(self, button, key, checked):
        self.config[key] = bool(checked)
        button.setText("已开启" if checked else "已关闭")
        self._mark_settings_dirty()

    def _update_text_value(self, key, text):
        self.config[key] = str(text or "").strip()
        self._mark_settings_dirty()

    def _update_sponsor_visibility_value(self, button, key, checked):
        visible = bool(checked)
        self.config[key] = not visible
        button.setText("已显示" if visible else "已隐藏")
        title_bar = getattr(self, "title_bar", None)
        if title_bar is not None:
            title_bar.sync_sponsor_visibility()
        self._mark_settings_dirty()

    def _sync_sponsor_visibility_setting_button(self):
        button = getattr(self, "sponsor_button_visible_button", None)
        if button is None:
            return
        visible = not bool(self.config.get("sponsor_button_hidden", False))
        was_blocked = button.blockSignals(True)
        button.setChecked(visible)
        button.setText("已显示" if visible else "已隐藏")
        button.blockSignals(was_blocked)

    def _mark_settings_dirty(self):
        if getattr(self, "_settings_building", False):
            return
        self._set_settings_dirty(self._has_unsaved_settings_changes())

    def _set_settings_dirty(self, dirty):
        self._settings_dirty = bool(dirty)
        self._apply_settings_action_bar_style(self._settings_dirty)
        if hasattr(self, "save_settings_btn"):
            self.save_settings_btn.setEnabled(self._settings_dirty)
        if hasattr(self, "settings_status_label"):
            if self._settings_dirty:
                self.settings_status_label.setText("有未保存更改。保存后会立即同步到运行参数，并写入配置文件。")
                self.settings_status_label.setStyleSheet(
                    f"background: transparent; border: none; color: {APP_COLORS['warning']}; font-size: 12px; font-weight: 700;"
                )
            else:
                self.settings_status_label.setText("当前设置已保存")
                self.settings_status_label.setStyleSheet(
                    f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
                )

    def _settings_snapshot_keys(self):
        if getattr(self, "_setting_widgets", None):
            return tuple(self._setting_widgets.keys())
        return tuple(self.default_config.keys())

    def _settings_config_snapshot(self):
        return {key: self.config.get(key) for key in self._settings_snapshot_keys()}

    def _refresh_settings_saved_snapshot(self):
        self._settings_saved_snapshot = self._settings_config_snapshot()

    def _settings_values_equal(self, left, right):
        if isinstance(left, bool) or isinstance(right, bool):
            return bool(left) == bool(right)
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            try:
                return abs(float(left) - float(right)) <= 0.000001
            except (TypeError, ValueError):
                return False
        return left == right

    def _has_unsaved_settings_changes(self):
        snapshot = getattr(self, "_settings_saved_snapshot", {}) or {}
        for key, current_value in self._settings_config_snapshot().items():
            if key not in snapshot:
                return True
            if not self._settings_values_equal(current_value, snapshot.get(key)):
                return True
        return False

    def _reset_current_settings_category(self):
        if not hasattr(self, "settings_stack"):
            return
        index = self.settings_stack.currentIndex()
        keys = self._settings_category_keys.get(index, [])
        changed = False
        for key in keys:
            if key not in self.default_config:
                continue
            widget_info = self._setting_widgets.get(key)
            if not widget_info:
                continue
            default_value = self.default_config[key]
            if widget_info["type"] == "slider":
                slider = widget_info["widget"]
                value_scale = widget_info.get("value_scale", 1.0)
                try:
                    slider_value = int(round(float(default_value) / float(value_scale)))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                slider_value = max(slider.minimum(), min(slider.maximum(), slider_value))
                if slider.value() != slider_value:
                    changed = True
                    slider.setValue(slider_value)
                else:
                    self.config[key] = self._config_from_slider_value(
                        slider_value,
                        value_scale,
                        widget_info.get("config_decimals"),
                    )
            elif widget_info["type"] == "toggle":
                button = widget_info["widget"]
                target = bool(default_value)
                if button.isChecked() != target:
                    changed = True
                    button.setChecked(target)
                else:
                    self.config[key] = target
            elif widget_info["type"] == "inverse_toggle":
                button = widget_info["widget"]
                target = not bool(default_value)
                if button.isChecked() != target:
                    changed = True
                    button.setChecked(target)
                else:
                    self.config[key] = not target
            elif widget_info["type"] == "text":
                line_edit = widget_info["widget"]
                target = str(default_value or "")
                if line_edit.text() != target:
                    changed = True
                    line_edit.setText(target)
                else:
                    self.config[key] = target
        if changed:
            self._mark_settings_dirty()
            self.show_toast("已恢复当前分类推荐值，请保存应用", "info")
        else:
            self.show_toast("当前分类已是推荐值", "info")

    def _save_settings(self):
        self._apply_state_machine_config()
        if self.save_config():
            if getattr(self, "_agreement_shown", False) and self.update_info is None:
                self._schedule_update_check(initial=False)
            title_bar = getattr(self, "title_bar", None)
            if title_bar is not None:
                title_bar.sync_sponsor_visibility()
            self._refresh_settings_saved_snapshot()
            self._set_settings_dirty(False)
            self.show_toast("高级设置已保存并应用", "success")
            self._check_monthly_card_daily_reset()
            self.start_authorization_check(silent=True, reason="manual")
        else:
            self.show_toast("设置保存失败，请查看运行日志", "danger")

    def switch_page(self, index, button):
        for nav in [self.nav_record, self.nav_encyclopedia, self.nav_log, self.nav_settings]:
            nav.setChecked(False)
        button.setChecked(True)
        if index == 1:
            self._ensure_encyclopedia_page()
        self.stack.setCurrentIndex(index)
        # Track page view
        _page_names = {0: "record", 1: "encyclopedia", 2: "log", 3: "settings"}
        try:
            EventTracker.get().ui_page_view(_page_names.get(index, f"page_{index}"))
        except Exception:
            pass
        if index == 0:
            self.page_record.refresh_data()
        elif index == 1:
            self.page_encyclopedia.refresh_data()

    def write_log(self, msg):
        if msg == "CMD_STOP_UPDATE_GUI":
            self.update_ui_on_stop()
            return
        if msg == "CMD_MAIN_HIDE_FOR_CAPTURE":
            self._set_main_capture_hidden(True)
            return
        if msg == "CMD_MAIN_RESTORE_AFTER_CAPTURE":
            self._set_main_capture_hidden(False)
            return
        if msg == "CMD_FLOATING_HIDE_FOR_CAPTURE":
            self._set_floating_capture_hidden(True)
            return
        if msg == "CMD_FLOATING_RESTORE_AFTER_CAPTURE":
            self._set_floating_capture_hidden(False)
            return
        if isinstance(msg, str) and msg.startswith("CMD_USER_TAKEOVER_PAUSED"):
            reason = ""
            if "::" in msg:
                reason = msg.split("::", 1)[1]
            self.handle_user_takeover_pause(reason)
            return

        import time

        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_deque.append(line)
        self._log_version += 1
        log_text = "\n".join(self.log_deque)
        if hasattr(self, "log_textbox"):
            self.log_textbox.setText(log_text)
            self.log_textbox.verticalScrollBar().setValue(self.log_textbox.verticalScrollBar().maximum())
        if self.floating_window is not None:
            self.floating_window.refresh_log_view()

    def _set_floating_capture_hidden(self, hidden):
        floating = getattr(self, "floating_window", None)
        if floating is None:
            return
        floating.set_capture_hidden(hidden)

    def _set_main_capture_hidden(self, hidden):
        hidden = bool(hidden)
        if hidden:
            if getattr(self, "_main_hidden_for_capture", False):
                return
            self._main_hidden_for_capture = True
            self._main_capture_was_visible = bool(self.isVisible())
            self._main_capture_geometry = self.geometry()
            self._main_capture_window_state = self.windowState()
            if self._main_capture_was_visible:
                if self.isMaximized() or self.isFullScreen():
                    self.showNormal()
                self.move(-32000, -32000)
            return

        if not getattr(self, "_main_hidden_for_capture", False):
            return
        was_visible = bool(getattr(self, "_main_capture_was_visible", False))
        geometry = getattr(self, "_main_capture_geometry", None)
        window_state = getattr(self, "_main_capture_window_state", None)
        self._main_hidden_for_capture = False
        self._main_capture_was_visible = False
        self._main_capture_geometry = None
        self._main_capture_window_state = None
        if was_visible:
            if geometry is not None:
                self.setGeometry(geometry)
            if window_state is not None:
                self.setWindowState(window_state)
            if not self.isVisible():
                self.show()
            self.raise_()

    def process_queue(self):
        self._sync_user_takeover_exclude_rects()
        try:
            while True:
                self.write_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        latest_debug_frame = None
        try:
            while True:
                latest_debug_frame = self.debug_queue.get_nowait()
        except queue.Empty:
            pass
        if latest_debug_frame is not None:
            self._set_debug_frame(latest_debug_frame)

    def _sync_user_takeover_exclude_rects(self):
        rects = []
        floating = getattr(self, "floating_window", None)
        if floating is not None and floating.isVisible():
            geom = floating.frameGeometry()
            padding = 10
            rects.append(
                (
                    geom.x() - padding,
                    geom.y() - padding,
                    geom.width() + padding * 2,
                    geom.height() + padding * 2,
                )
            )
        self.sm.update_config("user_takeover_exclude_rects", rects)

    def handle_user_takeover_pause(self, reason=""):
        self.update_ui_on_stop()
        self.status_chip.set_status("已暂停", "stopped")
        self.write_log(">>> 检测到用户接管游戏，自动钓鱼已暂停。")
        self.show_toast("检测到用户接管，自动钓鱼已暂停", "warning")
        if getattr(self, "_takeover_pause_dialog_visible", False):
            return

        detail = reason or "检测到游戏窗口内输入"
        dialog = TakeoverPauseDialog(detail)
        self._takeover_pause_dialog_visible = True
        self.takeover_pause_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "_takeover_pause_dialog_visible", False))
        rect = self.sm.wm.get_client_rect()
        if rect:
            left, top, width, height = rect
            dialog.move(int(left + width / 2 - dialog.width() / 2), int(top + height / 2 - dialog.height() / 2))
        else:
            screen_center = self.screen().availableGeometry().center()
            dialog.move(screen_center - dialog.rect().center())
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _confirm_game_resolution_before_start(self):
        min_width = 1600
        min_height = 900

        if not self.sm.wm.find_window():
            return True

        rect = self.sm.wm.get_client_rect()
        if not rect:
            return True

        left, top, width, height = rect
        if width >= min_width and height >= min_height:
            return True

        dialog = LowResolutionWarningDialog(width, height, min_width, min_height, self)
        dialog.move(int(left + width / 2 - dialog.width() / 2), int(top + height / 2 - dialog.height() / 2))
        result = dialog.exec()
        if result == QDialog.Accepted:
            self.write_log(
                f"[系统] 当前游戏客户区分辨率 {width}x{height} 低于建议的 {min_width}x{min_height}，用户选择继续启动。"
            )
            return True

        self.write_log(
            f"[系统] 当前游戏客户区分辨率 {width}x{height} 低于建议的 {min_width}x{min_height}，已取消启动。"
        )
        self.show_toast("已取消启动，请先调高游戏分辨率", "warning")
        return False

    def start_bot(self):
        if self.sm.is_running:
            return
        if not self.ensure_authorized_for_action("启动自动钓鱼"):
            return
        if not self.modules_ready:
            self.start_module_initialization()
            return

        if not self._confirm_game_resolution_before_start():
            return

        self._apply_state_machine_config()
        self._takeover_pause_dialog_visible = False

        self.status_chip.set_status("运行中", "running")
        if self.config.get("auto_switch_to_log", True):
            self.switch_page(2, self.nav_log)
        self.write_log(">>> 启动自动钓鱼。")
        self.sm.start()
        self.update_primary_buttons()
        self.show_toast("自动钓鱼已启动", "success")
        try:
            EventTracker.get().ui_click("start_fishing", "main")
        except Exception:
            pass

    def stop_bot(self):
        if not self.sm.is_running:
            self.show_toast("当前未在运行", "warning")
            return
        self.sm.stop()
        self.write_log(">>> 已发送停止指令。")
        self.update_ui_on_stop()
        self.show_toast("停止指令已发送", "warning")
        try:
            EventTracker.get().ui_click("stop_fishing", "main")
        except Exception:
            pass

    def update_ui_on_stop(self):
        self.status_chip.set_status("已停止", "stopped")
        self.update_primary_buttons()
        if hasattr(self, "debug_preview") and not self.config.get("debug_mode", False):
            self._refresh_debug_view_state()
        self.page_record.refresh_data()
        if self.page_encyclopedia is not None:
            self.page_encyclopedia.refresh_data()

