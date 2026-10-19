"""The indicator itself.

Up to three sessions get big badges: hexagons with a transport-control glyph (play, pause,
check, dot), side by side, each captioned with its session's name and directory once there
is more than one, and tagged with its agent count while any run. Four or more become a
list, one row per session: a small hexagon, the name over the directory, the state in words,
and a line about its agents while any run. The list opens away from the screen edge it's
nearer: upward from a count bar at its bottom, or, high on the screen, downward from a count
bar at its top. The oldest session sits next to the bar, so rows already on screen stay put
when a new session starts; the chevron on the bar folds the list away to just the counts.
More rows than fit (sixteen, or what the screen has room for) scroll, with the wheel or the
scroll bar beside them, which marks where the sessions waiting on me are.

A press that moves drags the indicator; one that doesn't is a click, which asks for that
session's terminal. The view moves its own window while dragging and says when it's
dropped; everything else about the window is the controller's business.

Your plan's usage limits ride along when known: every one of them, a bar each, between the
list's rows and its count bar; the session and the week as two small meters in the folded bar
and on a card under the badges. Hovering any of them gives the details.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QPainter,
                           QPainterPath, QPen, QPixmap, QRegion)
from PySide6.QtWidgets import QToolTip, QWidget

from ..sessions import BY_URGENCY, Session, State
from ..usage import Usage, describe, percent_text, when

ACCENT = {
    State.WORKING: QColor.fromRgbF(0.29, 0.80, 0.95),   # cyan
    State.WAITING: QColor.fromRgbF(1.00, 0.70, 0.16),   # amber
    State.DONE: QColor.fromRgbF(0.27, 0.88, 0.53),      # green
    State.IDLE: QColor.fromRgbF(0.60, 0.65, 0.74),      # slate
}
# A usage bar's fill: calm, then amber getting close, then red nearly out.
LEVEL = (QColor.fromRgbF(0.80, 0.84, 0.93), ACCENT[State.WAITING], QColor.fromRgbF(1.00, 0.40, 0.38))


def white(level: float, alpha: float) -> QColor:
    return QColor.fromRgbF(level, level, level, alpha)


def faded(color: QColor, alpha: float) -> QColor:
    copy = QColor(color)
    copy.setAlphaF(alpha)
    return copy


def lerp(a: QPointF, b: QPointF, t: float) -> QPointF:
    return QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)


def hexagon(centre: QPointF, radius: float) -> list[QPointF]:
    """CTally's shape: a hexagon standing on a point."""
    return [QPointF(centre.x() + radius * math.cos(math.radians(30 + 60 * i)),
                    centre.y() - radius * math.sin(math.radians(30 + 60 * i))) for i in range(6)]


def hexagon_path(centre: QPointF, radius: float) -> QPainterPath:
    points = hexagon(centre, radius)
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()
    return path


def draw_glyph(painter: QPainter, state: State, c: QPointF, s: float, color: QColor) -> None:
    """Play while working, pause while waiting on me, a check when done, a dot when idle."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    if state is State.WORKING:
        path = QPainterPath(QPointF(c.x() - 9 * s, c.y() - 13 * s))
        path.lineTo(c.x() + 14 * s, c.y())
        path.lineTo(c.x() - 9 * s, c.y() + 13 * s)
        path.closeSubpath()
        pen = QPen(color, 4 * s)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
    elif state is State.WAITING:
        for dx in (-8, 3):
            painter.drawRoundedRect(QRectF(c.x() + dx * s, c.y() - 13 * s, 5.5 * s, 26 * s), 2.5 * s, 2.5 * s)
    elif state is State.DONE:
        path = QPainterPath(QPointF(c.x() - 13 * s, c.y() - 1 * s))
        path.lineTo(c.x() - 4 * s, c.y() + 9 * s)
        path.lineTo(c.x() + 14 * s, c.y() - 12 * s)
        pen = QPen(color, 6 * s)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
    else:
        painter.drawEllipse(c, 5.5 * s, 5.5 * s)


class Fonts:
    """Sizes in points as on macOS, so the layout measures the same on Linux's 96 dpi."""

    def __init__(self):
        screen = QGuiApplication.primaryScreen()
        dpi = screen.logicalDotsPerInchY() if screen else 72.0

        def font(size: float, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QFont:
            base = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont) if mono else QFont(QGuiApplication.font())
            base.setPointSizeF(size * 72 / dpi)
            base.setWeight(weight)
            return base

        self.title = font(12, QFont.Weight.DemiBold)
        self.path = font(10)
        self.tmux = font(9.5, QFont.Weight.Medium, mono=True)
        self.state = font(10.5, QFont.Weight.Medium)
        self.caption_title = font(10.5, QFont.Weight.DemiBold)
        self.caption_path = font(9)
        self.count = font(11.5, QFont.Weight.DemiBold)
        self.agent = font(10, QFont.Weight.Medium)
        self.tag = font(10, QFont.Weight.DemiBold)
        self.usage = font(10, QFont.Weight.Medium)
        self.usage_when = font(9.5)
        self.meter = font(9, QFont.Weight.DemiBold)
        self.meter_tag = font(8.5, QFont.Weight.Medium)


def measure(text: str, font: QFont) -> float:
    return math.ceil(QFontMetricsF(font).horizontalAdvance(text))


def draw_text(painter: QPainter, text: str, rect: QRectF, font: QFont, color: QColor,
              align: Qt.AlignmentFlag = Qt.AlignLeft, elide: Qt.TextElideMode = Qt.ElideRight) -> None:
    """One line, vertically centred in its rect, shortened where it doesn't fit."""
    shown = QFontMetricsF(font).elidedText(text, elide, max(0.0, rect.width()))
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(rect, int(align | Qt.AlignVCenter), shown)


class Row(NamedTuple):
    session: Session
    title: str          # session name, numbered when two share one
    path: str           # directory, with ~ for home
    tmux: str           # "report:0.2", or empty outside tmux
    agents: str         # "4 agents · Verify", or empty with none running


class Meter(NamedTuple):
    """One usage limit, as drawn."""
    label: str          # "Session", "Week · Fable"
    tag: str            # "5h", for the small meters
    share: float        # used, 0–1
    percent: str        # "71%"
    when: str           # "in 1h 12m", "Sat 10 pm"
    level: int          # 0 fine, 1 getting close, 2 nearly out


class Chip(NamedTuple):
    """One big badge, or one of a few side by side with a caption under each."""
    width: float
    height: float
    radius: float
    centre_y: float     # from the top
    bounce: float
    captioned: bool


