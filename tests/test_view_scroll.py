"""More sessions than fit scroll, with the wheel or the scroll bar, rather than hide behind a
"+N more"; the list never runs taller than the screen. Qt runs in a separate process, so it
never starts inside the test run."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent('''
    import os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.app import INSET, Controller
    from ctally.gui.view import TallyView
    from ctally.sessions import Session, State

    def many(n):
        sessions = snapshot.demo_sessions()
        for i in range(len(sessions) + 1, n + 1):
            sessions.append(Session(f"demo-{i}", State.WAITING if i == n - 1 else State.IDLE, f"p{i}", 100 + i,
                                    f"Session {i}", f"/tmp/p{i}"))
        return sessions[:n]

    def mouse(view, kind, point, buttons=Qt.LeftButton):
        point = QPointF(point)
        view.event(QMouseEvent(kind, point, QPointF(view.mapToGlobal(point.toPoint())),
                               Qt.LeftButton, buttons, Qt.NoModifier))

    def click(view, point):
        chosen = []
        view.selected.connect(lambda session, when: chosen.append(session))
        mouse(view, QEvent.MouseButtonPress, point)
        mouse(view, QEvent.MouseButtonRelease, point, Qt.NoButton)
        view.selected.disconnect()
        return chosen

    def wheel(view, dy, pixels=False):
        point = QPointF(view.width() / 2, view.height() / 2)
        delta = QPoint(0, dy)
        event = QWheelEvent(point, QPointF(view.mapToGlobal(point.toPoint())), delta if pixels else QPoint(),
                            QPoint() if pixels else delta, Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        app.sendEvent(view, event)
        return event.isAccepted()

    def make(n, down=False, usage=True):
        view = TallyView()
        view.apply(many(n))
        if usage:
            view.apply_usage(snapshot.demo_usage(time.time()))
        view.grows_down = down
        view.settle()
        view.resize(view.preferred_size())
        view.show()
        return view

    full = TallyView.MAX_ROWS * TallyView.ROW_HEIGHT

    # Rows that fit don't scroll, and leave no gutter for a scroll bar.
    view = make(14, usage=False)
    assert view._rows_height <= full and view._view_height == view._rows_height
    assert not view._scrolls and view._scroll_limit == 0 and len(view._rows) == 14
    assert not wheel(view, 120) and view._offset == 0
    narrow = view.width()

    for down in (False, True):
        sessions = many(20)
        view = make(20, down)
        # Every session has a row; the band they show through holds sixteen rows' height.
        assert len(view._rows) == 20 and view._scrolls, down
        assert view._view_height == full and view._scroll_limit == view._rows_height - full > 0
        assert view.width() >= narrow + TallyView.SCROLL_GUTTER - 1
        # The oldest sits by the bar, and the newest is scrolled out of sight past the far edge.
        assert view._row_shows(0) and not view._row_shows(19)
        assert click(view, view._row_rect(0).center()) == [sessions[0]]

        # The wheel scrolls toward the far end, and stops there and back at the start.
        away = 1 if not down else -1                # rows move down to show what's above
        assert wheel(view, 120 * away)
        assert view._offset == TallyView.ROW_HEIGHT, view._offset
        wheel(view, 120 * 50 * away)
        assert view._offset == view._scroll_limit
        last = view._row_rect(19)
        assert view._rows_view.contains(last), (down, last, view._rows_view)
        assert click(view, last.center()) == [sessions[19]]
        wheel(view, -120 * 50 * away)
        assert view._offset == 0
        wheel(view, 30 * away, pixels=True)          # a trackpad, pixel for pixel
        assert view._offset == 30
        # A row scrolled under the edge can't be clicked through what covers it.
        hidden = view._row_rect(19).center()
        assert not view._rows_view.contains(hidden) and click(view, hidden) == []

        # The scroll bar: a press on the groove past the thumb jumps there, and a drag on the
        # thumb carries the rows; neither moves the window or picks a session.
        origin = view.pos()
        track = view._scroll_track()
        far = QPointF(track.center().x(), track.bottom() - 1 if down else track.top() + 1)
        assert click(view, far) == [] and view._offset == view._scroll_limit
        thumb = view._scroll_thumb()
        near = QPointF(track.center().x(), track.top() + 1 if down else track.bottom() - 1)
        mouse(view, QEvent.MouseButtonPress, thumb.center())
        mouse(view, QEvent.MouseMove, near)
        assert view._offset == 0, view._offset
        mouse(view, QEvent.MouseButtonRelease, near, Qt.NoButton)
        assert view.pos() == origin and view._grab is None

        # A tick on the groove for the session waiting on me, wherever it has scrolled to.
        assert sum(1 for r in view._rows if r.session.state is State.WAITING) == 2

        # Folded, nothing scrolls.
        view.folded = True
        assert not view._scrolls and not wheel(view, 120 * away) and view._offset == 0
        view.folded = False

        # Sessions ending pull the scroll back within what's left.
        wheel(view, 120 * 50 * away)
        view.apply(many(17))
        assert view._offset == view._scroll_limit < full
        view.apply(many(10))
        assert not view._scrolls and view._offset == 0 and view._row_shows(9)
        view.hide()

    # A short screen: the band shrinks to what fits, and the rest scrolls.
    view = make(20)
    view.max_height = 400
    assert view.preferred_size().height() <= 400 and view._view_height < full and view._scrolls

    # The controller keeps the whole list on the screen.
    area = app.primaryScreen().availableGeometry()
    c = Controller(app, QLocalServer())
    c.view.apply(many(30))
    c.view.settle()
    c.fit()
    g = c.view.geometry()
    assert c.view.max_height == area.height() - 2 * INSET
    assert g.height() <= area.height() - 2 * INSET and area.contains(g), (g, area)
    assert c.view._scrolls
    print("ok")
''')


def test_many_sessions_scroll(tmp_path):
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
