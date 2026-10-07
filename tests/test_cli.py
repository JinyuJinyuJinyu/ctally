"""`ctally status`, `list` and `jump`."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from conftest import TMUX, dead_pid, sleeper, wait_for

import ctally.cli
from ctally import cli, system


@pytest.fixture
def state(tmp_path, monkeypatch):
    folder = tmp_path / "ctally.d"
    folder.mkdir()
    monkeypatch.setattr(ctally.cli, "STATE_DIR", folder)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return folder


@pytest.fixture
def outside_tmux():
    """Live processes that aren't in tmux, to stand in for claude."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    children = []

    def make() -> int:
        child = sleeper(env)
        children.append(child)
        return child.pid

    yield make
    for child in children:
        child.kill()


def write(folder, name: str, text: str, mtime: float | None = None) -> None:
    (folder / name).write_text(text)
    if mtime is not None:
        os.utime(folder / name, (mtime, mtime))


def test_status_counts_most_urgent_first(state, capsys):
    pid = os.getpid()
    for name, word in [("a", "done"), ("b", "working"), ("c", "waiting"), ("d", "done"), ("e", "odd")]:
        write(state, name, f"{word} {pid} p\n")
    write(state, "f", f"working {dead_pid()} p\n")                    # its claude is gone: skipped
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == "!1 ▶1 ✓2 ·1\n"
    cli.main(["status", "--tmux"])
    assert capsys.readouterr().out == "#[fg=#ffb329]!1 #[fg=#4accf2]▶1 #[fg=#45e087]✓2 #[fg=#99a6bd]·1#[default]\n"


def test_limited_sessions_count_as_waiting_but_jump_skips_them(state, capsys, monkeypatch):
    pid = os.getpid()
    write(state, "a", f"limited {pid} a\n")
    write(state, "b", f"waiting {pid} b\n")
    write(state, ".limit", f"{int(time.time()) + 3600} You've hit your session limit\n")
    cli.main(["status"])
    assert capsys.readouterr().out == "!2\n"
    assert [(s.id, s.state) for s in cli.live_sessions()] == [("b", "waiting"), ("a", "limited")]
    monkeypatch.setattr(cli.system, "tmux_pane", lambda pid: system.TmuxPane("tmux", "/sock", "%1"))
    assert cli.jump("%9", None, None) == 1
    assert "next up is b (waiting)" in capsys.readouterr().err
    write(state, ".limit", f"{int(time.time()) - 1} You've hit your session limit\n")    # it has reset
    cli.main(["status"])
    assert capsys.readouterr().out == "!1 ✓1\n"


def test_status_with_nothing(state, capsys):
    cli.main(["status"])
    cli.main(["status", "--tmux"])
    assert capsys.readouterr().out == "\n\n"


def test_list_columns(state, capsys, outside_tmux):
    one, three, none = outside_tmux(), outside_tmux(), outside_tmux()
    write(state, "s-one", f"working {one} api\n", mtime=1000)
    write(state, "s-three", f"working {three} my project\n", mtime=2000)
    write(state, "s-none", f"waiting {none} docs\n")
    for sid, count in (("s-one", 1), ("s-three", 3)):
        folder = state / ".agents" / sid
        folder.mkdir(parents=True)
        for i in range(count):
            (folder / f"a{i}").write_text("Explore")
        (folder / ".background").write_text("")
    cli.main(["list"])
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"{'waiting':<8} {'-':<16} {'-':<9} {'docs':<24} s-none",
        f"{'working':<8} {'-':<16} {'1 agent':<9} {'api':<24} s-one",
        f"{'working':<8} {'-':<16} {'3 agents':<9} {'my project':<24} s-three",
    ]


def test_jump_with_nothing_to_do(state, capsys):
    assert cli.main(["jump"]) == 0
    assert capsys.readouterr().out == "CTally: nothing needs you\n"


def test_jump_outside_tmux_names_the_next_one(state, capsys, monkeypatch):
    write(state, "a", f"waiting {os.getpid()} api\n")
    monkeypatch.setattr(cli.system, "tmux_pane", lambda pid: system.TmuxPane("tmux", "/tmp/sock", "%1"))
    assert cli.main(["jump"]) == 1
    assert "next up is api (waiting)" in capsys.readouterr().err


def test_jump_order_and_cycle(state, monkeypatch, capsys, outside_tmux):
    """Waiting first, then done, longest-waiting first; each press moves to the next one
    after the pane I'm on, and wraps."""
    panes = {}
    now = time.time()
    for name, word, age in [("misc", "working", 0), ("done-new", "done", 10), ("wait", "waiting", 5),
                            ("done-old", "done", 50)]:
        pid = outside_tmux()
        panes[pid] = f"%{name}"
        write(state, name, f"{word} {pid} {name}\n", mtime=now - age)
    monkeypatch.setattr(cli.system, "tmux_pane", lambda pid: system.TmuxPane("tmux", "/sock", panes[pid]))
    switched = []

    def run(args, timeout=5):
        if "switch-client" in args:
            switched.append(args[args.index("-t") + 1])
        return ""

    monkeypatch.setattr(cli.system, "run", run)
    current = "%misc"
    for _ in range(4):
        assert cli.jump(current, None, "/sock") == 0
        current = switched[-1]
    assert switched == ["%wait", "%done-old", "%done-new", "%wait"]
    assert "CTally: wait · waiting" in capsys.readouterr().out


