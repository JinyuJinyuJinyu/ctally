"""The settings window: CTally on or off, and how see-through it is."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QSlider, QVBoxLayout, QWidget)

from .. import system
from ..sessions import BY_URGENCY

OPACITY_RANGE = (0.3, 1.0)
CYAN = QColor.fromRgbF(0.29, 0.80, 0.95)


def hexagon(center: QPointF, radius: float) -> list[QPointF]:
    """CTally's shape: a hexagon standing on a point."""
    return [QPointF(center.x() + radius * math.cos(math.radians(30 + 60 * i)),
                    center.y() - radius * math.sin(math.radians(30 + 60 * i))) for i in range(6)]


def app_icon(size: int, ratio: float = 2.0) -> QPixmap:
    """The app icon, the badge CTally shows for a working session: smoked hexagon, cyan
    segmented ring, play mark."""
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    c, r = QPointF(size / 2, size / 2), size * 0.44
    s = r / 44

    plate = QPainterPath()
    plate.addPolygon(hexagon(c, r))
    plate.closeSubpath()
    p.fillPath(plate, QColor.fromRgbF(0.09, 0.10, 0.13))
    p.strokePath(plate, QPen(QColor.fromRgbF(1, 1, 1, 0.12), max(0.5, s)))

    ring = QPen(QColor.fromRgbF(0.29, 0.80, 0.95, 0.9), 2.5 * s)
    ring.setCapStyle(Qt.RoundCap)
    p.setPen(ring)
    pts = hexagon(c, r * 0.82)
    for i in range(6):
        a, b = pts[i], pts[(i + 1) % 6]
        p.drawLine(a + (b - a) * 0.17, a + (b - a) * 0.83)

    play = QPainterPath()
    play.moveTo(c.x() - 9 * s, c.y() - 13 * s)
    play.lineTo(c.x() + 14 * s, c.y())
    play.lineTo(c.x() - 9 * s, c.y() + 13 * s)
    play.closeSubpath()
    edge = QPen(CYAN, 4 * s)
    edge.setJoinStyle(Qt.RoundJoin)
    p.setPen(edge)
    p.setBrush(CYAN)
    p.drawPath(play)
    p.end()
    return pixmap


