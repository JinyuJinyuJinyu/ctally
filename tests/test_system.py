"""Looking at other processes."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import dead_pid, sleeper, wait_for

from ctally import system


def test_is_alive():
    assert system.is_alive(os.getpid())
    assert not system.is_alive(dead_pid())
    assert system.is_alive(0)                    # no pid recorded: can't disprove it
    assert system.is_alive(1)                    # not ours to signal, but alive


def test_own_environment():
    # What this process started with: HOME, as conftest changes PATH (and others) only later on.
    env = system.environment(os.getpid())
    assert env is not None and env.get("HOME") == os.environ.get("HOME")


def test_a_childs_environment():
    child = sleeper({**os.environ, "CTALLY_TEST_VAR": "a=b c"})
    try:
        # Right after the fork the child still shows its parent's; wait for the exec.
        env = wait_for(lambda: (system.environment(child.pid) or {}).get("CTALLY_TEST_VAR"))
        assert env == "a=b c"
    finally:
        child.kill()


def test_environment_of_a_dead_process():
    assert system.environment(dead_pid()) is None


def test_parent():
    assert system.parent(os.getpid()) == os.getppid()


@pytest.mark.skipif(not system.LINUX, reason="reads /proc")
def test_started():
    child = sleeper()
    try:
        began = system.started(child.pid)
        assert began and began.isdigit() and system.started(child.pid) == began
        assert int(began) >= int(system.started(os.getpid()))     # after this test's own process
    finally:
        child.kill()
    assert system.started(dead_pid()) is None


@pytest.mark.skipif(not system.LINUX, reason="reads /proc")
def test_program():
    assert system.program(os.getpid()) == os.path.basename(os.path.realpath(sys.executable))
    assert system.program(dead_pid()) is None


def test_tmux_pane_outside_tmux():
    child = sleeper({k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")})
    try:
        wait_for(lambda: system.environment(child.pid) is not None
                 and "CTALLY_PARENT_ONLY" not in (system.environment(child.pid) or {}))
        assert system.tmux_pane(child.pid) is None
    finally:
        child.kill()


def test_tmux_pane_inside_tmux(private_tmux):
    private_tmux.new_session("demo")
    pid = private_tmux.pid_in("demo:0.0")
    target = system.tmux_pane(pid)
    assert target is not None
    assert target.socket == private_tmux.socket
    assert target.pane == private_tmux.pane("demo:0.0", "#{pane_id}")
    assert system.tmux_location(target) == "demo:0.0"


def test_run():
    assert system.run(["sh", "-c", "echo hi"]) == "hi\n"
    assert system.run(["sh", "-c", "exit 3"]) is None
    assert system.run(["/no/such/tool"]) is None


def test_tty_of_a_process_without_one():
    child = sleeper()
    try:
        assert system.tty(child.pid) in (None, system.tty(os.getpid()))
    finally:
        child.kill()


def test_what_a_command_line_hands_claude_code():
    assert system.claude_arguments(["claude", "attach", "f2b0a87f"]) == ["attach", "f2b0a87f"]
    assert system.claude_arguments(["/home/me/.local/share/claude/versions/2.1.326", "--resume", "x"]) == ["--resume", "x"]
    assert system.claude_arguments(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "attach", "j"]) == [
        "attach", "j"]
    assert system.claude_arguments(["vim", "claude-notes.txt"]) is None
    assert system.claude_arguments(["python", "-c", "x", "claude"]) is None   # past where a program could be


def test_a_terminal_running_claude_attach_is_found(tmp_path):
    """`claude attach <job>`, as ccsnap types it into a pane it brings back."""
    # A Python script named claude: its command line is "python .../claude attach f2b0a87f".
    stand_in = tmp_path / "claude"
    stand_in.write_text("import time; time.sleep(600)\n")
    child = subprocess.Popen([sys.executable, str(stand_in), "attach", "f2b0a87f"])
    other = subprocess.Popen([sys.executable, str(stand_in), "attach", "--help"])
    try:
        found = wait_for(lambda: system.attached().get(child.pid))
        assert found == "f2b0a87f"
        assert other.pid not in system.attached()       # an option, not a job
    finally:
        child.kill()
        other.kill()
