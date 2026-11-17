"""Snapshots of tmux and its Claude Code sessions, and bringing them back. tmux runs as a private
server, and Claude Code's folders are temporary: nothing here touches the real ones."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import wait_for

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