class Switch(QAbstractButton):
    """An on/off switch, drawn: Qt has none of its own."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(38, 22)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        on = self.isChecked()
        p.setPen(Qt.NoPen)
        p.setBrush(self.palette().highlight().color() if on else self.palette().mid().color())
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = track.height() - 4
        x = track.right() - 2 - knob if on else track.left() + 2
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QRectF(x, track.top() + 2, knob, knob))
        if self.hasFocus():
            p.setPen(QPen(self.palette().highlight().color(), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(track.adjusted(-0.5, -0.5, 0.5, 0.5), track.height() / 2, track.height() / 2)
        p.end()


class SettingsWindow(QWidget):
    """Switch, opacity, quit. Closing it with Esc or Cmd/Ctrl-W, like any small panel."""

    toggled = Signal(bool)
    opacity_changed = Signal(float)
    quit_requested = Signal()
    closed = Signal()

    WIDTH = 400

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("CTally")
        self.setWindowIcon(app_icon(64))

        self.icon = QLabel()
        self.icon.setPixmap(app_icon(40))
        self.icon.setFixedSize(40, 40)

        title = QLabel("Show CTally")
        bold = title.font()
        bold.setBold(True)
        title.setFont(bold)
        self.status = QLabel("")
        secondary(self.status)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        labels.addWidget(title)
        labels.addWidget(self.status)

        self.switch = Switch()
        self.switch.setAccessibleName("Show CTally")
        self.switch.toggled.connect(self.toggled)

        opacity_title = QLabel("Opacity")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(OPACITY_RANGE[0] * 100), round(OPACITY_RANGE[1] * 100))
        self.slider.setAccessibleName("Opacity")
        self.slider.setTracking(True)     # continuous, so the indicator fades live under it
        self.slider.valueChanged.connect(self._slid)
        self.opacity_value = QLabel("")
        secondary(self.opacity_value)
        self.opacity_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.opacity_value.setFixedWidth(38)

        where = "menu bar" if system.MAC else "top bar"
        hint = QLabel(f"Off stays off, even after a restart. The hexagon icon in the {where} switches it too.")
        hint.setWordWrap(True)
        secondary(hint)
        quit_ = QPushButton("Quit")
        quit_.setAutoDefault(False)
        quit_.clicked.connect(self.quit_requested)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Sunken)

        # The text and slider take the slack, pinning the switch, percentage and Quit to the
        # right edge. The opacity line is indented to sit under "Show CTally".
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.icon)
        row.addLayout(labels, 1)
        row.addWidget(self.switch, 0, Qt.AlignVCenter)
        fade = QHBoxLayout()
        fade.setSpacing(12)
        fade.setContentsMargins(52, 0, 0, 0)
        fade.addWidget(opacity_title)
        fade.addWidget(self.slider, 1)
        fade.addWidget(self.opacity_value)
        footer = QHBoxLayout()
        footer.setSpacing(12)
        footer.addWidget(hint, 1)
        footer.addWidget(quit_, 0, Qt.AlignVCenter)

        stack = QVBoxLayout(self)
        stack.setContentsMargins(20, 20, 20, 20)
        stack.setSpacing(14)
        stack.addLayout(row)
        stack.addLayout(fade)
        stack.addWidget(rule)
        stack.addLayout(footer)

        for keys in (QKeySequence(Qt.Key_Escape), QKeySequence("Ctrl+W")):
            QShortcut(keys, self, activated=self.close)

        self.setFixedWidth(SettingsWindow.WIDTH)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def update_state(self, enabled: bool, sessions: list) -> None:
        """Called on every poll, so it only touches what changed; setting the switch mid-click
        would fight the click."""
        if self.switch.isChecked() != enabled:
            self.switch.blockSignals(True)
            self.switch.setChecked(enabled)
            self.switch.blockSignals(False)
            self.switch.update()
        if self._enabled is not enabled:
            self._enabled = enabled
            self.icon.setPixmap(app_icon(40) if enabled else self._faded_icon())
        text = describe(sessions) if enabled else "Off · hidden"
        if self.status.text() != text:
            self.status.setText(text)

    def update_opacity(self, value: float) -> None:
        percent = round(value * 100)
        if self.slider.value() != percent:
            self.slider.blockSignals(True)
            self.slider.setValue(percent)
            self.slider.blockSignals(False)
        self.opacity_value.setText(f"{percent}%")

    def _slid(self, percent: int) -> None:
        self.opacity_value.setText(f"{percent}%")
        self.opacity_changed.emit(percent / 100)

    _enabled = None
    _faded = None

    def _faded_icon(self) -> QPixmap:
        if SettingsWindow._faded is None:
            source = app_icon(40)
            faded = QPixmap(source.size())
            faded.setDevicePixelRatio(source.devicePixelRatio())
            faded.fill(Qt.transparent)
            p = QPainter(faded)
            p.setOpacity(0.35)
            p.drawPixmap(0, 0, source)
            p.end()
            SettingsWindow._faded = faded
        return SettingsWindow._faded

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        self.closed.emit()


def secondary(label: QLabel) -> None:
    """Smaller and quieter, for the lines that explain rather than name."""
    font = label.font()
    font.setPointSizeF(font.pointSizeF() * 11 / 13)
    label.setFont(font)
    palette = label.palette()
    colour = palette.color(QPalette.WindowText)
    colour.setAlphaF(0.6)
    palette.setColor(QPalette.WindowText, colour)
    label.setPalette(palette)


def describe(sessions: list) -> str:
    """What the indicator is showing, in words: "On · 1 working, 2 done"."""
    counts = []
    for state in BY_URGENCY:
        n = sum(1 for s in sessions if s.state is state)
        if n:
            counts.append(f"{n} {state.value}")
    return "On · " + ", ".join(counts) if counts else "On · no Claude Code sessions"
