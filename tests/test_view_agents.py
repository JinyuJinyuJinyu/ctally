"""A closer look at a session's agents: clicking the line about them opens it up, a line per
phase of a workflow or per agent, and clicking again folds it away. Qt runs in a separate
process, so it never starts inside the test run."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent('''
    import dataclasses, os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.app import Controller
    from ctally.gui.view import Detail, TallyView, details
    from ctally.sessions import Agents, Member

    def click(view, point):
        got = []
        view.selected.connect(lambda session, when: got.append(("terminal", session.id)))
        for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
            point = QPointF(point)
            view.event(QMouseEvent(kind, point, QPointF(view.mapToGlobal(point.toPoint())),
                                   Qt.LeftButton, buttons, Qt.NoModifier))
        view.selected.disconnect()
        return got

    sessions = snapshot.demo_sessions()
    workflow = next(i for i, s in enumerate(sessions) if s.agents and s.agents.phases)
    c = Controller(app, QLocalServer())
    view = c.view
    view.apply(sessions)
    view.settle()
    c.fit()
    view.show()
    before = view.height()
    resized = []
    view.layout_changed.connect(lambda: resized.append(view.height()))

    # Opening up: a line per phase, the row and the window that much taller, at once.
    line = view._lines(view._row_rect(workflow), view._rows[workflow])["agents"]
    assert click(view, line.center()) == []
    row = view._rows[workflow]
    assert [d.text for d in row.details] == ["Review", "Fix", "Verify"]
    assert row.details[2] == Detail("Verify", "verify:schema, verify:queries, verify:pool, verify:backups", 0, 4)
    grown = 3 * TallyView.DETAIL_LINE
    assert view._row_rect(workflow).height() == TallyView.ROW_HEIGHT + TallyView.AGENT_LINE + grown
    assert resized == [before + grown], (resized, before)

    # A click on what opened folds it away again; the session's name still goes to its terminal.
    opened = view._lines(view._row_rect(workflow), view._rows[workflow])["details"]
    assert click(view, opened.center()) == [] and view._rows[workflow].details == ()
    assert view.height() == before
    assert click(view, view._row_rect(workflow).topLeft() + QPointF(60, 10)) == [("terminal", sessions[workflow].id)]

    # Agents known only by count have nothing more to show: their line goes to the terminal.
    bare = [dataclasses.replace(s, agents=Agents(2)) if s.agents else s for s in sessions]
    view.apply(bare)
    line = view._lines(view._row_rect(workflow), view._rows[workflow])["agents"]
    assert click(view, line.center()) == [("terminal", sessions[workflow].id)]

    # Hovering lists them too, which is all the badges have room for.
    view.apply(sessions)
    words = TallyView.describe_row(view._rows[workflow])
    assert "    Verify  0/4  (verify:schema, verify:queries, verify:pool, verify:backups)" in words, words

    # Many agents: the closer look stops at eight lines.
    many = Agents(12, members=tuple(Member(f"task {i}", "Explore") for i in range(12)))
    assert [d.text for d in details(many)][-2:] == ["Explore · task 6", "+5 more"] and len(details(many)) == 8
    print("ok")
''')


def test_a_closer_look_at_the_agents(tmp_path):
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
