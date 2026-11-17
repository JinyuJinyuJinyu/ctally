"""Codex's source and approval UI in an offscreen Qt process."""
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent('''
    from dataclasses import replace
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from ctally import answer, focus, system
    from ctally.gui.app import Controller, _Menu
    from ctally.gui.view import TallyView
    from ctally.sessions import Ask, Context, Session, State

    session = Session("codex:one", State.WAITING, "api", pid=42, tmux="work:0.1", source="codex",
                      ask=Ask("question", "Allow Bash(npm publish)?"), context=Context(50, 200000, 100000))
    view = TallyView()
    view.apply([Session("one", State.WORKING, "api"), session])
    assert [row.title for row in view._rows] == ["api", "Codex · api"]
    session = replace(session, title="修复审批状态")
    view.apply([Session("one", State.WORKING, "api"), session])
    assert [row.title for row in view._rows] == ["api", "Codex · 修复审批状态"]
    description = view.describe_row(view._rows[1])
    assert "修复审批状态" in description
    assert "Context 50% full" in description
    assert "answer from here" not in description

    def no_claude_choices(*_):
        raise AssertionError("Codex approval parsed as a Claude screen")
    answer.read = no_claude_choices
    system.tmux_pane = lambda _: system.TmuxPane("tmux", "/tmp/fake", "%1")
    controller = object.__new__(Controller)
    menu = _Menu()
    assert not controller._add_answers(menu, session)
    assert [action.text() for action in menu.actions()] == ["Allow Bash(npm publish)?", "Open Codex Task"]
    assert not menu.actions()[0].isEnabled()
    menu = _Menu()
    assert not controller._add_answers(menu, replace(session, pid=0, tmux=None))
    assert menu.actions()[-1].text() == "Open Codex Task"
    assert menu.actions()[-1].isEnabled()
    opened = []
    controller.view = view
    focus.open_session = lambda task, when, failed: opened.append((task.id, when))
    menu.actions()[-1].trigger()
    assert opened == [(session.id, menu.when)]
    view.selected.connect(controller.open_session)
    view.selected.emit(session, 123)
    assert opened[-1] == (session.id, 123)
    assert "Click to open this task in Codex" in description
    print("ok")
''')


def test_codex_label_context_and_terminal_only_answers():
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert result.returncode == 0 and result.stdout.strip().endswith("ok"), result.stderr
