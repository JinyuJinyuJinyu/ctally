"""Snapshots of tmux and its Claude Code sessions, and bringing them back. tmux runs as a private
server, and Claude Code's folders are temporary: nothing here touches the real ones."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import ROOT, wait_for

from ctally import snapshots, system
from ctally.snapshots import Keeper, Places, Snapshots, Tmux, capture, launch, options, restore, stop_ccsnap

DAY = 86400


@pytest.fixture
def places(tmp_path) -> Places:
    found = Places(tmp_path / "sessions", tmp_path / "jobs", tmp_path / "projects")
    for folder in found:
        folder.mkdir()
    return found


def transcript(places: Places, sid: str) -> None:
    folder = places.projects / "-work"
    folder.mkdir(exist_ok=True)
    (folder / f"{sid}.jsonl").write_text("{}\n")


def job(places: Places, short: str, sid: str, name: str, cwd: str) -> None:
    (places.jobs / short).mkdir()
    (places.jobs / short / "state.json").write_text(json.dumps(
        {"name": name, "sessionId": sid, "resumeSessionId": sid, "cwd": cwd}))


def stand_in(folder: Path) -> Path:
    """A program named claude that just waits: its command line is what a snapshot reads."""
    path = folder / "claude"
    path.write_text("import time; time.sleep(600)\n")
    return path


def listed(places: Places, pid: int, sid: str, cwd: str, **extra) -> None:
    record = {"pid": pid, "sessionId": sid, "cwd": cwd, "procStart": system.started(pid),
              "kind": "interactive", **extra}
    (places.registry / f"{pid}.json").write_text(json.dumps(record))


def pane(cwd: str, claude: dict | None = None, index: int = 0, active: bool = True) -> dict:
    found = {"index": index, "cwd": cwd, "active": active, "command": "bash"}
    if claude:
        found["claude"] = claude
    return found


def window(panes: list[dict], index: int = 0, name: str = "claude", active: bool = True) -> dict:
    return {"index": index, "name": name, "layout": "", "active": active, "width": 120, "height": 40,
            "auto_rename": False, "panes": panes}


# MARK: Which options a session is resumed with

def test_options_keep_the_settings_and_drop_what_picks_a_session():
    assert options(["--resume", "abc", "--dangerously-skip-permissions"]) == ["--dangerously-skip-permissions"]
    assert options(["-c", "--model", "opus", "fix the tests"]) == ["--model", "opus"]
    assert options(["--add-dir", "a", "b", "--print", "q"]) == ["--add-dir", "a", "b"]
    assert options(["--name=cb5", "--effort", "high", "--", "--model", "x"]) == ["--effort", "high"]


def test_what_a_pane_gets_typed_into_it(places, tmp_path):
    transcript(places, "s1")
    job(places, "f2b0a87f", "s2", "cb7", "/w")
    resumed = pane("/w", {"session_id": "s1", "name": "cb1", "name_source": "user", "cwd": "/w",
                          "args": ["--dangerously-skip-permissions"]})
    assert launch(resumed, places) == ("claude --resume s1 --dangerously-skip-permissions", "cb1")
    fresh = pane("/w", {"session_id": "s9", "name": "my one", "name_source": "user", "cwd": "/w", "args": []})
    assert launch(fresh, places) == ("claude -n 'my one'", "my one (nothing said yet: a new session)")
    attached = pane("/w", {"job_id": "f2b0a87f", "job_name": "cb7", "session_id": None, "attach": True})
    assert launch(attached, places) == ("claude attach f2b0a87f", "background job cb7")
    # A job that has finished since: its session, resumed in the pane.
    finished = pane("/w", {"job_id": "0ld", "job_session_id": "s1", "session_id": "old", "args": []})
    assert launch(finished, places)[0] == "claude --resume s1"
    assert launch(pane("/w"), places) == (None, "")


# MARK: Keeping them

def test_the_same_again_is_not_saved(tmp_path):
    store = Snapshots(tmp_path)
    first = [{"name": "a", "windows": [window([pane("/w")])]}]
    assert store.save(first, 1000) is not None
    assert store.save(first, 1100) is None
    assert store.save(first + [{"name": "b", "windows": []}], 1200) is not None
    assert len(store.paths()) == 2
    # And ccsnap's own, the same thing in the same place, read alike; anything else isn't one.
    (store.dir / "junk.json").write_text("{\"not\": \"one\"}")
    assert [store.load(p)["saved_at"] for p in store.paths()] == [1200, 1000]


def test_three_days_are_kept_and_the_last_before_a_restart_whatever_its_age(tmp_path):
    store = Snapshots(tmp_path)
    now, boot = 100 * DAY, 100 * DAY - 3600
    for age in (10 * DAY, 9 * DAY, 4 * DAY, 2 * DAY):           # the machine was off a week
        store.save([{"name": f"before-{age}", "windows": []}], boot - age)
    for age in (1800, 60):
        store.save([{"name": f"after-{age}", "windows": []}], now - age)
    gone = store.prune(now, boot)
    assert [store.load(p)["sessions"][0]["name"] for p in store.paths()] == [
        "after-60", "after-1800", f"before-{2 * DAY}"]
    assert len(gone) == 3
    # Once it's not the latest from before a restart, it goes too.
    store.prune(now + 4 * DAY, now + 3 * DAY)
    assert [store.load(p)["sessions"][0]["name"] for p in store.paths()] == ["after-60"]


def test_kept_as_many_days_as_set(tmp_path):
    store = Snapshots(tmp_path, keep_for=DAY)
    now, boot = 100 * DAY, 50 * DAY                     # no restart among them
    for age in (2 * DAY, DAY + 60, DAY - 60, 60):
        store.save([{"name": f"at-{age}", "windows": []}], now - age)
    assert len(store.prune(now, boot)) == 2
    assert [store.load(p)["sessions"][0]["name"] for p in store.paths()] == ["at-60", f"at-{DAY - 60}"]
    # Kept longer from now on: what's there stays.
    store.keep_for = 7 * DAY
    assert store.prune(now + 2 * DAY, boot) == []


def test_how_often_and_how_long_as_set_within_bounds():
    assert snapshots.minutes(2) == 2 and snapshots.minutes(15) == 15 and snapshots.minutes(7.9) == 7
    assert snapshots.minutes(0) == 1 and snapshots.minutes(-5) == 1 and snapshots.minutes(500) == 60
    assert snapshots.minutes(float("inf")) == 60
    # A file anyone can edit: what isn't a number is the default.
    for odd in ("10", None, True, [], float("nan")):
        assert snapshots.minutes(odd) == 2 and snapshots.days(odd) == 3, odd
    assert snapshots.days(3) == 3 and snapshots.days(14) == 14
    assert snapshots.days(0) == 1 and snapshots.days(365) == 30


def test_the_one_to_restore(tmp_path, monkeypatch):
    store = Snapshots(tmp_path)
    monkeypatch.setattr(snapshots, "boot_id", lambda: "this-boot")
    boot = 5000
    old = store.save([{"name": "a", "windows": []}, {"name": "b", "windows": []}], 4000)
    newer = store.save([{"name": "a", "windows": []}], 4500)
    since = store.save([{"name": "fresh", "windows": []}], 6000)       # taken as the machine came up
    # Until a restore this boot: the newest from before it with something not running now.
    assert store.pick(set(), boot) == newer
    assert store.pick({"a"}, boot) == old
    assert store.pick({"a", "b"}, boot) == newer                       # nothing missing: the newest
    store.marker.write_text("this-boot")
    assert store.pick(set(), boot) == since
    assert store.pick({"fresh"}, boot) == newer


# MARK: Noting tmux as it is

def test_a_snapshot_notes_each_pane_and_what_claude_code_runs_there(private_tmux, places, tmp_path):
    claude = stand_in(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    private_tmux("-f", "/dev/null", "new-session", "-d", "-s", "cb", "-n", "claude", "-c", str(work),
                 "-x", "120", "-y", "40", sys.executable, str(claude), "--resume", "s1", "--dangerously-skip-permissions")
    private_tmux("split-window", "-t", "cb:0", "-c", str(work), sys.executable, str(claude), "attach", "f2b0a87f")
    private_tmux("new-window", "-t", "cb:1", "-n", "notes", "-c", str(work))
    first = private_tmux.pid_in("cb:0.0")
    listed(places, first, "s1", str(work), name="cb1", nameSource="user")
    transcript(places, "s1")
    job(places, "f2b0a87f", "s7", "cb7 saves", str(work))
    private_tmux.pid_in("cb:0.1")

    [session] = wait_for(lambda: capture(Tmux(private_tmux.name), places))
    assert session["name"] == "cb"
    claude_window, notes = session["windows"]
    assert (claude_window["index"], claude_window["name"], len(claude_window["panes"])) == (0, "claude", 2)
    resumed, attached = (p["claude"] for p in claude_window["panes"])
    assert resumed == {"session_id": "s1", "name": "cb1", "name_source": "user", "cwd": str(work),
                       "args": ["--dangerously-skip-permissions"], "had_transcript": True}
    assert attached == {"job_id": "f2b0a87f", "job_name": "cb7 saves", "job_session_id": "s7",
                        "session_id": None, "name": "cb7 saves", "cwd": str(work), "args": [], "attach": True}
    assert notes["name"] == "notes" and "claude" not in notes["panes"][0]
    assert notes["panes"][0]["cwd"] == str(work)
    assert snapshots.counts([session]) == (3, 2)


def test_no_tmux_running_is_nothing_to_note(places):
    assert capture(Tmux("ctally-pytest-none-" + str(os.getpid())), places) is None


# MARK: Bringing them back

def test_a_restore_makes_what_is_missing_and_starts_claude_code_in_it(private_tmux, places, tmp_path):
    private_tmux.new_session("running")            # the server, started without anyone's tmux.conf
    record = tmp_path / "typed.txt"
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> '{record}'\n")
    fake.chmod(0o755)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    transcript(places, "s1")
    job(places, "f2b0a87f", "s7", "cb7", str(two))
    store = Snapshots(tmp_path / "state")
    store.save([
        {"name": "running", "windows": [window([pane(str(one))])]},
        {"name": "cb", "windows": [
            window([pane(str(one), {"session_id": "s1", "name": "cb1", "cwd": str(one),
                                    "args": ["--dangerously-skip-permissions"]}),
                    pane(str(two), {"job_id": "f2b0a87f", "job_name": "cb7", "session_id": None, "cwd": str(two),
                                    "args": [], "attach": True}, index=1, active=False)], index=0, active=False),
            window([pane(str(two))], index=3, name="notes"),
        ]},
        {"name": "lost", "windows": [window([pane(str(tmp_path / "not-mounted"))])]},
    ], 1000)
    said = []
    done = restore(store, Tmux(private_tmux.name), places=places, wait=0, claude=str(fake),
                   settle=0.5, stagger=0, say=said.append)
    assert done.made == ["cb"]
    assert sorted(done.skipped) == ["lost: no folder " + str(tmp_path / "not-mounted"),
                                    "running: a tmux session of that name is running"]
    assert private_tmux("list-windows", "-t", "cb", "-F", "#{window_index} #{window_name} #{window_panes}") \
        .splitlines() == ["0 claude 2", "3 notes 1"]
    assert private_tmux("display-message", "-p", "-t", "cb", "#{window_index}") == "3"     # the active window
    assert private_tmux.pane("cb:0.1", "#{pane_current_path}") == str(two)
    typed = wait_for(lambda: record.exists() and sorted(record.read_text().splitlines()) == [
        "--resume s1 --dangerously-skip-permissions", "attach f2b0a87f"], timeout=10)
    assert typed, record.read_text() if record.exists() else "nothing typed"
    assert store.restored_this_boot()


def test_a_dry_run_changes_nothing(private_tmux, places, tmp_path):
    private_tmux.new_session("running")
    store = Snapshots(tmp_path / "state")
    store.save([{"name": "cb", "windows": [window([pane(str(tmp_path))])]}], 1000)
    said = []
    done = restore(store, Tmux(private_tmux.name), places=places, wait=0, dry_run=True, say=said.append)
    assert done.made == ["cb"] and private_tmux("has-session", "-t", "=cb", check=False) == ""
    assert "cb" not in private_tmux("list-sessions", "-F", "#{session_name}").split()
    assert not store.restored_this_boot()


def test_the_app_finds_what_there_is_to_restore_and_takes_snapshots(private_tmux, places, tmp_path):
    private_tmux.new_session("running")
    store = Snapshots(tmp_path / "state")
    keeper = Keeper(store, Tmux(private_tmux.name), places, log=lambda _: None)
    assert keeper.restorable() is None                          # nothing saved yet
    keeper.save_soon()
    assert wait_for(lambda: store.paths())
    assert keeper.restorable() is None                          # all of it is running
    store.save([{"name": "gone", "windows": [window([pane(str(tmp_path))])]}], 1000)
    path, saved = keeper.restorable()
    assert saved == 1000 and store.load(path)["sessions"][0]["name"] == "gone"


def test_the_app_takes_over_from_ccsnap_and_restores_in_the_background(private_tmux, places, tmp_path, monkeypatch):
    private_tmux.new_session("running")
    monkeypatch.setattr(snapshots, "stop_ccsnap", lambda: "ccsnap's systemd timer (ccsnap.timer)")
    said = []
    store = Snapshots(tmp_path / "state")
    keeper = Keeper(store, Tmux(private_tmux.name), places, log=said.append)
    keeper.take_over()
    assert wait_for(lambda: said) == ["switched off ccsnap's systemd timer (ccsnap.timer): CTally takes the snapshots now"]

    store.save([{"name": "gone", "windows": [window([pane(str(tmp_path))])]}], 1000)
    gate = threading.Event()
    keeper.pool.submit(gate.wait)                                       # holds the restore back
    keeper.restore_soon()
    assert keeper.restoring and keeper.restorable() is None             # one at a time
    gate.set()
    assert wait_for(lambda: not keeper.restoring, timeout=20)
    assert "gone" in private_tmux("list-sessions", "-F", "#{session_name}").split()
    assert keeper.restorable() is None and store.restored_this_boot()
    assert not [line for line in said if "stopped" in line or "left out" in line], said


def test_the_snapshot_command(private_tmux):
    """`ctally snapshot`, run as it is from a shell, on this test's tmux server, snapshots and
    Claude Code folder (see conftest)."""
    def ctally(*args, server=private_tmux.name):
        env = {**os.environ, "CCSNAP_TMUX_SOCKET": server, "PYTHONPATH": str(ROOT / "src")}
        return subprocess.run([sys.executable, "-m", "ctally", "snapshot", *args], capture_output=True,
                              text=True, timeout=60, env=env)

    done = ctally("save", server=f"ctally-pytest-none-{os.getpid()}")
    assert done.returncode == 1 and "no tmux server running" in done.stderr
    done = ctally("list")
    assert done.returncode == 1 and "no snapshots yet" in done.stderr

    private_tmux.new_session("keep")
    private_tmux.new_session("work")
    done = ctally("save")
    assert done.returncode == 0 and done.stdout.startswith("saved "), done.stderr
    assert "2 tmux sessions, 2 panes, 0 Claude Code sessions" in done.stdout
    assert ctally("save").stdout.startswith("nothing has changed since the last one")

    done = ctally("list")
    rows = [line for line in done.stdout.splitlines() if line.lstrip(" *")[:1].isdigit()]
    assert done.returncode == 0 and len(rows) == 1, done.stdout
    assert rows[0].split()[:2] == ["*", "1"] and rows[0].endswith("keep(1) work(1)"), rows    # the newest
    done = ctally("restore", "2")
    assert done.returncode == 1 and "no snapshot 2" in done.stderr

    private_tmux("kill-session", "-t", "work")
    done = ctally("restore", "1", "--dry-run")
    assert done.returncode == 0 and "work:0.0  shell" in done.stdout, done.stdout + done.stderr
    assert "keep: a tmux session of that name is running" in done.stdout
    assert private_tmux("has-session", "-t", "=work", check=False) == ""
    assert "work" not in private_tmux("list-sessions", "-F", "#{session_name}").split()
    done = ctally("restore", "1")
    assert done.returncode == 0 and "made 1 tmux session(s), started 0 Claude Code session(s)" in done.stdout, \
        done.stdout + done.stderr
    assert "work" in private_tmux("list-sessions", "-F", "#{session_name}").split()


def test_the_snapshot_command_keeps_them_as_long_as_the_settings_say(private_tmux):
    from ctally.prefs import Prefs

    def save():
        env = {**os.environ, "CCSNAP_TMUX_SOCKET": private_tmux.name, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "ctally", "snapshot", "save"], capture_output=True,
                              text=True, timeout=60, env=env)
        assert done.returncode == 0 and done.stdout.startswith("saved "), done.stdout + done.stderr

    store = Snapshots()
    now = time.time()
    for age in (2.6 * DAY, 2.5 * DAY, 2.4 * DAY):
        store.save([{"name": f"old-{age}", "windows": []}], now - age)

    def old() -> int:
        return sum(1 for p in store.paths() if store.load(p)["sessions"][0]["name"].startswith("old-"))

    private_tmux.new_session("one")
    save()
    assert old() == 3                   # three days, as it was
    Prefs()["snapshot_days"] = 1
    private_tmux.new_session("two")
    save()
    # A day: gone, but for the newest from before the computer started, if one of them is.
    assert old() <= 1 and len(store.paths()) == old() + 2


def test_when_it_was_saved():
    now = time.mktime((2026, 11, 17, 22, 30, 0, 0, 0, -1))
    assert snapshots.saved_when(now - 600, now).endswith("pm")
    assert snapshots.saved_when(now - 2 * DAY, now).startswith("Sun ")
    assert snapshots.saved_when(now - 9 * DAY, now) == "Nov 8"


# MARK: ccsnap's own timer

@pytest.mark.skipif(system.MAC, reason="systemd, on Linux")
def test_ccsnap_s_timer_is_switched_off_only_when_it_is_on(monkeypatch):
    monkeypatch.setattr(snapshots.shutil, "which", lambda name: "/usr/bin/" + name)
    ran = []

    def run(on: bool):
        def answer(args):
            ran.append(args)
            return "enabled\n" if on and "is-enabled" in args else None
        return answer

    assert stop_ccsnap(run(True)) == "ccsnap's systemd timer (ccsnap.timer)"
    assert ran[-1] == ["systemctl", "--user", "disable", "--now", "ccsnap.timer"]
    ran.clear()
    assert stop_ccsnap(run(False)) is None
    assert all("disable" not in args for args in ran)
