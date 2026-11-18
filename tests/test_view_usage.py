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
        # Elsewhere, no usage: nothing, or over a session (the badge's corner), the session.
        assert not tip(view, QPoint(2, 2)).startswith("Current session"), (mode, folded)
        assert click(view, area.center()) == [], (mode, folded)
        view.apply_usage(None)
        # Gone, and so is its tooltip (rows can slide into where it was).
        assert view._usage_rect() is None and not tip(view, area.center()).startswith("Current session")
        view.hide()
    print("ok")
''')


def test_usage_tooltip_and_clicks():
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr


BEHIND = textwrap.dedent('''
    import os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.view import TallyView

    now = time.time()
    fresh = snapshot.demo_usage(now)
    # The model's week from /usage five hours ago, the rest from a reply just now.
    usage = fresh._replace(limits=tuple(
        limit._replace(seen=now - 5 * 3600 if "Fable" in limit.title else now) for limit in fresh.limits))
    view = TallyView()
    view.apply(snapshot.sessions_for("list"))
    view.apply_usage(usage, now)
    shown = {meter.label: (meter.when, meter.old) for meter in view._meters}
    assert shown["Week · Fable"] == ("5 h ago", True), shown
    assert not shown["Week"][1] and not shown["Week"][0].endswith("ago"), shown
    view.resize(view.preferred_size())
    view.grab()                         # draws, faded row and all
    print("ok")
''')


def test_a_number_well_behind_the_rest_shows_its_age():
    done = subprocess.run([sys.executable, "-c", BEHIND, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
