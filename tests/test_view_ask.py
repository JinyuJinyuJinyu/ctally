"""A session waiting on me says what for, and clicking that asks for its answers, while a click
anywhere else on it still goes to its terminal. Qt runs in a separate process, so it never
starts inside the test run."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent('''
    import os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QContextMenuEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.view import TallyView
    from ctally.sessions import State

    def mouse(view, kind, point, buttons=Qt.LeftButton):
        point = QPointF(point)
        view.event(QMouseEvent(kind, point, QPointF(view.mapToGlobal(point.toPoint())),
                               Qt.LeftButton, buttons, Qt.NoModifier))

    def click(view, point):
        got = []
        view.selected.connect(lambda session, when: got.append(("terminal", session.id)))
        view.answer_requested.connect(lambda session, where: got.append(("answer", session.id)))
        mouse(view, QEvent.MouseButtonPress, point)
        mouse(view, QEvent.MouseButtonRelease, point, Qt.NoButton)
        view.selected.disconnect()
        view.answer_requested.disconnect()
        return got

    def right_click(view, point):
        got = []
        view.menu_requested.connect(lambda where, session: got.append(session.id if session else None))
        point = QPointF(point).toPoint()
        app.sendEvent(view, QContextMenuEvent(QContextMenuEvent.Mouse, point, view.mapToGlobal(point)))
        view.menu_requested.disconnect()
        return got

    def make(sessions):
        view = TallyView()
        view.apply(sessions)
        view.settle()
        view.resize(view.preferred_size())
        view.show()
        return view

    sessions = snapshot.demo_sessions()
    waiting = next(i for i, s in enumerate(sessions) if s.ask)

    # The list: the waiting row is a line taller, and only that line asks for the answers.
    view = make(sessions)
    rect = view._row_rect(waiting)
    assert rect.height() == TallyView.ROW_HEIGHT + TallyView.ASK_LINE
    line = view._lines(rect, view._rows[waiting])["ask"]
    assert click(view, line.center()) == [("answer", "demo-2")]
    assert click(view, QPointF(rect.center().x(), rect.y() + 10)) == [("terminal", "demo-2")]
    # The other rows go to the terminal anywhere, their agents' line included.
    other = view._row_rect(0)
    assert click(view, other.center()) == [("terminal", sessions[0].id)]
    # A right-click says which session it was on, or none.
    assert right_click(view, line.center()) == ["demo-2"]
    assert right_click(view, QPointF(view.width() - 20, view.height() - 20)) == [None]

    # Hovering spells it out.
    text = TallyView.describe_row(view._rows[waiting])
    assert text.splitlines()[0] == "fix-flaky-tests: waiting", text
    assert "Retry the flaky tests, or skip them?" in text and "Click the amber line" in text

    # Badges: the caption says what's asked, and clicking it asks for the answers.
    view = make(sessions[:3])
    badge = view._item_rect(1)
    caption = TallyView._caption_rect(badge)
    assert click(view, caption.center()) == [("answer", "demo-2")]
    assert click(view, QPointF(badge.center().x(), 40)) == [("terminal", "demo-2")]
    assert click(view, TallyView._caption_rect(view._item_rect(0)).center()) == [("terminal", sessions[0].id)]
    print("ok")
''')


def test_asking_from_the_view(tmp_path):
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