def test_jump_refuses_another_server(state, monkeypatch, capsys):
    write(state, "a", f"waiting {os.getpid()} api\n")
    monkeypatch.setattr(cli.system, "tmux_pane", lambda pid: system.TmuxPane("tmux", "/other", "%1"))
    assert cli.jump("%9", None, "/mine") == 1
    assert "on another tmux server" in capsys.readouterr().out


def attach_client(server, session: str):
    """A real client attached to the private server, in a pseudo-terminal of its own."""
    command = [TMUX, "-L", server.name, "attach-session", "-t", session]
    if sys.platform == "darwin":
        script = ["script", "-q", "/dev/null", *command]
    else:
        script = ["script", "-q", "-c", " ".join(command), "/dev/null"]
    # Its stdin is a pipe we keep open: at end of input `script` would end the client.
    client = subprocess.Popen(script, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=server.env)
    server.processes.append(client)
    name = wait_for(lambda: server("list-clients", "-F", "#{client_name}", check=False), timeout=5)
    if not name:
        pytest.skip("couldn't attach a tmux client here")
    return name.splitlines()[0]


@pytest.mark.skipif(not shutil.which("script"), reason="needs script(1) for a client terminal")
def test_jump_switches_a_real_client(state, private_tmux, capsys):
    now = time.time()
    private_tmux.new_session("misc")
    for name in ("api", "web"):
        private_tmux.new_session(name)
    private_tmux.split("api:0")
    targets = {"misc": "misc:0.0", "wait": "api:0.0", "done-old": "api:0.1", "done-new": "web:0.0"}
    words = {"misc": "working", "wait": "waiting", "done-old": "done", "done-new": "done"}
    ages = {"misc": 0, "wait": 1, "done-old": 30, "done-new": 10}
    pane_ids = {}
    for name, target in targets.items():
        write(state, name, f"{words[name]} {private_tmux.pid_in(target)} {name}\n", mtime=now - ages[name])
        pane_ids[private_tmux.pane(target, "#{pane_id}")] = name

    client = attach_client(private_tmux, "misc")
    socket = private_tmux.socket
    visited = []
    for _ in range(4):
        current = private_tmux("display-message", "-p", "-c", client, "#{pane_id}")
        assert cli.jump(current, client, socket) == 0
        landed = wait_for(lambda: private_tmux("display-message", "-p", "-c", client, "#{pane_id}") != current
                          and private_tmux("display-message", "-p", "-c", client, "#{pane_id}"))
        visited.append(pane_ids.get(landed, landed))
    assert visited == ["wait", "done-old", "done-new", "wait"]
    messages = private_tmux("show-messages")
    assert "CTally: done-old · done" in messages


# MARK: Usage limits

def usage_files(home, five=71, week=49, fable=None):
    """~/.claude.json with the session and the week, and a Fable week if given."""
    import json
    limits = [{"kind": "session", "group": "session", "percent": five, "resets_at": "2099-01-01T08:30:00Z"},
              {"kind": "weekly_all", "group": "weekly", "percent": week, "resets_at": "2099-01-03T22:00:00Z"}]
    if fable is not None:
        limits.append({"kind": "weekly_scoped", "group": "weekly", "percent": fable,
                       "resets_at": "2099-01-03T22:00:00Z", "scope": {"model": {"display_name": "Fable"}}})
    (home / ".claude.json").write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": time.time() * 1000 - 120_000, "utilization": {"limits": limits}}}))


def test_status_for_tmux_adds_the_session_and_week(state, capsys, own_usage_and_prefs):
    write(state, "a", f"working {os.getpid()} p\n")
    usage_files(own_usage_and_prefs, five=80, week=49, fable=3)
    cli.main(["status", "--tmux"])
    assert capsys.readouterr().out == ("#[fg=#4accf2]▶1 #[fg=#ffb329]5h 80% #[fg=#99a6bd]7d 49%#[default]\n")
    cli.main(["status"])
    assert capsys.readouterr().out == "▶1\n"                   # the plain counts stay just counts


def test_status_for_tmux_with_usage_switched_off(state, capsys, own_usage_and_prefs):
    from ctally.prefs import Prefs
    usage_files(own_usage_and_prefs, five=95)
    cli.main(["status", "--tmux"])
    assert capsys.readouterr().out == "#[fg=#ff6661]5h 95% #[fg=#99a6bd]7d 49%#[default]\n"
    Prefs()["usage"] = False
    cli.main(["status", "--tmux"])
    assert capsys.readouterr().out == "\n"


def test_usage_lists_every_limit(state, capsys, own_usage_and_prefs):
    usage_files(own_usage_and_prefs, five=71, week=49, fable=0)
    assert cli.main(["usage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[0] for line in lines[:3]] == [
        "Current session", "Current week (all models)", "Current week (Fable)"]
    assert "███████░░░  71%  resets " in lines[0] and "░░░░░░░░░░   0%" in lines[2]
    assert lines[3] == "Updated 2 min ago"


def test_usage_as_json(state, capsys, own_usage_and_prefs):
    import json
    usage_files(own_usage_and_prefs, five=92)
    assert cli.main(["usage", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["stale"] is False
    assert [(l["title"], l["percent"], l["level"]) for l in found["limits"]] == [
        ("Current session", 92, "critical"), ("Current week (all models)", 49, "ok")]
    assert found["limits"][0]["resets_at"] == 4070939400


def test_usage_with_nothing_known(state, capsys):
    assert cli.main(["usage"]) == 1
    assert "no usage limits known yet" in capsys.readouterr().err
