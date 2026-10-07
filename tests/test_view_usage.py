"""Hovering over the usage limits explains them, and clicking them never focuses a session.
Qt runs in a separate process, so it never starts inside the test run."""
from __future__ import annotations

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
    from PySide6.QtGui import QHelpEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication, QToolTip
    app = QApplication([])
    import snapshot
    from ctally.gui.view import TallyView

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def tip(view, point):
        """The tooltip a hover there brings up, once any earlier one has faded."""
        QToolTip.hideText()
        settle(0.5)
        view.event(QHelpEvent(QEvent.ToolTip, point, view.mapToGlobal(point)))
        settle(0.5)
        return QToolTip.text() if QToolTip.isVisible() else ""

    def click(view, point):
        chosen = []
        view.selected.connect(lambda session, when: chosen.append(session))
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            view.event(QMouseEvent(kind, QPointF(point), QPointF(view.mapToGlobal(point)),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        return chosen

    now = time.time()
    for mode, folded in (("one", False), ("three", False), ("list", False), ("list", True)):
        view = TallyView()
        view.apply(snapshot.sessions_for(mode))
        view.apply_usage(snapshot.demo_usage(now))
        view.folded = folded
        view.resize(view.preferred_size())
        view.show()
        area = view._usage_rect().toAlignedRect()
        over = tip(view, area.center())
        assert over.startswith("Current session: 64% used"), (mode, folded, over)
        assert "Current week (Fable): 12% used" in over and over.endswith("Updated just now"), over
        assert tip(view, QPoint(2, 2)) == "", (mode, folded)
        assert click(view, area.center()) == [], (mode, folded)
        view.apply_usage(None)
        assert view._usage_rect() is None and tip(view, area.center()) == ""
        view.hide()
    print("ok")
''')


def test_usage_tooltip_and_clicks():
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
