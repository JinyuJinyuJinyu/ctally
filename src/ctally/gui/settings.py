"""The settings window: CTally on or off, how see-through it is, whether it shows your
plan's usage limits and asks Anthropic for them, and whether it takes snapshots of tmux for
after a restart, how often, and how many days it keeps them. Close Window puts it away and
leaves CTally running; Quit CTally stops it, and says how to start it again."""
from __future__ import annotations

import math
import re

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut,
                           QValidator)
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

from .. import install, snapshots, system
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


def duration_text(minutes: int) -> str:
    """Minutes as hours and minutes: "30 min", "1 h", "1 h 30 min"."""
    hours, rest = divmod(minutes, 60)
    return " ".join(part for part in (f"{hours} h" if hours else "", f"{rest} min" if rest or not hours else "")
                    if part)


_DURATION = re.compile(r"\s*(?:(\d+(?:[.,]\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*(m(?:in(?:ute)?s?)?)?)?\s*",
                       re.IGNORECASE)


def duration_minutes(text: str, most_hours: int) -> int | None:
    """A time as typed, in minutes: "90", "90 min", "2 h", "1.5h", "1 h 30 min". A bare number
    no bigger than the most hours there can be is hours: nobody means 4 minutes there."""
    found = _DURATION.fullmatch(text)
    if not found or not (found[1] or found[2]):
        return None
    hours = float(found[1].replace(",", ".")) if found[1] else 0.0
    rest = int(found[2]) if found[2] else 0
    if not found[1] and not found[3] and rest <= most_hours:
        hours, rest = float(rest), 0
    return round(hours * 60) + rest