class TallyView(QWidget):

    fold_requested = Signal(bool)       # the chevron was clicked: the folded state wanted
    selected = Signal(object, object)   # a row or badge was clicked: its Session, and when (focus.focus)
    drag_finished = Signal()            # dropped after a drag
    menu_requested = Signal(QPoint)     # a right-click, in global coordinates

    SINGLE = Chip(120, 120, 44, 66, 18, False)
    SEVERAL = Chip(124, 140, 33, 52, 13, True)
    LIST_FROM = 4

    # The list.
    MAX_ROWS = 16           # rows shown at once; past that they scroll
    PADDING = 8
    ROW_HEIGHT = 38
    AGENT_LINE = 14         # added under a row while its agents run
    PROGRESS_WIDTH = 54     # a workflow's progress bar, at the end of that line
    HEADER_HEIGHT = 26      # the count bar, which is all a folded list shows
    HEADER_GAP = 6          # between the count bar and the rows
    FOLD_SIZE = 22
    MARK_RADIUS = 10
    MARK_X = 12             # mark centre, from the row's left edge
    TEXT_X = 30             # text start, from the row's left edge
    STATE_WIDTH = 46        # right-hand column for the state word
    WIDTH_RANGE = (200, 320)
    SCROLL_GUTTER = 8       # added beside the rows for the scroll bar, while they scroll
    SCROLL_WIDTH = 4
    SCROLL_MIN_THUMB = 24

    # Usage limits.
    USAGE_LINE = 18         # one limit in the list
    USAGE_TRACK = 60        # the least a bar there shrinks to
    METER_LINE = 12         # one of the two small meters
    METER_TRACK = 44
    PLATE_HEIGHT = 30       # the small meters' card under the badges
    PLATE_MARGIN = 8        # under that card

    BOUNCE_DURATION = 1.5
    WASH_DURATION = 2.5

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.fonts = Fonts()
        self._epoch = time.monotonic()
        self._sessions: list[Session] = []
        self._rows: list[Row] = []
        self._row_offsets = [0.0]       # each row's bottom above the first's, and the total last
        self._counts: list[tuple[State, int]] = []
        self._scroll = 0.0              # how far the rows are scrolled, away from the bar
        self._max_height: int | None = None
        self._grab: float | None = None     # dragging the scroll bar: where in its thumb I hold it
        self._over_scroll_bar = False
        self._list_width = float(TallyView.WIDTH_RANGE[0])
        self._bar_width = float(TallyView.WIDTH_RANGE[0])
        self._entered: dict[str, float] = {}
        self._primed = False
        self._folded = False
        self._grows_down = False
        self._hovered: int | None = None
        self._press: list | None = None    # [global start, window origin, dragging]
        self._region = QRegion()
        # Everything that holds still, drawn once into an image; each frame copies it and
        # draws only the marks that move, themselves from images. Python can't afford to
        # draw the whole list 24 times a second.
        self._layer: QPixmap | None = None
        self._layer_key: tuple | None = None
        self._version = 0                       # bumped by anything that changes the still parts
        self._still_only = False                # while drawing the layer: leave out what moves
        self._sprites: dict[tuple, QPixmap] = {}
        self._usage: Usage | None = None
        self._meters: tuple[Meter, ...] = ()        # every limit, for the list
        self._compact: tuple[Meter, ...] = ()       # the session and the week, for small places
        self._usage_dim = False                     # numbers over an hour old
        self._usage_width = 0.0                     # what the list needs to show every limit
        self._meter_width = 0.0                     # the small meters, side by side with a gap
        # The indicator is never the active window, and Qt otherwise shows tooltips only for
        # the active one.
        self.setAttribute(Qt.WA_AlwaysShowToolTips)

    # MARK: The interface

    @property
    def folded(self) -> bool:
        return self._folded

    @folded.setter
    def folded(self, value: bool) -> None:
        if value != self._folded:
            self._folded = value
            self._hovered = None
            self._version += 1
            self.update()

    @property
    def grows_down(self) -> bool:
        """Whether the list opens downward, from a count bar at its top: the controller's
        call, by where on the screen the indicator sits."""
        return self._grows_down

    @grows_down.setter
    def grows_down(self, value: bool) -> None:
        if value != self._grows_down:
            self._grows_down = value
            self._hovered = None
            self._version += 1
            self.update()

    @property
    def max_height(self) -> int | None:
        """The tallest the indicator may get, from the controller, by the screen it's on; the
        rows scroll rather than run past it."""
        return self._max_height

    @max_height.setter
    def max_height(self, value: int | None) -> None:
        if value != self._max_height:
            self._max_height = value
            self._version += 1
            self.update()

    @property
    def is_list(self) -> bool:
        return len(self._sessions) >= TallyView.LIST_FROM

    @property
    def is_dragging(self) -> bool:
        return bool(self._press and self._press[2])

    @property
    def chip(self) -> Chip:
        return TallyView.SINGLE if len(self._sessions) <= 1 else TallyView.SEVERAL

    def apply(self, sessions: list[Session]) -> None:
        first = not self._primed
        self._primed = True
        if sessions == self._sessions:
            return
        # On the first look nothing has just happened, so nothing replays its entry.
        now = self._epoch - 60 if first else time.monotonic()
        previous = {s.id: s.state for s in self._sessions}
        for session in sessions:
            if previous.get(session.id) != session.state:
                self._entered[session.id] = now      # this session just changed: replay its entry
        ids = {s.id for s in sessions}
        self._entered = {k: v for k, v in self._entered.items() if k in ids}
        self._sessions = list(sessions)
        self._hovered = None
        self._version += 1
        self._recompute()
        self.update()

    def apply_usage(self, usage: Usage | None, now: float | None = None) -> None:
        """The plan's usage limits, or None to show none. Redraws only when what's shown
        changes, which the countdowns do once a minute."""
        now = time.time() if now is None else now
        self._usage = usage
        meters: tuple[Meter, ...] = ()
        compact: tuple[Meter, ...] = ()
        dim = False
        if usage is not None:
            current = usage.at(now)
            drawn = {limit: Meter(limit.short, limit.tag or limit.short, min(max(limit.percent / 100, 0.0), 1.0),
                                  percent_text(limit), when(limit.resets_at, now) or limit.detail, limit.level)
                     for limit in current.limits}
            meters = tuple(drawn.values())
            compact = tuple(drawn[limit] for limit in current.compact)
            dim = usage.is_stale(now)
        if (meters, compact, dim) == (self._meters, self._compact, self._usage_dim):
            return
        self._meters, self._compact, self._usage_dim = meters, compact, dim
        self._version += 1
        self._recompute()
        self.update()

    def settle(self) -> None:
        """Skips every entry animation, as if all of it had happened long ago."""
        self._entered = {k: self._epoch - 60 for k in self._entered}
        self._version += 1
        self.update()

    def preferred_size(self) -> QSize:
        if not self.is_list:
            chip = self.chip
            width = max(1, len(self._rows)) * chip.width
            if not self._compact:
                return QSize(math.ceil(width), math.ceil(chip.height))
            plate = TallyView.PLATE_HEIGHT + TallyView.PLATE_MARGIN
            return QSize(math.ceil(max(width, self._meter_width + 16)), math.ceil(chip.height + plate))
        bar = 2 * TallyView.PADDING + TallyView.HEADER_HEIGHT
        if self._folded:
            return QSize(math.ceil(self._bar_width + (self._meter_width + 14 if self._compact else 0)), bar)
        height = bar + TallyView.HEADER_GAP + self._usage_height + self._view_height
        width = self._list_width + (TallyView.SCROLL_GUTTER if self._scrolls else 0)
        return QSize(math.ceil(max(width, self._bar_width)), math.ceil(height))

    @property
    def is_animating(self) -> bool:
        """False once everything on screen has settled, so redraws can stop."""
        if not self._rows:
            return True                                  # the idle placeholder breathes
        if self.is_list and self._folded:
            return any(self._is_moving(s, TallyView.WASH_DURATION) for s in self._sessions)
        settle = TallyView.WASH_DURATION if self.is_list else TallyView.BOUNCE_DURATION
        return any(self._is_moving(row.session, settle) for row in self._rows)

    def invalidate_motion(self) -> None:
        """Asks for a redraw of only what moves: text costs far more to draw than the marks,
        and it never moves. In the list that's the column of marks plus any row still
        washing; folded, the whole (small) bar; under the badges, everything above the
        captions."""
        w, h = self.width(), self.height()
        if not self.is_list:
            chip = self.chip
            self.update(QRect(0, 0, w, math.ceil(chip.height) - (48 if chip.captioned else 0)))
            return
        if self._folded:
            # The bar's marks move and its numbers don't; all of it only while it washes.
            if self._landed_age < TallyView.WASH_DURATION:
                self.update()
                return
            x = TallyView.PADDING + 4
            for _, count in self._counts:
                self.update(QRect(int(x) - 2, 0, 20, h))
                x += self._count_item_width(count)
            return
        column = TallyView.PADDING + TallyView.TEXT_X - 2
        if self._grows_down:
            edge = math.floor(self._rows_edge)
            self.update(QRect(0, edge, column, h - edge))
        else:
            self.update(QRect(0, 0, column, math.ceil(self._rows_edge)))
        for index, row in enumerate(self._rows):
            if row.session.state is State.DONE and self._is_moving(row.session, TallyView.WASH_DURATION):
                self.update(self._row_rect(index).adjusted(-4, 0, 4, 0).toAlignedRect())

    # MARK: Layout

    def _recompute(self) -> None:
        # Number repeated names in age order, so a row keeps its name as its siblings come and go.
        seen: dict[str, int] = {}
        titles: dict[str, str] = {}
        for session in self._sessions:
            base = session.title or session.label or "Claude"
            seen[base] = seen.get(base, 0) + 1
            titles[session.id] = base if seen[base] == 1 else f"{base} {seen[base]}"

        home = str(Path.home())
        self._rows = [Row(session=s,
                          title=titles.get(s.id, ""),
                          path=("~" + s.path[len(home):]) if s.path and (s.path + "/").startswith(home + "/") else (s.path or ""),
                          tmux=s.tmux or "",
                          agents=s.agents.summary if s.agents else "")
                      for s in self._sessions]
        self._row_offsets = [0.0]
        for row in self._rows:
            self._row_offsets.append(self._row_offsets[-1] + TallyView.ROW_HEIGHT
                                     + (TallyView.AGENT_LINE if row.agents else 0))

        # Wide enough for the longest name or path, within reason; longer ones get shortened.
        f = self.fonts

        def agents_width(row: Row) -> float:
            if not row.agents:
                return 0
            progress = row.session.agents.progress if row.session.agents else None
            extra = 14 + TallyView.PROGRESS_WIDTH + measure(progress, f.tag) if progress else 0
            return 16 + measure(row.agents, f.agent) + extra

        widest = max((max(measure(r.title, f.title) + 8 + TallyView.STATE_WIDTH,
                          measure(r.path, f.path) + (10 + measure(r.tmux, f.tmux) if r.tmux else 0),
                          agents_width(r))
                      for r in self._rows), default=0)
        wanted = 2 * TallyView.PADDING + TallyView.TEXT_X + widest
        low, high = TallyView.WIDTH_RANGE
        self._usage_width = self._usage_line_width() if self._meters else 0
        self._list_width = min(max(wanted, self._usage_width, low), high)
        self._meter_width = self._small_meter_width() if self._compact else 0

        # The count bar covers every session, scrolled out of sight or not, most urgent first.
        self._counts = [(state, n) for state in BY_URGENCY
                        if (n := sum(1 for s in self._sessions if s.state is state))]
        items = sum(self._count_item_width(n) for _, n in self._counts)
        self._bar_width = math.ceil(2 * TallyView.PADDING + 4 + items + TallyView.FOLD_SIZE + 8)

    def _count_item_width(self, count: int) -> float:
        """A count in the bar: small mark, then the number, then a gap before the next."""
        return 2 * 8 + 5 + measure(str(count), self.fonts.count) + 12

    def _usage_columns(self) -> tuple[float, float, float]:
        """The list's usage lines: widths of the label, percentage and reset columns."""
        f = self.fonts
        return (min(max(measure(m.label, f.usage) for m in self._meters), 110),
                max(measure("100%", f.tag), max(measure(m.percent, f.tag) for m in self._meters)),
                min(max(measure(m.when, f.usage_when) for m in self._meters), 110))

    def _usage_line_width(self) -> float:
        label, percent, reset = self._usage_columns()
        return 2 * TallyView.PADDING + 4 + label + 10 + TallyView.USAGE_TRACK + 8 + percent + 10 + reset + 4

    def _meter_columns(self) -> tuple[float, float]:
        f = self.fonts
        return (max(measure(m.tag, f.meter_tag) for m in self._compact),
                max(measure("100%", f.meter), max(measure(m.percent, f.meter) for m in self._compact)))

    def _small_meter_width(self) -> float:
        tag, percent = self._meter_columns()
        return tag + 5 + TallyView.METER_TRACK + 5 + percent

    @property
    def _usage_height(self) -> float:
        """The list's usage block and the gap above it; nothing folded, or with none known."""
        if not self._meters or self._folded:
            return 0.0
        return len(self._meters) * TallyView.USAGE_LINE + TallyView.HEADER_GAP

    @property
    def _rows_height(self) -> float:
        """Every row's, whether it shows or is scrolled away."""
        return self._row_offsets[-1]

    @property
    def _view_height(self) -> float:
        """How much of the rows shows at once: all of them, or as many as fit, the rest
        scrolling. Never under a couple of rows, however short the screen."""
        room = TallyView.MAX_ROWS * TallyView.ROW_HEIGHT
        if self._max_height is not None:
            others = 2 * TallyView.PADDING + TallyView.HEADER_HEIGHT + TallyView.HEADER_GAP + self._usage_height
            room = min(room, max(2 * TallyView.ROW_HEIGHT, self._max_height - others))
        return min(self._rows_height, room)

    @property
    def _scrolls(self) -> bool:
        return self.is_list and not self._folded and self._rows_height > self._view_height + 0.5

    @property
    def _scroll_limit(self) -> float:
        return max(0.0, self._rows_height - self._view_height) if self._scrolls else 0.0

    @property
    def _offset(self) -> float:
        """The scroll in use: what's kept may be past the end, once rows have gone."""
        return min(max(self._scroll, 0.0), self._scroll_limit)

    @property
    def _rows_view(self) -> QRectF:
        """The band of the window the rows show through, edge to edge so a highlight's
        margin isn't cut off."""
        height = self._view_height
        top = self._rows_edge if self._grows_down else self._rows_edge - height
        return QRectF(0, top, self.width(), height)

    @property
    def _bar_rect(self) -> QRectF:
        """The count bar, on the edge the window is pinned by, so it stays put while the rows
        fold away from it."""
        p = TallyView.PADDING
        y = p if self._grows_down else self.height() - p - TallyView.HEADER_HEIGHT
        return QRectF(p, y, self.width() - 2 * p, TallyView.HEADER_HEIGHT)

    @property
    def _rows_edge(self) -> float:
        """Where the rows start, on the bar's side: past the bar and the usage limits."""
        beyond = TallyView.PADDING + TallyView.HEADER_HEIGHT + TallyView.HEADER_GAP + self._usage_height
        return beyond if self._grows_down else self.height() - beyond

    def _row_rect(self, index: int) -> QRectF:
        """Row 0, the oldest session, next to the bar; each later one further out, and all of
        them further in as the list scrolls."""
        if self._grows_down:
            top = self._rows_edge + self._row_offsets[index] - self._offset
        else:
            top = self._rows_edge - self._row_offsets[index + 1] + self._offset
        gutter = TallyView.SCROLL_GUTTER if self._scrolls else 0
        return QRectF(TallyView.PADDING, top, self.width() - 2 * TallyView.PADDING - gutter,
                      self._row_offsets[index + 1] - self._row_offsets[index])

    def _row_shows(self, index: int) -> bool:
        return not self._scrolls or self._row_rect(index).intersects(self._rows_view)

    def _scroll_track(self) -> QRectF:
        """The scroll bar's groove, in the gutter beside the rows."""
        view = self._rows_view
        x = self.width() - TallyView.PADDING + 2 - TallyView.SCROLL_WIDTH
        return QRectF(x, view.y() + 3, TallyView.SCROLL_WIDTH, view.height() - 6)

    def _scroll_thumb(self) -> QRectF:
        """The part of the groove for the rows in view: at the bar's end unscrolled, moving
        away from it as the rows do."""
        track = self._scroll_track()
        length = max(TallyView.SCROLL_MIN_THUMB, track.height() * self._view_height / max(self._rows_height, 1))
        length = min(length, track.height())
        travel = (track.height() - length) * self._offset / max(self._scroll_limit, 1)
        y = track.y() + travel if self._grows_down else track.bottom() - length - travel
        return QRectF(track.x(), y, track.width(), length)

    def _scroll_to(self, value: float, pointer: QPointF | None = None) -> None:
        """Scrolls the rows; the row under the pointer, if it's over them, is a new one now."""
        value = min(max(value, 0.0), self._scroll_limit)
        if value == self._offset and value == self._scroll:
            return
        self._scroll = value
        self._version += 1
        self.update()
        self._hovered = None
        if pointer is not None:
            self._set_hovered(self._item_at(pointer))

    def _scroll_by_thumb(self, y: float) -> None:
        """Puts the thumb's top (or, opening upward, its bottom) where the pointer drags it."""
        track, thumb = self._scroll_track(), self._scroll_thumb()
        travel = track.height() - thumb.height()
        if travel <= 0:
            return
        moved = (y - track.y()) if self._grows_down else (track.bottom() - thumb.height() - y)
        self._scroll_to(self._scroll_limit * moved / travel)

    def _fold_rect(self) -> QRectF:
        size = TallyView.FOLD_SIZE
        bar = self._bar_rect
        return QRectF(self.width() - TallyView.PADDING - size - 2, bar.y() + (bar.height() - size) / 2, size, size)

    @property
    def _badges_left(self) -> float:
        """The badges sit centred over the usage card when it's the wider."""
        chip = self.chip if self._rows else TallyView.SINGLE
        return max(0.0, (self.width() - max(1, len(self._rows)) * chip.width) / 2)

    def _item_rect(self, index: int) -> QRectF:
        if self.is_list:
            return self._row_rect(index)
        chip = self.chip
        return QRectF(self._badges_left + index * chip.width, 0, chip.width, chip.height)

    def _usage_rect(self) -> QRectF | None:
        """Where the usage limits are drawn: the list's block, the folded bar's meters, or
        the card under the badges."""
        if self.is_list and self._folded and self._compact:
            fold = self._fold_rect()
            bar = self._bar_rect
            return QRectF(fold.x() - 10 - self._meter_width, bar.y(), self._meter_width, bar.height())
        if self.is_list and not self._folded and self._meters:
            height = len(self._meters) * TallyView.USAGE_LINE
            bar = self._bar_rect
            y = bar.bottom() + TallyView.HEADER_GAP if self._grows_down else bar.y() - TallyView.HEADER_GAP - height
            return QRectF(bar.x(), y, bar.width(), height)
        if not self.is_list and self._compact:
            width = min(self._meter_width + 16, self.width() - 4)
            top = (self.chip if self._rows else TallyView.SINGLE).height
            return QRectF((self.width() - width) / 2, top, width, TallyView.PLATE_HEIGHT)
        return None

    def _item_at(self, point: QPointF) -> int | None:
        """The row or badge under a point, if it stands for a session."""
        if not self._rows or (self.is_list and self._folded):
            return None
        if self._scrolls and not self._rows_view.contains(point):
            return None                              # a row scrolled under the bar or the edge
        return next((i for i in range(len(self._rows)) if self._item_rect(i).contains(point)), None)

    def _on_scroll_bar(self, point: QPointF) -> bool:
        """Over the scroll bar, with a little to spare: it's thin."""
        return self._scrolls and self._scroll_track().adjusted(-4, -3, 4, 3).contains(point)

    # MARK: Clicks, drags and hovering

    def _set_hovered(self, index: int | None) -> None:
        if index == self._hovered:
            return
        for old in (self._hovered, index):
            if old is not None and old < len(self._rows):
                self.update(self._item_rect(old).adjusted(-4, -2, 4, 2).toAlignedRect())
        self._hovered = index
        self._version += 1

    def _set_over_scroll_bar(self, over: bool) -> None:
        if over != self._over_scroll_bar:
            self._over_scroll_bar = over
            self._version += 1
            self.update(self._scroll_track().adjusted(-3, -3, 3, 3).toAlignedRect())

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        point = event.position()
        if self._on_scroll_bar(point):
            # The thumb is held where it's pressed; pressed beside it, it jumps there first.
            thumb = self._scroll_thumb()
            if not thumb.top() - 3 <= point.y() <= thumb.bottom() + 3:
                self._scroll_by_thumb(point.y() - thumb.height() / 2)
                thumb = self._scroll_thumb()
            self._grab = point.y() - thumb.y()
            self._press = None
            return
        self._press = [event.globalPosition(), self.window().pos(), False]

    def wheelEvent(self, event) -> None:
        """The wheel or two fingers scroll the rows, once there are more than fit. The rows
        follow the fingers: moving them down shows what's above."""
        if not self._scrolls:
            event.ignore()
            return
        pixels = event.pixelDelta().y()
        down = pixels if pixels else event.angleDelta().y() / 120 * TallyView.ROW_HEIGHT
        if down:
            self._scroll_to(self._offset + (-down if self._grows_down else down), event.position())
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._grab is not None:
            self._scroll_by_thumb(event.position().y() - self._grab)
            return
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            self._set_over_scroll_bar(self._on_scroll_bar(event.position()))
            self._set_hovered(self._item_at(event.position()))
            return
        start, origin, dragging = self._press
        delta = event.globalPosition() - start
        if not dragging and math.hypot(delta.x(), delta.y()) < 3:
            return                                   # a wobble, not a drag
        if not dragging:
            self._press[2] = True
            self._set_hovered(None)
            if QGuiApplication.platformName() == "wayland":
                # Wayland lets no window place itself; the compositor can carry it.
                handle = self.windowHandle()
                self._press = None
                if handle:
                    handle.startSystemMove()
                return
        self.window().move(origin + delta.toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._grab is not None:
            self._grab = None
            self._set_over_scroll_bar(self._on_scroll_bar(event.position()))
            return
        if event.button() != Qt.LeftButton or self._press is None:
            return
        dragging = self._press[2]
        self._press = None
        if dragging:
            self.drag_finished.emit()
            return
        point = event.position()
        if self.is_list and self._fold_rect().contains(point):
            self.fold_requested.emit(not self._folded)
            return
        index = self._item_at(point)
        if index is not None:
            self.selected.emit(self._rows[index].session, event.timestamp())

    def contextMenuEvent(self, event) -> None:
        self._press = self._grab = None
        self.menu_requested.emit(event.globalPos())

    def leaveEvent(self, event) -> None:
        self._set_hovered(None)
        if self._grab is None:
            self._set_over_scroll_bar(False)

    def event(self, event) -> bool:
        """Hovering over the usage limits spells them out: every limit, its reset, and how old
        the numbers are."""
        if event.type() == QEvent.ToolTip:
            area = self._usage_rect()
            if self._usage is not None and area is not None and area.contains(QPointF(event.pos())):
                QToolTip.showText(event.globalPos(), "\n".join(describe(self._usage, time.time())), self,
                                  area.toAlignedRect())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    # MARK: Motion

    def _age(self, session: Session) -> float:
        return time.monotonic() - self._entered.get(session.id, self._epoch - 60)

    def _is_moving(self, session: Session, settles_after: float) -> bool:
        if session.state is State.DONE:
            return self._age(session) < settles_after
        return True

    @property
    def _landed_age(self) -> float:
        """How long ago the most recent turn landed."""
        return min((self._age(s) for s in self._sessions if s.state is State.DONE), default=math.inf)

    def _motion(self, state: State, age: float, working: float = 2, waiting: float = 1.5,
                bounce: float = 5) -> tuple[float, float]:
        """How a mark moves: (rise, glow). A slow bob while working or waiting, one damped
        bounce when a turn lands and then dead still, a breath while idle. Heights are in
        points; the defaults suit the list's small marks."""
        clock = time.monotonic() - self._epoch
        if state is State.WORKING:
            return working * math.sin(2 * math.pi * clock / 1.6), 1.0
        if state is State.WAITING:
            return waiting * math.sin(2 * math.pi * clock / 1.1), 1.0
        if state is State.DONE:
            if age >= TallyView.BOUNCE_DURATION:
                return 0.0, 1.0
            return bounce * math.exp(-3.2 * age) * abs(math.sin(2 * math.pi * age / 0.7)), 1.0
        return 0.0, self._breath()

    def _breath(self) -> float:
        """Slow breath for idle marks; the shape itself holds steady."""
        return 0.32 + 0.58 * (0.5 + 0.5 * math.sin(2 * math.pi * (time.monotonic() - self._epoch) / 4.0))

    # MARK: Drawing

    def _needs(self, rect: QRectF) -> bool:
        return self._region.intersects(rect.toAlignedRect())

    def paintEvent(self, event) -> None:
        self._region = event.region()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.is_list and self._landed_age < TallyView.WASH_DURATION:
            # A wash passes under a row's text, so for its few seconds draw everything.
            self._draw_all(painter)
        else:
            painter.drawPixmap(0, 0, self._still_layer())
            self._draw_moving(painter)
        painter.end()

    def _draw_all(self, painter: QPainter) -> None:
        if self.is_list:
            self._draw_list(painter)
            return
        if not self._rows:
            single = TallyView.SINGLE
            self._draw_chip(painter, self._placeholder(),
                            QRectF(self._badges_left, 0, single.width, single.height), single)
        else:
            chip = self.chip
            for index, row in enumerate(self._rows):
                self._draw_chip(painter, row, self._item_rect(index), chip, highlighted=index == self._hovered)
        plate = self._usage_rect()
        if plate is not None and self._needs(plate):
            self._draw_usage_plate(painter, plate)

    @staticmethod
    def _placeholder() -> Row:
        return Row(Session(id="", state=State.IDLE, label=""), "", "", "", "")

    def _still_layer(self) -> QPixmap:
        """The still parts, redrawn only when they change."""
        ratio = self.devicePixelRatioF()
        key = (self._version, self.width(), self.height(), ratio)
        if self._layer is None or self._layer_key != key:
            layer = QPixmap(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))
            layer.setDevicePixelRatio(ratio)
            layer.fill(Qt.transparent)
            painter = QPainter(layer)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.TextAntialiasing)
            region, self._region = self._region, QRegion(self.rect())
            self._still_only = True
            try:
                self._draw_all(painter)
            finally:
                self._still_only = False
                self._region = region
                painter.end()
            self._layer, self._layer_key = layer, key
        return self._layer

    def _draw_moving(self, painter: QPainter) -> None:
        """The marks: the badges whole, the list's column of marks, or a folded bar's."""
        if not self.is_list:
            chip = self.chip if self._rows else TallyView.SINGLE
            left = self._badges_left
            for index, row in enumerate(self._rows or [self._placeholder()]):
                rect = QRectF(left + index * chip.width, 0, chip.width, chip.height)
                if self._needs(rect):
                    self._draw_badge(painter, row, rect, chip, highlighted=index == self._hovered)
            return
        if self._folded:
            bar = self._bar_rect
            x = bar.x() + 4
            for state, count in self._counts:
                rise, glow = self._motion(state, self._landed_age)
                self._draw_sprite(painter, state, 8, QPointF(x + 8, bar.center().y() - rise), glow)
                x += self._count_item_width(count)
            return
        scrolls = self._scrolls
        if scrolls:
            painter.save()
            painter.setClipRect(self._rows_view)
        for index, row in enumerate(self._rows):
            if not self._row_shows(index):
                continue
            main = self._row_rect(index)
            centre = QPointF(main.x() + TallyView.MARK_X, main.y() + TallyView.ROW_HEIGHT / 2)
            if self._needs(QRectF(centre.x() - 12, centre.y() - 18, 24, 36)):
                rise, glow = self._motion(row.session.state, self._age(row.session))
                self._draw_sprite(painter, row.session.state, TallyView.MARK_RADIUS,
                                  QPointF(centre.x(), centre.y() - rise), glow)
        if scrolls:
            painter.restore()

    def _sprite(self, key: tuple, size: float, draw) -> QPixmap:
        """A small image drawn once and reused: `draw(painter, centre)` paints it."""
        ratio = self.devicePixelRatioF()
        full = key + (ratio,)
        sprite = self._sprites.get(full)
        if sprite is None:
            sprite = QPixmap(max(1, round(size * ratio)), max(1, round(size * ratio)))
            sprite.setDevicePixelRatio(ratio)
            sprite.fill(Qt.transparent)
            painter = QPainter(sprite)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.TextAntialiasing)
            draw(painter, QPointF(size / 2, size / 2))
            painter.end()
            self._sprites[full] = sprite
        return sprite

    def _blit(self, painter: QPainter, sprite: QPixmap, centre: QPointF, opacity: float = 1.0) -> None:
        size = sprite.width() / sprite.devicePixelRatio()
        if opacity < 1:
            painter.save()
            painter.setOpacity(opacity)
        painter.drawPixmap(QPointF(centre.x() - size / 2, centre.y() - size / 2), sprite)
        if opacity < 1:
            painter.restore()

    def _draw_sprite(self, painter: QPainter, state: State, radius: float, centre: QPointF, glow: float) -> None:
        """A small mark, from its image."""
        size = 2 * radius + 6
        sprite = self._sprite(("mark", state, radius), size,
                              lambda p, c: self._draw_small_mark(p, state, c, 1.0, radius=radius))
        self._blit(painter, sprite, centre, glow)

    def _draw_badge(self, painter: QPainter, row: Row, rect: QRectF, chip: Chip, highlighted: bool) -> None:
        """A badge's hexagon, from images: the plate, then the ring and glyph, which breathe
        while idle, then the agents tag."""
        session = row.session
        rise, glow = self._motion(session.state, self._age(session), working=5, waiting=3, bounce=chip.bounce)
        centre = QPointF(rect.center().x(), chip.centre_y - rise)
        radius = chip.radius
        size = 2 * radius + 6
        lit = highlighted and not chip.captioned

        def plate(p: QPainter, c: QPointF) -> None:
            path = hexagon_path(c, radius)
            p.fillPath(path, white(0.2 if lit else 0.04, 0.44))
            p.strokePath(path, QPen(white(1, 0.10), 1))

        def mark(p: QPainter, c: QPointF) -> None:
            self._draw_ring_and_glyph(p, session.state, c, radius)

        self._blit(painter, self._sprite(("plate", radius, lit), size, plate), centre)
        self._blit(painter, self._sprite(("ring", session.state, radius), size, mark), centre, glow)
        if session.agents:
            self._draw_agent_tag(painter, session.agents.running,
                                 QPointF(centre.x() + radius * 0.8, centre.y() - radius * 0.66))

    # Badges

    def _draw_chip(self, painter: QPainter, row: Row, rect: QRectF, chip: Chip, highlighted: bool = False) -> None:
        if chip.captioned and self._needs(QRectF(rect.x(), rect.height() - 48, rect.width(), 48)):
            self._draw_caption(painter, row, rect, highlighted)
        if self._still_only:
            return

        session = row.session
        rise, glow = self._motion(session.state, self._age(session), working=5, waiting=3, bounce=chip.bounce)
        centre = QPointF(rect.center().x(), chip.centre_y - rise)
        radius = chip.radius

        # Smoked-glass chip, so the mark survives a light wallpaper.
        plate = hexagon_path(centre, radius)
        painter.fillPath(plate, white(0.2 if highlighted and not chip.captioned else 0.04, 0.44))
        painter.strokePath(plate, QPen(white(1, 0.10), 1))

        painter.save()
        painter.setOpacity(glow)
        self._draw_ring_and_glyph(painter, session.state, centre, radius)
        painter.restore()

        if session.agents:
            self._draw_agent_tag(painter, session.agents.running,
                                 QPointF(centre.x() + radius * 0.8, centre.y() - radius * 0.66))

    @staticmethod
    def _draw_ring_and_glyph(painter: QPainter, state: State, centre: QPointF, radius: float) -> None:
        """Segmented ring, six edges with the corners left open, around the state's glyph."""
        accent = ACCENT[state]
        points = hexagon(centre, radius * 0.82)
        pen = QPen(faded(accent, 0.9), 2.5 * radius / 44)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        for i in range(6):
            a, b = points[i], points[(i + 1) % 6]
            painter.drawLine(lerp(a, b, 0.17), lerp(a, b, 0.83))
        draw_glyph(painter, state, centre, radius / 44, accent)

    def _draw_agent_tag(self, painter: QPainter, count: int, c: QPointF) -> None:
        """On a badge's shoulder while its agents run: how many."""
        text = f"⚙ {count}"
        width = measure(text, self.fonts.tag) + 10
        tag = QRectF(c.x() - width / 2, c.y() - 8, width, 16)
        accent = ACCENT[State.WORKING]
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(0.04, 0.8))
        painter.drawRoundedRect(tag, 8, 8)
        painter.setPen(QPen(faded(accent, 0.6), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(tag.adjusted(0.5, 0.5, -0.5, -0.5), 7.5, 7.5)
        draw_text(painter, text, tag, self.fonts.tag, accent, Qt.AlignHCenter)

    def _draw_caption(self, painter: QPainter, row: Row, rect: QRectF, highlighted: bool) -> None:
        """Which session this badge is: its name over its directory, on a card of their own so
        they stay legible on any wallpaper."""
        card = QRectF(rect.x() + 6, rect.height() - 8 - 38, rect.width() - 12, 38)
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(0.2 if highlighted else 0.04, 0.55))
        painter.drawRoundedRect(card, 8, 8)
        inner = card.adjusted(7, 3, -7, -3)
        half = inner.height() / 2
        draw_text(painter, row.title, QRectF(inner.x(), inner.y(), inner.width(), half),
                  self.fonts.caption_title, white(0.95, 1), Qt.AlignHCenter)
        # In tmux, where to find it there; otherwise where it runs.
        detail = row.path if not row.tmux else f"{row.tmux} · {row.path.rstrip('/').rsplit('/', 1)[-1]}"
        draw_text(painter, detail, QRectF(inner.x(), inner.y() + half, inner.width(), half),
                  self.fonts.caption_path, white(0.95, 0.6), Qt.AlignHCenter, Qt.ElideMiddle)

    # The list

    def _draw_list(self, painter: QPainter) -> None:
        # One smoked-glass plate behind the list, so it survives a light wallpaper.
        plate = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(white(1, 0.10), 1))
        painter.setBrush(white(0.04, 0.55))
        painter.drawRoundedRect(plate, 12, 12)

        bar = self._bar_rect
        if self._needs(bar.adjusted(0, -6, 0, 6)):
            self._draw_count_bar(painter, bar)
        fold = self._fold_rect()
        if self._needs(fold):
            self._draw_fold_button(painter, fold)
        if self._folded:
            return

        # Hairlines between the count bar, the usage limits and the rows.
        half = TallyView.HEADER_GAP / 2 * (-1 if self._grows_down else 1)
        painter.fillRect(QRectF(bar.x() + 4, self._rows_edge + half - 0.5, bar.width() - 8, 1), white(1, 0.08))
        usage = self._usage_rect()
        if usage is not None:
            beside_bar = bar.bottom() + TallyView.HEADER_GAP / 2 if self._grows_down else bar.y() - TallyView.HEADER_GAP / 2
            painter.fillRect(QRectF(bar.x() + 4, beside_bar - 0.5, bar.width() - 8, 1), white(1, 0.08))
            if self._needs(usage):
                self._draw_usage_lines(painter, usage)
        # Scrolling, rows are cut off where the band they show through ends.
        scrolls = self._scrolls
        if scrolls:
            painter.save()
            painter.setClipRect(self._rows_view)
        for index, row in enumerate(self._rows):
            if not self._row_shows(index):
                continue
            rect = self._row_rect(index)
            # Bounces reach a little beyond the row.
            if self._needs(rect.adjusted(-4, -6, 4, 6)):
                self._draw_row(painter, row, rect, highlighted=index == self._hovered)
        if scrolls:
            painter.restore()
            track = self._scroll_track()
            if self._needs(track.adjusted(-3, -3, 3, 3)):
                self._draw_scroll_bar(painter, track)

    def _draw_scroll_bar(self, painter: QPainter, track: QRectF) -> None:
        """A groove, the thumb for the rows in view, brighter under the pointer, and a tick
        for each session waiting on me, so none is missed for being scrolled away."""
        radius = track.width() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(1, 0.08))
        painter.drawRoundedRect(track, radius, radius)
        lit = self._over_scroll_bar or self._grab is not None
        painter.setBrush(white(1, 0.55 if lit else 0.32))
        painter.drawRoundedRect(self._scroll_thumb(), radius, radius)
        total = max(self._rows_height, 1)
        painter.setBrush(ACCENT[State.WAITING])
        for index, row in enumerate(self._rows):
            if row.session.state is not State.WAITING:
                continue
            middle = (self._row_offsets[index] + TallyView.ROW_HEIGHT / 2) / total * track.height()
            y = track.y() + middle if self._grows_down else track.bottom() - middle
            painter.drawRoundedRect(QRectF(track.x() - 1, y - 1.5, track.width() + 2, 3), 1.5, 1.5)

    def _draw_fold_button(self, painter: QPainter, rect: QRectF) -> None:
        """The chevron that folds the list: pointing the way the rows will open, or, open, the
        way they fold away."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(1, 0.08))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        c = rect.center()
        points_up = self._folded != self._grows_down
        rise = 2.5 if points_up else -2.5
        pen = QPen(white(1, 0.75), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        chevron = QPainterPath(QPointF(c.x() - 4.5, c.y() + rise))
        chevron.lineTo(c.x(), c.y() - rise)
        chevron.lineTo(c.x() + 4.5, c.y() + rise)
        painter.drawPath(chevron)

    def _draw_count_bar(self, painter: QPainter, bar: QRectF) -> None:
        """Sessions counted by state, most urgent first. Folded, the bar is all there is, so
        it carries the motion the rows otherwise would, the green wash and bounce included;
        unfolded, the rows move and the bar holds still."""
        landed = self._landed_age
        if self._folded and landed < TallyView.WASH_DURATION:
            fade = 1 - landed / TallyView.WASH_DURATION
            painter.setPen(Qt.NoPen)
            painter.setBrush(faded(ACCENT[State.DONE], 0.30 * fade))
            painter.drawRoundedRect(bar.adjusted(-3, 0, 3, 0), 8, 8)

        x = bar.x() + 4
        for state, count in self._counts:
            if not (self._still_only and self._folded):
                rise, glow = self._motion(state, landed) if self._folded else (0.0, 1.0)
                self._draw_small_mark(painter, state, QPointF(x + 8, bar.center().y() - rise), glow, radius=8)
            number = QRectF(x + 21, bar.y(), 40, bar.height())
            if self._needs(number):
                draw_text(painter, str(count), number, self.fonts.count, ACCENT[state])
            x += self._count_item_width(count)
        meters = self._usage_rect() if self._folded else None
        if meters is not None and self._needs(meters):
            painter.fillRect(QRectF(meters.x() - 8, bar.y() + 5, 1, bar.height() - 10), white(1, 0.10))
            self._draw_small_meters(painter, meters)

    def _draw_row(self, painter: QPainter, row: Row, rect: QRectF, highlighted: bool) -> None:
        state = row.session.state
        age = self._age(row.session)

        # Under the pointer: clicking brings this session's terminal forward.
        if highlighted:
            painter.setPen(Qt.NoPen)
            painter.setBrush(white(1, 0.08))
            painter.drawRoundedRect(rect.adjusted(-3, 1, 3, -1), 7, 7)
        # The one memorable moment: a finished turn washes its row green and its mark
        # bounces once, then both settle and stay.
        if state is State.DONE and age < TallyView.WASH_DURATION:
            painter.setPen(Qt.NoPen)
            painter.setBrush(faded(ACCENT[state], 0.30 * (1 - age / TallyView.WASH_DURATION)))
            painter.drawRoundedRect(rect.adjusted(-3, 1, 3, -1), 7, 7)

        # The session itself takes the top of the row; what its agents are doing, if any
        # run, a line under it.
        main = QRectF(rect.x(), rect.y(), rect.width(), TallyView.ROW_HEIGHT)
        if not self._still_only:
            rise, glow = self._motion(state, age)
            self._draw_small_mark(painter, state, QPointF(main.x() + TallyView.MARK_X, main.center().y() - rise), glow)

        left = rect.x() + TallyView.TEXT_X
        width = rect.right() - left
        if not self._needs(QRectF(left, rect.y(), width, rect.height())):
            return

        if row.agents:
            line = QRectF(left, rect.bottom() - 1 - TallyView.AGENT_LINE, width, TallyView.AGENT_LINE)
            agents = row.session.agents
            if agents and agents.progress:
                line.setWidth(line.width() - self._draw_progress(painter, agents.progress, agents.done,
                                                                 agents.started, line.right(), line.center().y()) - 8)
            draw_text(painter, "⚙ " + row.agents, line, self.fonts.agent, faded(ACCENT[State.WORKING], 0.85))

        # Name and state on the upper line, the directory under them; with no directory known
        # yet, the name line sits centred instead.
        half = main.height() / 2
        upper = (QRectF(left, main.y(), width, main.height()) if not row.path
                 else QRectF(left, main.y() + 2, width, half - 2))
        lower = QRectF(left, main.y() + half, width, half - 2)
        draw_text(painter, row.title, QRectF(upper.x(), upper.y(), upper.width() - TallyView.STATE_WIDTH - 6, upper.height()),
                  self.fonts.title, white(0.95, 1))
        word = "limit" if row.session.limited else state.value     # stopped by the usage limit
        draw_text(painter, word, QRectF(upper.right() - TallyView.STATE_WIDTH, upper.y(), TallyView.STATE_WIDTH, upper.height()),
                  self.fonts.state, ACCENT[state], Qt.AlignRight)
        # Where it sits in tmux, at the end of the directory line; the path gives way to it.
        path_line = QRectF(lower)
        if row.tmux:
            tmux_width = min(measure(row.tmux, self.fonts.tmux), lower.width() / 2)
            draw_text(painter, row.tmux, QRectF(lower.right() - tmux_width, lower.y(), tmux_width, lower.height()),
                      self.fonts.tmux, white(0.95, 0.5), Qt.AlignRight)
            path_line.setWidth(path_line.width() - tmux_width - 10)
        draw_text(painter, row.path, path_line, self.fonts.path, white(0.95, 0.6), elide=Qt.ElideMiddle)

    def _draw_progress(self, painter: QPainter, label: str, done: int, started: int,
                       right: float, mid_y: float) -> float:
        """A workflow's agents finished out of those started, as a bar and in numbers,
        right-aligned to a point. Returns the width it took."""
        accent = ACCENT[State.WORKING]
        label_width = measure(label, self.fonts.tag)
        draw_text(painter, label, QRectF(right - label_width, mid_y - 7, label_width, 14),
                  self.fonts.tag, faded(accent, 0.85), Qt.AlignRight)
        track = QRectF(right - label_width - 6 - TallyView.PROGRESS_WIDTH, mid_y - 2, TallyView.PROGRESS_WIDTH, 4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(1, 0.12))
        painter.drawRoundedRect(track, 2, 2)
        share = min(done, started) / max(started, 1)
        if share > 0:
            filled = QRectF(track)
            filled.setWidth(max(4.0, track.width() * share))
            painter.setBrush(faded(accent, 0.9))
            painter.drawRoundedRect(filled, 2, 2)
        return right - track.x()

    # Usage limits

    def _draw_usage_lines(self, painter: QPainter, rect: QRectF) -> None:
        """Every limit, a line each: its name, a bar, the share used, and when it resets."""
        f = self.fonts
        label_width, percent_width, reset_width = self._usage_columns()
        painter.save()
        if self._usage_dim:
            painter.setOpacity(0.5)
        for index, meter in enumerate(self._meters):
            line = QRectF(rect.x() + 4, rect.y() + index * TallyView.USAGE_LINE, rect.width() - 8, TallyView.USAGE_LINE)
            draw_text(painter, meter.label, QRectF(line.x(), line.y(), label_width, line.height()),
                      f.usage, white(0.95, 0.75))
            reset = QRectF(line.right() - reset_width, line.y(), reset_width, line.height())
            draw_text(painter, meter.when, reset, f.usage_when, white(0.95, 0.5), Qt.AlignRight)
            percent = QRectF(reset.x() - 10 - percent_width, line.y(), percent_width, line.height())
            draw_text(painter, meter.percent, percent, f.tag,
                      LEVEL[meter.level] if meter.level else white(0.95, 0.9), Qt.AlignRight)
            left = line.x() + label_width + 10
            self._draw_track(painter, QRectF(left, line.center().y() - 2, percent.x() - 8 - left, 4), meter)
        painter.restore()

    def _draw_small_meters(self, painter: QPainter, rect: QRectF) -> None:
        """The session and the week, one over the other: "5h ▬▬▭ 71%"."""
        f = self.fonts
        tag_width, percent_width = self._meter_columns()
        track_width = max(16.0, rect.width() - tag_width - 5 - 5 - percent_width)
        top = rect.center().y() - len(self._compact) * TallyView.METER_LINE / 2
        painter.save()
        if self._usage_dim:
            painter.setOpacity(0.5)
        for index, meter in enumerate(self._compact):
            line = QRectF(rect.x(), top + index * TallyView.METER_LINE, rect.width(), TallyView.METER_LINE)
            draw_text(painter, meter.tag, QRectF(line.x(), line.y(), tag_width, line.height()),
                      f.meter_tag, white(0.95, 0.6))
            track = QRectF(line.x() + tag_width + 5, line.center().y() - 1.5, track_width, 3)
            self._draw_track(painter, track, meter)
            draw_text(painter, meter.percent, QRectF(track.right() + 5, line.y(), percent_width, line.height()),
                      f.meter, LEVEL[meter.level] if meter.level else white(0.95, 0.85), Qt.AlignRight)
        painter.restore()

    def _draw_usage_plate(self, painter: QPainter, rect: QRectF) -> None:
        """Under the badges, the small meters on a card of their own, like the captions."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(0.04, 0.55))
        painter.drawRoundedRect(rect, 8, 8)
        self._draw_small_meters(painter, rect.adjusted(8, 0, -8, 0))

    @staticmethod
    def _draw_track(painter: QPainter, track: QRectF, meter: Meter) -> None:
        radius = track.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(white(1, 0.12))
        painter.drawRoundedRect(track, radius, radius)
        if meter.share > 0:
            filled = QRectF(track)
            filled.setWidth(max(track.height(), track.width() * meter.share))
            painter.setBrush(faded(LEVEL[meter.level], 0.9))
            painter.drawRoundedRect(filled, radius, radius)

    def _draw_small_mark(self, painter: QPainter, state: State, c: QPointF, glow: float,
                         radius: float = MARK_RADIUS) -> None:
        """A small tinted hexagon with the state's glyph: a solid outline rather than the big
        badge's segmented ring, which turns to noise at this size."""
        painter.save()
        painter.setOpacity(glow)
        accent = ACCENT[state]
        plate = hexagon_path(c, radius)
        painter.fillPath(plate, faded(accent, 0.16))
        painter.strokePath(plate, QPen(faded(accent, 0.9), 1.2))
        draw_glyph(painter, state, c, radius / 44 * 1.3, accent)
        painter.restore()
