"""The indicator app: one floating window that never takes focus, a tray icon (the menu bar on
macOS, the top bar on Ubuntu), a settings window, and timers that keep them in step with the
state files CTally's hooks write.

Switch it off and on from the tray icon, the settings window, or the indicator's right-click
menu. Off is remembered, and while off the app sits dormant: only the tray icon stays, and no
timers run. The tray icon's menu also lists your plan's usage limits, whenever they're known.
"""
from __future__ import annotations

import os
import signal
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import focus, prefs, system
from ..sessions import StateReader
from ..usage import UsageReader, describe
from . import mac
from .settings import OPACITY_RANGE, SettingsWindow, app_icon, hexagon
from .view import TallyView

INSET = 24          # the default corner's distance from the screen's edges
POLL_MS = 200       # the hooks' files, five times a second
FRAME_MS = 1000 // 24


def run() -> int:
    """`ctally run`: start the indicator, or, if it's already running, open its settings."""
    _prefer_xwayland()
    if prefs.tell_running("show"):
        return 0

    app = QApplication(sys.argv[:1])
    app.setApplicationName("CTally")
    app.setQuitOnLastWindowClosed(False)    # closing the settings window isn't quitting
    if system.MAC:
        mac.become_accessory()
    else:
        app.setWindowIcon(QIcon(app_icon(128)))

    path = prefs.socket_path()
    server = QLocalServer()
    QLocalServer.removeServer(path)         # one left behind by a crash
    if not server.listen(path):
        print(f"ctally: can't listen on {path} ({server.errorString()})", file=sys.stderr)

    controller = Controller(app, server)
    # Python handles signals only when it gets control back, so give it some regularly. The
    # quit is queued: one asked for before the event loop starts would otherwise be lost.
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: QTimer.singleShot(0, app.quit))
    ticker = QTimer()
    ticker.timeout.connect(lambda: None)
    ticker.start(500)
    controller.start(by_hand=bool(sys.stdin and sys.stdin.isatty()))
    return app.exec()


def _prefer_xwayland() -> None:
    """Native Wayland lets no window keep itself on top or choose where it sits; XWayland
    still does, and Ubuntu runs it alongside every Wayland session."""
    if not system.LINUX or os.environ.get("QT_QPA_PLATFORM"):
        return
    wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY")
    if wayland and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def tray_icon(dimmed: bool) -> QIcon:
    """The hexagon and play mark. On macOS a mask, so the menu bar tints it for light and dark;
    on Linux drawn light, for Ubuntu's dark top bar. Dimmed while CTally is off, so it is
    always one click from coming back."""
    size, ratio = 18, 2.0
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    alpha = 0.35 if dimmed else (1.0 if system.MAC else 0.9)
    colour = QColor.fromRgbF(0, 0, 0, alpha) if system.MAC else QColor.fromRgbF(1, 1, 1, alpha)
    c = QPointF(size / 2, size / 2)

    outline = QPainterPath()
    outline.addPolygon(hexagon(c, 7.5))
    outline.closeSubpath()
    pen = QPen(colour, 1.5)
    pen.setJoinStyle(Qt.RoundJoin)
    p.strokePath(outline, pen)

    play = QPainterPath()
    play.moveTo(c.x() - 2, c.y() - 3.25)
    play.lineTo(c.x() + 3.5, c.y())
    play.lineTo(c.x() - 2, c.y() + 3.25)
    play.closeSubpath()
    p.fillPath(play, colour)
    p.end()

    icon = QIcon(pixmap)
    if system.MAC:
        icon.setIsMask(True)
    return icon