class DurationBox(QSpinBox):
    """A time of half an hour to some hours: kept in minutes, shown as hours and minutes, and
    stepped by the half hour. Typed, it's taken any way duration_minutes() reads it, and too
    much or too little is the nearest allowed."""

    def __init__(self, least: int, most: int):
        super().__init__()
        self.setRange(least, most)
        self.setSingleStep(30)
        # As wide as its longest: Qt allows only for the ends of the range.
        longest = max((duration_text(m) for m in range(least, most + 1, 30)), key=len)
        metrics = self.fontMetrics()
        extra = metrics.horizontalAdvance(longest) - max(metrics.horizontalAdvance(duration_text(m)) for m in (least, most))
        self.setMinimumWidth(self.sizeHint().width() + max(extra, 0))

    def _typed(self, text: str) -> int | None:
        return duration_minutes(text, self.maximum() // 60)

    def textFromValue(self, value: int) -> str:
        return duration_text(value)

    def valueFromText(self, text: str) -> int:
        minutes = self._typed(text)
        return self.value() if minutes is None else min(max(minutes, self.minimum()), self.maximum())

    def validate(self, text: str, position: int):
        minutes = self._typed(text)
        allowed = minutes is not None and self.minimum() <= minutes <= self.maximum()
        return (QValidator.Acceptable if allowed else QValidator.Intermediate), text, position

    def fixup(self, text: str) -> str:
        minutes = self._typed(text)
        return text if minutes is None else duration_text(min(max(minutes, self.minimum()), self.maximum()))


class SettingsWindow(QWidget):
    """Switch, opacity, usage limits, snapshots; Close Window and Quit CTally. Closing it with
    Esc or Cmd/Ctrl-W, like any small panel."""

    toggled = Signal(bool)
    opacity_changed = Signal(float)
    usage_toggled = Signal(bool)
    exact_usage_toggled = Signal(bool)
    snapshots_toggled = Signal(bool)
    snapshot_minutes_changed = Signal(int)
    snapshot_days_changed = Signal(int)
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

        usage_title = QLabel("Usage limits")
        usage_hint = QLabel("Session and weekly limits, as in /usage")
        secondary(usage_hint)
        usage_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        usage_labels = QVBoxLayout()
        usage_labels.setSpacing(2)
        usage_labels.addWidget(usage_title)
        usage_labels.addWidget(usage_hint)
        self.usage_switch = Switch()
        self.usage_switch.setAccessibleName("Usage limits")
        self.usage_switch.toggled.connect(self.usage_toggled)

        exact_title = QLabel("Exact usage")
        exact_hint = QLabel("Asks Anthropic every 5 min, as /usage does")
        secondary(exact_hint)
        exact_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        exact_labels = QVBoxLayout()
        exact_labels.setSpacing(2)
        exact_labels.addWidget(exact_title)
        exact_labels.addWidget(exact_hint)
        self.exact_switch = Switch()
        self.exact_switch.setAccessibleName("Exact usage")
        self.exact_switch.toggled.connect(self.exact_usage_toggled)

        snapshots_title = QLabel("Snapshots")
        snapshots_hint = QLabel("To restore tmux after a restart")
        secondary(snapshots_hint)
        snapshots_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        snapshots_labels = QVBoxLayout()
        snapshots_labels.setSpacing(2)
        snapshots_labels.addWidget(snapshots_title)
        snapshots_labels.addWidget(snapshots_hint)
        self.snapshots_switch = Switch()
        self.snapshots_switch.setAccessibleName("Snapshots")
        self.snapshots_switch.toggled.connect(self.snapshots_toggled)

        # How often, and for how long. Typed numbers count once they're done with (Return, or
        # moving on), so typing 15 isn't 1 first.
        every_title = QLabel("Every")
        self.every_box = DurationBox(*snapshots.MINUTES)
        self.every_box.setAccessibleName("Time between snapshots")
        self.every_box.setKeyboardTracking(False)
        self.every_box.valueChanged.connect(self.snapshot_minutes_changed)
        keep_title = QLabel("Keep for")
        self.keep_box = QSpinBox()
        self.keep_box.setRange(*snapshots.DAYS)
        self.keep_box.setSuffix(" days")
        self.keep_box.setAccessibleName("Days to keep snapshots")
        self.keep_box.setKeyboardTracking(False)
        self.keep_box.valueChanged.connect(self._kept)

        where = "menu bar" if system.MAC else "top bar"
        hint = QLabel(f"Off stays off, even after a restart. The {where}'s hexagon switches it too.")
        hint.setWordWrap(True)
        secondary(hint)
        # Two buttons that say what they do: put the window away, or stop CTally. One named
        # Quit alone was taken for the first.
        self.quit_button = QPushButton("Quit CTally")
        self.quit_button.setAutoDefault(False)
        self.quit_button.clicked.connect(self.quit_requested)
        self.close_button = QPushButton("Close Window")
        self.close_button.setAutoDefault(False)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.close)
        if system.MAC:
            again = "run “ctally run” in a terminal"
        elif install.LAUNCHER.exists():
            again = "open CTally from your applications, or run “ctally run” in a terminal"
        else:
            again = "run “ctally run” in a terminal"
        self.quit_hint = QLabel(f"Close Window leaves CTally running. Quit CTally stops it: to start it again, {again}.")
        self.quit_hint.setWordWrap(True)
        secondary(self.quit_hint)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Sunken)

        # The text and slider take the slack, pinning the switch and percentage to the right
        # edge. The opacity line is indented to sit under "Show CTally".
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
        limits = QHBoxLayout()
        limits.setSpacing(12)
        limits.setContentsMargins(52, 0, 0, 0)
        limits.addLayout(usage_labels, 1)
        limits.addWidget(self.usage_switch, 0, Qt.AlignVCenter)
        exact = QHBoxLayout()
        exact.setSpacing(12)
        exact.setContentsMargins(52, 0, 0, 0)
        exact.addLayout(exact_labels, 1)
        exact.addWidget(self.exact_switch, 0, Qt.AlignVCenter)
        keep = QHBoxLayout()
        keep.setSpacing(12)
        keep.setContentsMargins(52, 0, 0, 0)
        keep.addLayout(snapshots_labels, 1)
        keep.addWidget(self.snapshots_switch, 0, Qt.AlignVCenter)
        times = QHBoxLayout()
        times.setSpacing(8)
        times.setContentsMargins(52, 0, 0, 0)
        times.addWidget(every_title)
        times.addWidget(self.every_box)
        times.addSpacing(16)
        times.addWidget(keep_title)
        times.addWidget(self.keep_box)
        times.addStretch(1)
        # Quit to the left, out of the way; Close Window where the hand goes, to the right.
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addWidget(self.quit_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        stack = QVBoxLayout(self)
        stack.setContentsMargins(20, 20, 20, 20)
        stack.setSpacing(14)
        stack.addLayout(row)
        stack.addLayout(fade)
        stack.addLayout(limits)
        stack.addLayout(exact)
        stack.addLayout(keep)
        stack.addLayout(times)
        stack.addWidget(hint)
        stack.addWidget(rule)
        stack.addWidget(self.quit_hint)
        stack.addLayout(buttons)

        for keys in (QKeySequence(Qt.Key_Escape), QKeySequence("Ctrl+W")):
            QShortcut(keys, self, activated=self.close)
        # Opened, the window has the focus, not the first box: no ring around it, and nothing
        # typed into it by the way. A click on the window takes it back, and settles a box.
        self.setFocusPolicy(Qt.ClickFocus)
        self.setFocus()

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

    def update_usage(self, shown: bool) -> None:
        if self.usage_switch.isChecked() != shown:
            self.usage_switch.blockSignals(True)
            self.usage_switch.setChecked(shown)
            self.usage_switch.blockSignals(False)
            self.usage_switch.update()

    def update_exact_usage(self, on: bool) -> None:
        if self.exact_switch.isChecked() != on:
            self.exact_switch.blockSignals(True)
            self.exact_switch.setChecked(on)
            self.exact_switch.blockSignals(False)
            self.exact_switch.update()

    def update_snapshots(self, on: bool) -> None:
        if self.snapshots_switch.isChecked() != on:
            self.snapshots_switch.blockSignals(True)
            self.snapshots_switch.setChecked(on)
            self.snapshots_switch.blockSignals(False)
            self.snapshots_switch.update()

    def update_snapshot_times(self, minutes: int, days: int) -> None:
        for box, value in ((self.every_box, minutes), (self.keep_box, days)):
            if box.value() != value:
                box.blockSignals(True)
                box.setValue(value)
                box.blockSignals(False)
        self._days_suffix(days)

    def _kept(self, days: int) -> None:
        self._days_suffix(days)
        self.snapshot_days_changed.emit(days)

    def _days_suffix(self, days: int) -> None:
        # Only when it changes: setting it puts the box's text back to its value, and a
        # number being typed would be gone.
        suffix = " day" if days == 1 else " days"
        if self.keep_box.suffix() != suffix:
            self.keep_box.setSuffix(suffix)

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
        for box in (self.every_box, self.keep_box):
            box.interpretText()             # a number typed and not yet done with counts too
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
