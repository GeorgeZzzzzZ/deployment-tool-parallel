#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Glassmorphism visual components for Deploy Console Parallel."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Property, QEasingCurve, QRect, QRectF, QSize, QVariantAnimation, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QLayout, QWidget


class WrappingActionLayout(QLayout):
    """Keep related toolbar controls together, wrapping only when needed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def addItem(self, item):
        self._items.append(item)

    def addLayout(self, layout):
        self.addChildLayout(layout)
        self.addItem(layout)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def _arrange(self, rect, apply):
        visible = [item for item in self._items if not item.isEmpty()]
        rows = []
        row = []
        width = 0
        for item in visible:
            size = item.sizeHint().expandedTo(item.minimumSize())
            gap = self.spacing() if row else 0
            if row and width + gap + size.width() > rect.width():
                rows.append(row)
                row, width, gap = [], 0, 0
            row.append((item, size))
            width += gap + size.width()
        if row:
            rows.append(row)
        y = rect.y()
        for row in rows:
            height = max(size.height() for _, size in row)
            x = rect.x()
            for item, size in row:
                # The last group contains status/progress and stays at the right edge.
                if item is visible[-1]:
                    x = max(x, rect.right() + 1 - size.width())
                if apply:
                    item.setGeometry(QRect(x, y + (height - size.height()) // 2,
                                           size.width(), size.height()))
                x += size.width() + self.spacing()
            y += height + self.spacing()
        return y - rect.y() - (self.spacing() if rows else 0)


def is_reduced_motion() -> bool:
    """Read Windows' client-area animation preference without changing it."""
    if sys.platform == "win32":
        enabled = ctypes.c_int(0)
        try:
            # SPI_GETCLIENTAREAANIMATION (0x1042) writes a Win32 BOOL.
            success = ctypes.windll.user32.SystemParametersInfoW(
                0x1042, 0, ctypes.byref(enabled), 0,
            )
            return not success or not enabled.value
        except (AttributeError, OSError):
            return True
    return False


class GlassBackground(QWidget):
    """Deep navy canvas overlaid with subtle rose, amber, cyan-blue, and purple glowing auras."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        w, h = rect.width(), rect.height()
        if w <= 0 or h <= 0:
            return

        # 1. Base deep navy gradient
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor("#0B1020"))
        base.setColorAt(1.0, QColor("#080D1A"))
        painter.fillRect(rect, base)

        # 2. Rose red top-left aura
        r_tl = max(w, h) * 0.55
        g_tl = QRadialGradient(w * 0.12, h * 0.14, r_tl)
        g_tl.setColorAt(0.0, QColor(225, 29, 72, 54))
        g_tl.setColorAt(0.45, QColor(168, 85, 247, 22))
        g_tl.setColorAt(1.0, QColor(11, 16, 32, 0))
        painter.fillRect(rect, g_tl)

        # 3. Amber mid-left glow
        r_ml = max(w, h) * 0.45
        g_ml = QRadialGradient(w * 0.08, h * 0.60, r_ml)
        g_ml.setColorAt(0.0, QColor(245, 158, 11, 30))
        g_ml.setColorAt(0.50, QColor(234, 88, 12, 12))
        g_ml.setColorAt(1.0, QColor(11, 16, 32, 0))
        painter.fillRect(rect, g_ml)

        # 4. Cyan-blue mid-bottom glow
        r_mb = max(w, h) * 0.50
        g_mb = QRadialGradient(w * 0.45, h * 0.88, r_mb)
        g_mb.setColorAt(0.0, QColor(14, 165, 233, 38))
        g_mb.setColorAt(0.55, QColor(59, 130, 246, 16))
        g_mb.setColorAt(1.0, QColor(11, 16, 32, 0))
        painter.fillRect(rect, g_mb)

        # 5. Purple bottom-right aura
        r_br = max(w, h) * 0.60
        g_br = QRadialGradient(w * 0.88, h * 0.80, r_br)
        g_br.setColorAt(0.0, QColor(139, 92, 246, 52))
        g_br.setColorAt(0.45, QColor(99, 102, 241, 24))
        g_br.setColorAt(1.0, QColor(11, 16, 32, 0))
        painter.fillRect(rect, g_br)


class GlassPanel(QFrame):
    """Frosted-glass card container with 16px radius, 1px highlight border, and smooth hover animation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._hover_amount: float = 0.0

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim_value_changed)

    def _get_hover_amount(self) -> float:
        return self._hover_amount

    def _set_hover_amount(self, value: float) -> None:
        self._hover_amount = max(0.0, min(1.0, float(value)))
        self.update()

    hover_amount = Property(float, _get_hover_amount, _set_hover_amount)

    def _on_anim_value_changed(self, value: float) -> None:
        self._hover_amount = float(value)
        self.update()

    def _animate_to(self, target: float) -> None:
        if is_reduced_motion():
            self._anim.stop()
            self._set_hover_amount(target)
            return
        self._anim.stop()
        self._anim.setStartValue(self._hover_amount)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._animate_to(1.0)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._animate_to(0.0)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        w, h = rect.width(), rect.height()
        if w <= 0 or h <= 0:
            return

        radius = 16.0
        t = self._hover_amount

        # 1. Fill
        is_dark_card = self.objectName() == "DarkCard"
        if is_dark_card:
            # Slightly darker frosted glass fill for high-contrast logs
            alpha_dark = int(255 * (0.86 + 0.06 * t))
            bg_grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
            bg_grad.setColorAt(0.0, QColor(7, 11, 20, alpha_dark))
            bg_grad.setColorAt(1.0, QColor(5, 8, 16, alpha_dark))
        else:
            # Translucent white frosted glass fill (0.05 -> 0.15)
            alpha_top = int(255 * (0.07 + 0.08 * t))
            alpha_mid = int(255 * (0.05 + 0.06 * t))
            alpha_bot = int(255 * (0.03 + 0.04 * t))

            bg_grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
            bg_grad.setColorAt(0.0, QColor(255, 255, 255, alpha_top))
            bg_grad.setColorAt(0.45, QColor(255, 255, 255, alpha_mid))
            bg_grad.setColorAt(1.0, QColor(255, 255, 255, alpha_bot))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_grad)
        painter.drawRoundedRect(rect, radius, radius)

        # 2. 1px gradient highlight stroke
        b_top = int(55 + 40 * t)
        b_mid = int(22 + 20 * t)
        b_bot = int(12 + 12 * t)

        border_grad = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        border_grad.setColorAt(0.0, QColor(255, 255, 255, b_top))
        border_grad.setColorAt(0.35, QColor(255, 255, 255, b_mid))
        border_grad.setColorAt(1.0, QColor(255, 255, 255, b_bot))

        painter.setPen(QPen(QBrush(border_grad), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        # 3. Subtle inner glow along top edge
        inner_top = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
        inner_top.setColorAt(0.0, QColor(255, 255, 255, int(10 + 15 * t)))
        inner_top.setColorAt(0.25, QColor(255, 255, 255, int(35 + 30 * t)))
        inner_top.setColorAt(0.75, QColor(255, 255, 255, int(20 + 20 * t)))
        inner_top.setColorAt(1.0, QColor(255, 255, 255, int(5 + 10 * t)))
        painter.setPen(QPen(QBrush(inner_top), 1.0))
        painter.drawLine(
            int(rect.left() + radius),
            int(rect.top() + 1),
            int(rect.right() - radius),
            int(rect.top() + 1),
        )
