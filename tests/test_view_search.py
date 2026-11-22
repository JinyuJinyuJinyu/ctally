"""Searching the list: a field beside the rows narrows them to the sessions with every word
typed in their name, folder or tmux pane. The indicator never takes the keyboard, so a box of
its own over the field does while it's typed in. Qt runs in a separate process, so it never
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
    import os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtCore import QEvent, QPointF, QRect, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication([])
    import snapshot
    from ctally.gui.view import Row, TallyView, matches
    from ctally.sessions import Session, State

    home = os.path.expanduser("~")
    sessions = snapshot.demo_sessions()     # api:0.0, webapp:0.0, webapp:0.1, docs:0.0, api:0.1, playground

    # What a search looks at: the name, the folder as shown or in full, the project, the tmux
    # pane. Every word, in any case, anywhere among them.
    row = Row(sessions[0], "Migrate to Postgres 16", "~/code/api", "api:0.0", "")
    for query in ("postgres", "MIGRATE", "~/code/api", home + "/code", "code api", "api:0", "", "  "):
        assert matches(row, query), query
    for query in ("webapp", "postgres webapp", "demo-1"):
        assert not matches(row, query), query

    def mouse(view, kind, point, buttons=Qt.LeftButton):
        point = QPointF(point)
        view.event(QMouseEvent(kind, point, QPointF(view.mapToGlobal(point.toPoint())),
                               Qt.LeftButton, buttons, Qt.NoModifier))

    def click(view, point):
        chosen = []
        view.selected.connect(lambda session, when: chosen.append(session.id))
        mouse(view, QEvent.MouseButtonPress, point)
        mouse(view, QEvent.MouseButtonRelease, point, Qt.NoButton)
        view.selected.disconnect()
        return chosen

    def ids(view):
        return [r.session.id for r in view._rows]

    def make(down=False):
        view = TallyView()
        view.apply(sessions)
        view.apply_usage(snapshot.demo_usage(time.time()))
        view.grows_down = down
        view.settle()
        view.layout_changed.connect(lambda: view.resize(view.preferred_size()))
        view.resize(view.preferred_size())
        view.show()
        return view

    for down in (False, True):
        view = make(down)
        everyone, counts, width = ids(view), list(view._counts), view.width()
        field = view._search_rect
        # The field sits between the usage limits and the rows, clear of both.
        assert view._has_search and not view.searching and view.query == ""
        assert not field.intersects(view._usage_rect()) and not field.intersects(view._row_rect(0)), down
        assert field.intersects(view._rows_view) is False

        # Typed into, it narrows the rows; the count bar still counts them all, and the list
        # holds its width, growing back to the bar.
        tall = view.height()
        view.set_query("webapp")
        assert ids(view) == ["demo-2", "demo-3"] and view._counts == counts and view.width() == width
        assert view.height() < tall and view._found_text == "2 of 6"
        assert click(view, view._row_rect(1).center()) == ["demo-3"]
        assert view.query == "" and ids(view) == everyone             # found it: the search is done with
        view.set_query("nothing like it")
        assert ids(view) == [] and view._rows_height == TallyView.ROW_HEIGHT and not view.is_animating
        view.set_query("")
        assert ids(view) == everyone and view.height() == tall

        # A click on the field brings up the box, over the field's text, taking the keyboard.
        started = []
        view.search_started.connect(lambda box: started.append(box))
        assert click(view, field.center()) == [] and view.searching and len(started) == 1
        box = view._box
        assert QRect(view.mapToGlobal(field.topLeft().toPoint()), field.size().toSize()).contains(box.geometry())
        QTest.keyClicks(box, "API")
        assert view.query == "API" and ids(view) == ["demo-1", "demo-5"]
        # The box follows the window as it shrinks to the matches.
        assert abs(box.geometry().center().y() - view.mapToGlobal(view._search_rect.center().toPoint()).y()) <= 1

        # Return opens the nearest match; Up and Down move along the rows, the way they point.
        assert view._picked == 0
        further, nearer = (Qt.Key_Down, Qt.Key_Up) if down else (Qt.Key_Up, Qt.Key_Down)
        QTest.keyClick(box, further)
        assert view._picked == 1
        QTest.keyClick(box, further)
        assert view._picked == 1                                        # the last stays the last
        QTest.keyClick(box, nearer)
        QTest.keyClick(box, further)
        chosen = []
        view.selected.connect(lambda session, when: chosen.append(session.id))
        QTest.keyClick(box, Qt.Key_Return)
        view.selected.disconnect()
        assert chosen == ["demo-5"] and not view.searching and view.query == ""

        # Esc clears it.
        click(view, view._search_rect.center())
        QTest.keyClicks(box, "docs")
        assert ids(view) == ["demo-4"]
        QTest.keyClick(box, Qt.Key_Escape)
        assert not view.searching and view.query == "" and ids(view) == everyone

        # Going elsewhere leaves it as typed, for the ✕ to clear.
        click(view, view._search_rect.center())
        QTest.keyClicks(box, "play")
        other = QWidget()
        other.show()
        other.activateWindow()
        deadline = time.monotonic() + 5
        while view.searching and time.monotonic() < deadline:
            app.processEvents()
        assert not view.searching and view.query == "play" and ids(view) == ["demo-6"]
        other.close()
        assert click(view, view._clear_rect.center()) == [] and view.query == "" and ids(view) == everyone

        # Folding takes the field away, and the search with it, the window going straight to the
        # bar rather than first out to every row; so does going down to badges.
        view.set_query("api")
        sizes = []
        record = lambda: sizes.append(view.preferred_size())
        view.layout_changed.connect(record)
        view.folded = True
        view.layout_changed.disconnect(record)
        assert view.query == "" and not view._has_search
        assert sizes and all(size == view.preferred_size() for size in sizes), (sizes, view.preferred_size())
        view.folded = False
        view.set_query("api")
        click(view, view._search_rect.center())
        view.apply(sessions[:3])
        assert not view.is_list and view.query == "" and not view.searching
        view.apply(sessions)

        # Polls keep the search: a new session that matches turns up, one that doesn't stays out.
        view.set_query("api")
        view.apply(sessions + [Session("new", State.IDLE, "api", 200, "New thing", home + "/code/api")])
        assert ids(view) == ["demo-1", "demo-5", "new"]
        view.apply(sessions + [Session("new", State.IDLE, "web", 200, "Other", home + "/code/web")])
        assert ids(view) == ["demo-1", "demo-5"]
        view.hide()

    # On macOS the box isn't raised: Qt's raise there brings the whole app forward, and it stays
    # forward once the box is gone, with the keyboard.
    from ctally import system
    from ctally.gui.view import SearchBox
    raised, was = [], system.MAC
    SearchBox.raise_ = lambda self: raised.append(self)
    for mac in (True, False):
        system.MAC, raised[:] = mac, []
        view = make()
        view.start_search()
        assert view.searching and bool(raised) is not mac, mac
        view.end_search(clear=True)
        view.hide()
    system.MAC = was

    print("ok")
''')


def test_searching_the_list():
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stdout + done.stderr
