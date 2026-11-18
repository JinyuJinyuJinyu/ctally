"""Open an exact Codex task, including tasks with no terminal or a shared tmux pane."""
from dataclasses import replace
import json
import os
import shlex
import sys
import threading

import pytest

from conftest import wait_for
from ctally import focus, system
from ctally.sessions import Session, State

SID = "01a1e858-418a-7440-8cc6-4180bd1eeac1"
TASK = Session("codex:" + SID, State.WAITING, "api", source="codex", owner_pid=100)


@pytest.fixture
def launcher(monkeypatch):
    commands = []
    monkeypatch.setattr(system, "LINUX", True)
    monkeypatch.setattr(system, "MAC", False)
    monkeypatch.setattr(system, "parent", lambda pid: 1)
    monkeypatch.setattr(focus.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(focus.subprocess, "Popen", lambda args, **kwargs: commands.append(args))
    return commands


def test_daemon_task_opens_exact_id_without_prompt_or_permission_overrides(launcher):
    focus._open_session_now(TASK, 23)
    [args] = launcher
    assert args[:4] == ["/usr/bin/x-terminal-emulator", "-e", "sh", "-c"]
    assert shlex.split(args[-1])[3:] == ["/usr/bin/codex", "resume", "--", SID]


def test_shared_pane_tasks_are_opened_by_id_not_by_pane(launcher, monkeypatch):
    monkeypatch.setattr(focus, "_focus_now", lambda *_: pytest.fail("wrong task could be shown"))
    for sid in (SID, "01a1e7a2-3f11-71d2-9f90-b5cc0b9f0135"):
        focus._open_session_now(replace(TASK, id="codex:" + sid, pid=50, tmux="work:0.0"), 2)
        assert shlex.split(launcher[-1][-1])[-1] == sid
    assert len(launcher) == 2


def test_private_codex_server_returns_to_its_terminal(launcher, monkeypatch):
    monkeypatch.setattr(system, "parent", lambda pid: {100: 90, 90: 50}.get(pid))
    raised = []
    monkeypatch.setattr(focus, "_focus_now", lambda *args: raised.append(args))
    focus._open_session_now(replace(TASK, pid=50), 123)
    assert raised == [(50, 123)] and not launcher


def test_claude_keeps_existing_terminal_navigation(launcher, monkeypatch):
    raised = []
    monkeypatch.setattr(focus, "_focus_now", lambda *args: raised.append(args))
    focus._open_session_now(Session("claude", State.DONE, "api", pid=42), 12)
    assert raised == [(42, 12)] and not launcher


def test_invalid_task_id_never_launches_a_command(launcher):
    with pytest.raises(ValueError, match="valid session ID"):
        focus._open_session_now(replace(TASK, id="codex:--help; touch unwanted"), 0)
    assert not launcher


def test_missing_cli_or_terminal_reports_actionable_error(launcher, monkeypatch):
    monkeypatch.setattr(focus.shutil, "which", lambda _: None)
    monkeypatch.setattr(focus.os, "access", lambda *_: False)
    with pytest.raises(OSError, match="Codex CLI was not found"):
        focus._open_session_now(TASK, 0)
    monkeypatch.setattr(focus.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)
    with pytest.raises(OSError, match="terminal"):
        focus._open_session_now(TASK, 0)
    assert not launcher


def test_macos_passes_command_as_data_to_applescript(launcher, monkeypatch):
    monkeypatch.setattr(system, "MAC", True)
    monkeypatch.setattr(system, "run", lambda args: launcher.append(args) or "")
    focus._open_session_now(TASK, 0)
    [args] = launcher
    assert args[:2] == ["osascript", "-e"]
    assert "do script (item 1 of argv)" in args[2]
    assert shlex.split(args[3])[-3:] == ["resume", "--", SID]
    monkeypatch.setattr(system, "run", lambda _: None)
    with pytest.raises(OSError, match="Automation"):
        focus._open_session_now(TASK, 0)


def test_real_launch_preserves_quoted_paths_and_codex_home(tmp_path, monkeypatch):
    # The terminal and Codex are harmless executables, exercising the actual shell boundary.
    tools = tmp_path / "bin with spaces"
    tools.mkdir()
    output = tmp_path / "launched.json"
    terminal = tools / "x-terminal-emulator"
    terminal.write_text(f"#!{sys.executable}\nimport os, sys\nos.execvp(sys.argv[2], sys.argv[2:])\n")
    binary = tools / "codex"
    binary.write_text(f"#!{sys.executable}\nimport os, sys, json\nfrom pathlib import Path\n"
                      f"Path({str(output)!r}).write_text(json.dumps([sys.argv[1:], os.getcwd(), os.environ['CODEX_HOME']]))\n")
    terminal.chmod(0o755)
    binary.chmod(0o755)
    project = tmp_path / "project 'quoted' $(touch UNWANTED)"
    project.mkdir()
    codex_home = tmp_path / "codex 'home' $(touch UNWANTED)"
    monkeypatch.setenv("CTALLY_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(system, "MAC", False)
    monkeypatch.setattr(system, "LINUX", True)
    focus._open_session_now(replace(TASK, path=str(project)), 0)
    assert wait_for(output.exists)
    assert json.loads(output.read_text()) == [["resume", "--", SID], str(project), str(codex_home)]
    assert not (project / "UNWANTED").exists()


def test_repeated_clicks_open_once_and_failures_can_be_retried(monkeypatch):
    focus._opened_at.clear()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def launch(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(focus, "_open_session_now", launch)
    focus.open_session(TASK)
    assert entered.wait(5)
    try:
        focus.open_session(TASK)
        assert len(calls) == 1
    finally:
        release.set()
    assert wait_for(lambda: TASK.id not in focus._opening)
    focus.open_session(TASK)
    assert len(calls) == 1
    focus._opened_at.clear()
    errors = []

    def fail(*args):
        raise OSError("No terminal")

    monkeypatch.setattr(focus, "_open_session_now", fail)
    for attempt in (1, 2):
        focus.open_session(TASK, failed=errors.append)
        assert wait_for(lambda: TASK.id not in focus._opening)
        assert errors == ["No terminal"] * attempt
