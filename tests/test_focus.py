"""Clicking a session brings its terminal forward. On Linux that's GNOME Terminal's own tab,
found from the shell it started there, or else the window of the nearest process that has one,
through xdotool. The processes and windows here are made up; the real desktop is never touched."""
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

from ctally import focus, system

TAB = "41e2cd26-d5ed-4a2e-96f9-ee2c45475262"
SCREEN = "/org/gnome/Terminal/screen/" + TAB.replace("-", "_")


class Desktop:
    """Processes (each one's parent, program and environment) and the windows they own, as
    xdotool lists them; and what CTally runs against them."""

    def __init__(self, monkeypatch):
        self.processes: dict[int, tuple[int, str, dict]] = {}
        self.windows: dict[int, list[str]] = {}
        self.ran: list[list[str]] = []
        self.gdbus = True
        self.xdotool = True
        monkeypatch.setattr(system, "parent", lambda pid: self.processes[pid][0] if pid in self.processes else None)
        monkeypatch.setattr(system, "program", lambda pid: self.processes[pid][1] if pid in self.processes else None)
        monkeypatch.setattr(system, "environment", lambda pid: self.processes[pid][2] if pid in self.processes else None)
        monkeypatch.setattr(system, "run", self.run)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" and self.xdotool else None)

    def process(self, pid: int, parent: int, program: str, env: dict | None = None, windows=()) -> None:
        self.processes[pid] = (parent, program, env or {})
        self.windows[pid] = list(windows)

    def run(self, args: list[str], timeout: float = 5) -> str | None:
        self.ran.append(args)
        if args[0] == "gdbus":
            return "()\n" if self.gdbus else None
        if args[1] == "search":
            return "".join(window + "\n" for window in self.windows.get(int(args[-1]), []))
        return ""

    def raised(self) -> list[tuple]:
        """What came forward: ("tab", its id, the click's time), or ("window", its id)."""
        found = []
        for args in self.ran:
            if args[0] == "gdbus" and self.gdbus:
                found.append(("tab", args[-3].strip("'"), int(args[-1])))
            elif args[1] == "windowactivate":
                found.append(("window", args[-1]))
        return found


@pytest.fixture
def desktop(monkeypatch):
    """GNOME Terminal with three windows; in one tab, a shell running Claude Code."""
    d = Desktop(monkeypatch)
    d.process(1, 0, "systemd")
    d.process(100, 1, "gnome-terminal-server", windows=["11", "12", "13"])
    d.process(200, 100, "bash", {"GNOME_TERMINAL_SCREEN": SCREEN, "GNOME_TERMINAL_SERVICE": ":1.197"})
    d.process(300, 200, "claude", {"GNOME_TERMINAL_SCREEN": SCREEN})
    return d


def test_gnome_terminal_brings_up_the_sessions_own_tab(desktop):
    focus._raise_linux(300, when=876567876)
    assert desktop.raised() == [("tab", TAB, 876567876)]
    asked = desktop.ran[-1]
    assert asked[asked.index("--dest") + 1] == ":1.197"         # that very terminal, by its bus name


def test_a_tmux_client_brings_up_its_own_tab(desktop):
    # What raising does for a session in tmux: the client attached to it is in the tab.
    desktop.process(400, 200, "tmux", {"GNOME_TERMINAL_SCREEN": SCREEN})
    focus._raise_linux(400, when=5)
    assert desktop.raised() == [("tab", TAB, 5)]


def test_without_gdbus_the_terminal_window_comes_up(desktop):
    desktop.gdbus = False
    focus._raise_linux(300, when=5)
    assert desktop.raised() == [("window", "13")]


def test_without_xdotool_gnome_terminal_still_comes_up(desktop):
    desktop.xdotool = False
    focus._raise_linux(300, when=5)
    assert desktop.raised() == [("tab", TAB, 5)]

    desktop.ran.clear()
    desktop.process(600, 1, "xterm", windows=["31"])
    desktop.process(610, 600, "bash")
    focus._raise_linux(610, when=5)
    assert desktop.raised() == []


def test_a_tab_id_passed_on_outside_gnome_terminal_is_ignored(desktop):
    # VS Code started from a GNOME Terminal tab hands that tab's variables to its own terminals.
    desktop.process(500, 1, "code", {"GNOME_TERMINAL_SCREEN": SCREEN}, windows=["21"])
    desktop.process(510, 500, "bash", {"GNOME_TERMINAL_SCREEN": SCREEN})
    desktop.process(520, 510, "claude", {"GNOME_TERMINAL_SCREEN": SCREEN})
    focus._raise_linux(520, when=5)
    assert desktop.raised() == [("window", "21")]


def test_an_odd_tab_id_is_never_passed_on(desktop):
    desktop.process(200, 100, "bash", {"GNOME_TERMINAL_SCREEN": "/org/gnome/Terminal/screen/x' 'y"})
    focus._raise_linux(300, when=5)
    assert desktop.raised() == [("window", "13")]


def test_other_terminals_raise_the_nearest_window(desktop):
    desktop.process(600, 1, "xterm", windows=["31", "32"])
    desktop.process(610, 600, "bash")
    desktop.process(620, 610, "claude")
    focus._raise_linux(620, when=5)
    assert desktop.raised() == [("window", "32")]


CLICK = textwrap.dedent('''
    import os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/tools"]
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import snapshot
    from ctally.gui.view import TallyView

    # Past what a 32-bit signed int holds: the X server's clock gets there after 25 days.
    PRESS, RELEASE = 4294967000, 4294967290
    for mode in ("one", "three", "list"):
        view = TallyView()
        view.apply(snapshot.sessions_for(mode))
        view.resize(view.preferred_size())
        view.show()
        chosen = []
        view.selected.connect(lambda session, when: chosen.append((session.id, when)))
        point = view._item_rect(1 if mode != "one" else 0).center()
        for kind, time in ((QEvent.MouseButtonPress, PRESS), (QEvent.MouseButtonRelease, RELEASE)):
            event = QMouseEvent(kind, point, view.mapToGlobal(point), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            event.setTimestamp(time)
            view.event(event)
        expected = view._rows[1 if mode != "one" else 0].session.id
        assert chosen == [(expected, RELEASE)], (mode, chosen)
        view.hide()
    print("ok")
''')


def test_a_click_passes_on_its_time():
    """The window system lets a terminal come forward only for the time of the click that asked."""
    pytest.importorskip("PySide6")
    done = subprocess.run([sys.executable, "-c", CLICK, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
