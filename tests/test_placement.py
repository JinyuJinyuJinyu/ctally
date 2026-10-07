"""Where the indicator sits, and which way the list opens: away from the screen edge it's
nearer. Runs the real controller on Qt's offscreen screen, in a separate process, so Qt never
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
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.app import INSET, Controller

    area = app.primaryScreen().availableGeometry()
    sessions = snapshot.sessions_for("list")

    def controller():
        c = Controller(app, QLocalServer())
        c.view.apply(sessions)
        c.view.settle()
        c.fit()
        return c

    def drop(c, y):
        """Drags the indicator so its top is at y, and lets go."""
        c.view.move(c.view.x(), y)
        c.settle_after_drag()
        return c.view.geometry()

    def bottom(g):
        return g.y() + g.height()

    # Out of the box: the bottom-right corner, opening upward.
    c = controller()
    g = c.view.geometry()
    assert not c.view.grows_down
    assert (g.x() + g.width(), bottom(g)) == (area.right() + 1 - INSET, area.bottom() + 1 - INSET), g
    c.set_folded(True)
    folded = c.view.geometry()
    assert bottom(folded) == bottom(g) and folded.height() < g.height()

    # Dropped in the top half: the top edge is pinned and the list opens downward.
    g = drop(c, area.y() + 30)
    assert c.prefs["anchor"] == [g.x() + g.width(), area.y() + 30, "top"], c.prefs["anchor"]
    assert c.view.grows_down
    c.set_folded(False)
    opened = c.view.geometry()
    assert opened.y() == area.y() + 30 and opened.height() > g.height(), opened
    assert c.view._bar_rect.y() < c.view._row_rect(0).y()          # the bar on top, the rows below

    # It stays that way after a restart.
    c2 = controller()
    assert c2.view.grows_down and c2.view.geometry().y() == area.y() + 30

    # Dropped in the bottom half again: the bottom edge, opening upward.
    g = drop(c, area.bottom() + 1 - 40 - c.view.height())
    assert c.prefs["anchor"][2] == "bottom" and not c.view.grows_down
    c.set_folded(True)
    assert bottom(c.view.geometry()) == bottom(g)
    assert c.view._bar_rect.y() > 0 and c.view.folded

    # A corner saved before it could grow downward, [right, bottom], still works: low on the
    # screen as it was, and high on it turned into a pinned top edge, without moving it.
    c.prefs["anchor"] = [area.x() + 500, area.bottom() + 1 - 20]
    c.fit()
    g = c.view.geometry()
    assert (g.x() + g.width(), bottom(g), c.view.grows_down) == (area.x() + 500, area.bottom() + 1 - 20, False)
    assert c.prefs["anchor"] == [area.x() + 500, area.bottom() + 1 - 20]
    c.prefs["anchor"] = [area.x() + 500, area.y() + 200]
    c.fit()
    g = c.view.geometry()
    assert (g.x() + g.width(), bottom(g), c.view.grows_down) == (area.x() + 500, area.y() + 200, True)
    assert c.prefs["anchor"] == [area.x() + 500, area.y() + 200 - g.height(), "top"]
    c.set_folded(False)
    assert c.view.geometry().y() == g.y() and c.view.geometry().height() > g.height()
    c.set_folded(True)

    # Pinned low enough that the open list won't fit below: it slides up to stay on screen,
    # and the saved corner stays put for when it folds again.
    c.prefs["anchor"] = [area.x() + 500, area.y() + area.height() // 2 + 40, "top"]
    c.set_folded(False)
    g = c.view.geometry()
    assert c.view.grows_down and bottom(g) <= area.bottom() + 1, g
    assert c.prefs["anchor"] == [area.x() + 500, area.y() + area.height() // 2 + 40, "top"]
    print("ok")
''')


def test_the_list_opens_away_from_the_nearer_edge(tmp_path):
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