class Controller:
    """Owns the indicator window and keeps it, the tray icon and the settings window in step
    with the one setting that matters: on or off."""

    def __init__(self, app: QApplication, server: QLocalServer):
        self.app = app
        self.server = server
        self.prefs = prefs.Prefs()
        self.reader = StateReader()
        self.usage = UsageReader()
        self.sessions: list = []
        self.settings: SettingsWindow | None = None

        self.view = TallyView()
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        self.view.setWindowFlags(flags)
        self.view.setAttribute(Qt.WA_TranslucentBackground)
        self.view.setAttribute(Qt.WA_ShowWithoutActivating)
        if system.MAC:
            # Otherwise a tool window vanishes whenever the app isn't the active one, which
            # for CTally is always.
            self.view.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.view.setWindowTitle("CTally")
        self.view.folded = bool(self.prefs["folded"])
        self.view.setWindowOpacity(self.opacity)
        self.view.fold_requested.connect(self.set_folded)
        self.view.selected.connect(lambda session: focus.focus(session.pid))
        self.view.drag_finished.connect(self.settle_after_drag)
        self.view.menu_requested.connect(self.show_menu)

        self.poll_timer = QTimer()
        self.poll_timer.setInterval(POLL_MS)
        self.poll_timer.timeout.connect(self.poll)
        self.frame_timer = QTimer()
        self.frame_timer.setInterval(FRAME_MS)
        self.frame_timer.timeout.connect(self.frame)

        self.tray = self._make_tray()

        # Monitors come and go, and the Dock or a panel can change size: the indicator
        # follows, without forgetting the corner I put it in.
        for screen in app.screens():
            screen.availableGeometryChanged.connect(self.fit)
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(lambda *_: QTimer.singleShot(0, self.fit))
        app.primaryScreenChanged.connect(lambda *_: QTimer.singleShot(0, self.fit))

        server.newConnection.connect(self._accept)
        app.aboutToQuit.connect(self._shut_down)

    def start(self, by_hand: bool) -> None:
        self.refresh_controls()
        if self.enabled:
            self.show_tally()
        elif by_hand or self.tray is None:
            # Started while off: show the way back on rather than nothing at all.
            self.show_settings()

    # MARK: Settings

    @property
    def enabled(self) -> bool:
        """Off is remembered, so once switched off it stays off through restarts and logins."""
        return bool(self.prefs["enabled"])

    @property
    def opacity(self) -> float:
        """See-through by default, so the list doesn't blot out what's behind it."""
        try:
            value = float(self.prefs["opacity"])
        except (TypeError, ValueError):
            value = prefs.Prefs.DEFAULTS["opacity"]
        return min(max(value, OPACITY_RANGE[0]), OPACITY_RANGE[1])

    def set_enabled(self, enabled: bool) -> None:
        self.prefs["enabled"] = bool(enabled)
        if enabled:
            self.show_tally()
        else:
            self.hide_tally()
        self.refresh_controls()

    @property
    def show_usage(self) -> bool:
        return bool(self.prefs["usage"])

    def set_show_usage(self, shown: bool) -> None:
        self.prefs["usage"] = bool(shown)
        if self.settings is not None:
            self.settings.update_usage(self.show_usage)
        if self.enabled:
            self.poll()

    def set_opacity(self, value: float) -> None:
        self.prefs["opacity"] = round(min(max(float(value), OPACITY_RANGE[0]), OPACITY_RANGE[1]), 2)
        self.view.setWindowOpacity(self.opacity)

    def set_folded(self, folded: bool) -> None:
        """Remembered, so the list comes back the way I left it."""
        self.prefs["folded"] = bool(folded)
        self.view.folded = bool(folded)
        self.fit()

    def refresh_controls(self) -> None:
        """The tray icon, its menu and the settings window all mirror the one setting."""
        enabled = self.enabled
        if self.tray is not None:
            self.show_action.setChecked(enabled)
            self.tray.setIcon(tray_icon(dimmed=not enabled))
        if self.settings is not None:
            self.settings.update_state(enabled, self.sessions)

    # MARK: Showing and hiding

    def show_tally(self) -> None:
        self.poll()
        self.view.show()
        if system.MAC:
            mac.float_everywhere(self.view)
        if not self.poll_timer.isActive():
            self.poll_timer.start()
            self.frame_timer.start()

    def hide_tally(self) -> None:
        """Off means dormant: nothing on screen and no timers waking the CPU."""
        self.poll_timer.stop()
        self.frame_timer.stop()
        self.view.hide()

    def poll(self) -> None:
        self.sessions = self.reader.poll()
        self.view.apply(self.sessions)
        self.view.apply_usage(self.usage.poll() if self.show_usage else None)
        self.fit()
        if self.settings is not None and self.settings.isVisible():
            self.settings.update_state(self.enabled, self.sessions)

    def frame(self) -> None:
        if self.view.is_animating:
            self.view.invalidate_motion()

    def show_settings(self) -> None:
        if self.settings is None:
            window = SettingsWindow()
            window.toggled.connect(self.set_enabled)
            window.opacity_changed.connect(self.set_opacity)
            window.usage_toggled.connect(self.set_show_usage)
            window.quit_requested.connect(self.app.quit)
            window.closed.connect(self._settings_closed)
            window.winId()                  # a native window to set the Space behaviour on
            if system.MAC:
                mac.follow_active_space(window)
            area = self._screen_under_pointer().availableGeometry()
            window.move(area.center() - window.rect().center())
            self.settings = window
        self.settings.update_state(self.enabled, self.sessions)
        self.settings.update_opacity(self.opacity)
        self.settings.update_usage(self.show_usage)
        if system.MAC:
            mac.activate()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def _settings_closed(self) -> None:
        if system.MAC:
            mac.hide_app()

    # MARK: Menus

    def _make_tray(self) -> QSystemTrayIcon | None:
        """The icon stays put while the indicator is off, dimmed, so it is always one click
        from coming back."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(tray_icon(dimmed=not self.enabled))
        tray.setToolTip("CTally")
        self.tray_menu = QMenu()
        self.usage_actions: list[QAction] = []
        self.tray_menu.aboutToShow.connect(self._list_usage)
        self.show_action = self.tray_menu.addAction("Show CTally")
        self.show_action.setCheckable(True)
        self.show_action.triggered.connect(lambda: self.set_enabled(not self.enabled))
        self.tray_menu.addAction("Settings…").triggered.connect(self.show_settings)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Quit CTally").triggered.connect(self.app.quit)
        tray.setContextMenu(self.tray_menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _list_usage(self) -> None:
        """At the top of the tray menu, each usage limit in words, fresh each time it opens."""
        for action in self.usage_actions:
            self.tray_menu.removeAction(action)
            action.deleteLater()
        self.usage_actions = []
        usage = self.usage.poll()
        if usage is None:
            return
        first = self.tray_menu.actions()[0]
        for line in describe(usage, time.time()):
            action = QAction(line, self.tray_menu)
            action.setEnabled(False)
            self.usage_actions.append(action)
        rule = QAction(self.tray_menu)
        rule.setSeparator(True)
        self.usage_actions.append(rule)
        self.tray_menu.insertActions(first, self.usage_actions)

    def _tray_activated(self, reason) -> None:
        # macOS opens the menu on a click by itself; on Linux a plain click brings up settings.
        if system.MAC or reason != QSystemTrayIcon.Trigger:
            return
        if self.settings is not None and self.settings.isVisible():
            self.settings.close()
        else:
            self.show_settings()

    def show_menu(self, at: QPoint) -> None:
        """The indicator's right-click menu. Folding only means something while the list shows."""
        menu = QMenu()
        hide = menu.addAction("Hide CTally")
        hide.setToolTip("Bring it back from the hexagon icon in the " + ("menu bar." if system.MAC else "top bar."))
        hide.triggered.connect(lambda: self.set_enabled(False))
        if self.view.is_list:
            fold = menu.addAction("Unfold List" if self.view.folded else "Fold List")
            fold.triggered.connect(lambda: self.set_folded(not self.view.folded))
        limits = menu.addAction("Hide Usage Limits" if self.show_usage else "Show Usage Limits")
        limits.triggered.connect(lambda: self.set_show_usage(not self.show_usage))
        menu.addAction("Settings…").triggered.connect(self.show_settings)
        menu.addSeparator()
        menu.addAction("Quit CTally").triggered.connect(self.app.quit)
        menu.exec(at)

    # MARK: Placement

    def fit(self, *_) -> None:
        """Size to what the view wants, keeping the bottom-right corner planted. Also does
        the initial placement, so it compares the position and not just the size."""
        # While I'm dragging it, the pointer places it; it settles once I let go.
        if self.view.is_dragging:
            return
        size = self.view.preferred_size()
        right, bottom = self.anchor()
        target = QRect(right - size.width(), bottom - size.height(), size.width(), size.height())
        # A tall list slides down rather than run off the top of the screen, and a wide one
        # right rather than off the left edge; the saved corner stays where I put it, for
        # when the list shrinks again.
        screen = QGuiApplication.screenAt(QPoint(right - 1, bottom - 1)) or QGuiApplication.primaryScreen()
        if screen is not None:
            target.moveTo(*_clamped(target, screen.availableGeometry()))
        if self.view.geometry() != target:
            self.view.setGeometry(target)

    def anchor(self) -> tuple[int, int]:
        """The bottom-right corner is what's remembered, so it grows leftward and upward. A
        corner saved on a monitor that is no longer attached must not come back off-screen;
        a drop is settled inside the screen, so just inside the corner is enough."""
        saved = self.prefs["anchor"]
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            try:
                right, bottom = round(float(saved[0])), round(float(saved[1]))
            except (TypeError, ValueError):
                right = bottom = None
            if right is not None and any(s.availableGeometry().contains(QPoint(right - 1, bottom - 1))
                                         for s in QGuiApplication.screens()):
                return right, bottom
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 120 + INSET, 120 + INSET
        area = screen.availableGeometry()
        return area.x() + area.width() - INSET, area.y() + area.height() - INSET

    def settle_after_drag(self) -> None:
        """Dropped partly off-screen or over the Dock, it is pulled back inside, and where it
        lands is the corner it grows from from now on."""
        frame = self.view.geometry()
        screen = (QGuiApplication.screenAt(frame.center()) or self.view.screen()
                  or QGuiApplication.primaryScreen())
        if screen is not None:
            x, y = _clamped(frame, screen.availableGeometry())
            if (x, y) != (frame.x(), frame.y()):
                self.view.move(x, y)
                frame.moveTo(x, y)
        self.prefs["anchor"] = [frame.x() + frame.width(), frame.y() + frame.height()]
        self.fit()

    def _screen_added(self, screen) -> None:
        screen.availableGeometryChanged.connect(self.fit)
        QTimer.singleShot(0, self.fit)

    def _screen_under_pointer(self):
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    # MARK: Another ctally asking

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.readyRead.connect(lambda c=connection: self._answer(c))
            connection.disconnected.connect(connection.deleteLater)

    def _answer(self, connection) -> None:
        """`ctally run` while running opens the settings; `ctally setup` and `uninstall` ask
        the old instance to quit, and `setup` checks the new one answers. The reply says it's
        done."""
        if not connection.canReadLine():
            return
        word = bytes(connection.readLine()).decode(errors="replace").strip()
        if word == "show":
            self.show_settings()
        connection.write(b"ok\n")
        connection.flush()
        if word == "quit":
            connection.waitForBytesWritten(500)
            QTimer.singleShot(0, self.app.quit)

    def _shut_down(self) -> None:
        self.poll_timer.stop()
        self.frame_timer.stop()
        self.server.close()             # takes the socket file with it
        if self.tray is not None:
            self.tray.hide()


def _clamped(frame: QRect, area: QRect) -> tuple[int, int]:
    """Where a frame's top-left goes so that the whole frame sits inside an area."""
    x = max(min(frame.x(), area.x() + area.width() - frame.width()), area.x())
    y = max(min(frame.y(), area.y() + area.height() - frame.height()), area.y())
    return x, y
