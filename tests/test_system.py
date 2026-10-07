"""Looking at other processes."""
from __future__ import annotations

import os
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
    env = system.environment(os.getpid())
    assert env is not None and env.get("PATH") == os.environ.get("PATH")


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
