"""Codex's observer hooks, incremental rollouts, and mixed CLI sessions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from conftest import dead_pid
from ctally import cli, codex, codex_hooks, system
from ctally.sessions import Context, State, StateReader


@pytest.fixture
def observer(own_usage_and_prefs, monkeypatch):
    directory = codex_hooks.home() / "ctally.d"
    monkeypatch.setattr(codex, "_process", lambda: (os.getpid(), os.getpid()))
    monkeypatch.setattr(codex.TmuxLocator, "location", lambda *args: None)

    def emit(event, **fields):
        codex.record({"session_id": "one", "hook_event_name": event,
                      "cwd": "/work/my project", **fields}, directory)

    return directory, emit


def read(directory):
    return json.loads((directory / "one.json").read_text())


def test_lifecycle_and_session_end(observer):
    directory, emit = observer
    reader = codex.CodexReader(directory)
    for event, expected in (("SessionStart", State.IDLE), ("UserPromptSubmit", State.WORKING),
                            ("Stop", State.DONE), ("UserPromptSubmit", State.WORKING),
                            ("Interrupt", State.DONE)):
        emit(event)
        [session] = reader.poll()
        assert (session.id, session.source, session.label) == ("codex:one", "codex", "my project")
        assert session.state is expected
    emit("SessionEnd")
    assert reader.poll() == []


def test_parallel_tools_preserve_outstanding_approvals(observer):
    directory, emit = observer
    first = {"tool_name": "Bash", "tool_input": {"command": "npm publish"}}
    second = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
    emit("UserPromptSubmit")
    emit("PermissionRequest", **first)
    emit("PreToolUse", **second)
    emit("PostToolUse", **second)
    assert read(directory)["state"] == "waiting"
    emit("PermissionRequest", **second)
    emit("PostToolUse", **first)
    [session] = codex.CodexReader(directory).poll()
    assert session.state is State.WAITING
    assert session.ask.summary == "Allow Bash(git push)?"
    emit("PostToolUse", **second)
    assert read(directory)["state"] == "working"
    emit("PermissionRequest", **first)
    emit("PreModelRequest")     # a refused tool may never get PostToolUse
    assert read(directory)["pending"] == {}
    assert read(directory)["state"] == "working"


def test_question_waits_until_its_own_tool_returns(observer):
    directory, emit = observer
    question = {"tool_name": "functions.request_user_input", "tool_input": {
        "questions": [{"question": "Which database?", "options": []}]}}
    emit("PreToolUse", **question)
    [session] = codex.CodexReader(directory).poll()
    assert session.state is State.WAITING and session.ask.summary == "Which database?"
    emit("PostToolUse", tool_name="Bash", tool_input={"command": "true"})
    assert read(directory)["state"] == "waiting"
    emit("PostToolUse", **question)
    assert read(directory)["state"] == "working"


def test_compaction_and_subagent_events_do_not_overwrite_root(observer):
    directory, emit = observer
    emit("UserPromptSubmit")
    emit("SessionStart", source="compact")
    assert read(directory)["state"] == "working"
    before = read(directory)
    for event in ("SessionStart", "Stop", "SessionEnd", "PermissionRequest"):
        emit(event, agent_id="child")
    assert read(directory) == before
    emit("SessionStart", source="resume")
    assert read(directory)["state"] == "idle"


@pytest.mark.parametrize("sid", ["", ".", "../outside", "a/b", "a\\b", None, 12])
def test_session_ids_cannot_escape_state_folder(observer, sid):
    directory, emit = observer
    emit("UserPromptSubmit", session_id=sid)
    assert not directory.exists()


def event(kind, timestamp=None, **payload):
    return json.dumps({"type": "event_msg", "timestamp": datetime.fromtimestamp(
        timestamp or time.time(), timezone.utc).isoformat(), "payload": {"type": kind, **payload}}) + "\n"


def tokens(count, window=200_000):
    return event("token_count", info={"last_token_usage": {"total_tokens": count},
                                      "total_token_usage": {"total_tokens": 9_000_000},
                                      "model_context_window": window})


def test_context_uses_latest_request_and_handles_partial_lines(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(tokens(100_000))
    reader = codex.Transcript()
    reader.poll(path)
    assert reader.context == Context(50, 200_000, 100_000)
    position = reader.offset
    reader.poll(path)
    assert reader.offset == position
    next_line = tokens(40_000)
    with path.open("a") as stream:
        stream.write(next_line[:30])
    reader.poll(path)
    assert reader.context.percent == 50
    assert reader.offset == position
    with path.open("a") as stream:
        stream.write(next_line[30:])
    reader.poll(path)
    assert reader.context == Context(20, 200_000, 40_000)
    path.write_text(tokens(0))       # compaction or a replacement rollout
    reader.poll(path)
    assert reader.context == Context(0, 200_000, 0)


def test_large_or_unknown_transcripts_do_not_invent_context(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text('"' + 'x' * (2 * 1024 * 1024) + '"\nnull\n[]\n' + tokens(25_000))
    reader = codex.Transcript()
    reader.poll(path)
    assert reader.context.percent == 12.5
    path.write_text('{"type":"some_future_format"}\n')
    reader.poll(path)
    assert reader.context is None


@pytest.mark.parametrize("window,count", [(0, 10), (True, 10), (200, -1), (200, "50"),
                                          (200, float("nan")), (float("inf"), 50)])
def test_invalid_token_counts_are_ignored(window, count):
    assert codex.context_from({"info": {"model_context_window": window,
                                        "last_token_usage": {"total_tokens": count}}}) is None


def test_newer_rollout_recovers_a_missed_stop_but_old_turn_does_not(observer, tmp_path):
    directory, emit = observer
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(event("task_complete", time.time() - 60) + tokens(30_000))
    emit("UserPromptSubmit", transcript_path=str(transcript))
    reader = codex.CodexReader(directory)
    [session] = reader.poll()
    assert session.state is State.WORKING and session.context.percent == 15
    with transcript.open("a") as stream:
        stream.write(event("turn_aborted", time.time() + 1))
    [session] = reader.poll()
    assert session.state is State.DONE


def test_dead_process_reused_pid_and_bad_records_are_ignored(observer):
    directory, emit = observer
    emit("UserPromptSubmit")
    value = read(directory)
    path = directory / "one.json"
    (directory / "broken.json").write_text("not json")
    (directory / "list.json").write_text("[]")
    path.write_text(json.dumps({**value, "owner_pid": dead_pid()}))
    assert codex.CodexReader(directory).poll() == []
    path.write_text(json.dumps({**value, "pid": 0, "owner_pid": 0}))
    assert codex.CodexReader(directory).poll() == []
    if system.LINUX:
        path.write_text(json.dumps({**value, "owner_start": "not-the-same-process"}))
        assert codex.CodexReader(directory).poll() == []


def test_atomic_state_replacement_is_seen_even_with_same_size_and_mtime(observer, monkeypatch):
    directory, emit = observer
    emit("Stop")
    reader = codex.CodexReader(directory)
    assert reader.poll()[0].state is State.DONE
    path = directory / "one.json"
    previous = path.stat()
    replacement = directory / ".replacement"
    replacement.write_text(path.read_text().replace('"done"', '"idle"'))
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)
    assert reader.poll()[0].state is State.IDLE
    monkeypatch.setattr(codex, "_read", lambda _: pytest.fail("unchanged state was read again"))
    assert reader.poll()[0].state is State.IDLE


def test_daemon_without_terminal_is_visible_without_fake_focus(observer, monkeypatch):
    directory, emit = observer
    monkeypatch.setattr(codex, "_process", lambda: (os.getpid(), 0))
    emit("UserPromptSubmit")
    [session] = codex.CodexReader(directory).poll()
    assert session.state is State.WORKING and session.pid == 0 and session.tmux is None


def test_mix_with_claude_in_gui_reader_cli_counts_and_jump(observer, tmp_path, monkeypatch, capsys):
    directory, emit = observer
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "one").write_text(f"working {os.getpid()} api\n")
    monkeypatch.setattr(cli, "STATE_DIR", claude)
    emit("Stop")
    sessions = StateReader(claude, tmp_path / "projects").poll()
    assert {s.id: s.state for s in sessions} == {"one": State.WORKING, "codex:one": State.DONE}
    cli.main(["status"])
    assert capsys.readouterr().out == "▶1 ✓1\n"
    cli.main(["list"])
    assert "[codex] my project" in capsys.readouterr().out
    target = system.TmuxPane("tmux", "/tmp/ctally-test-server", "%4")
    monkeypatch.setattr(system, "tmux_pane", lambda _: target)
    calls = []
    monkeypatch.setattr(system, "run", lambda args: calls.append(args) or "")
    assert cli.jump("%1", "client", target.socket) == 0
    assert any(args[-4:] == ["-c", "client", "-t", "%4"] for args in calls)


def test_hook_returns_only_empty_json_and_never_changes_approval(tmp_path):
    env = {**os.environ, "CTALLY_CODEX_HOME": str(tmp_path)}
    for payload in ('not json', '[]', '{"session_id":"../bad","hook_event_name":"PermissionRequest"}'):
        result = subprocess.run([sys.executable, "-m", "ctally.codex"], input=payload,
                                capture_output=True, text=True, env=env, timeout=5)
        assert result.returncode == 0 and result.stdout == "{}\n" and not result.stderr
    assert not (tmp_path / "ctally.d").exists()


def test_tmux_hook_uses_session_pane_even_with_shared_daemon(private_tmux, monkeypatch):
    location = private_tmux.new_session("codex")
    pane_pid = private_tmux.pid_in(location)
    target = system.TmuxPane(system.tmux_binary(), private_tmux.socket, private_tmux.pane(location, "#{pane_id}"))
    monkeypatch.setattr(system, "tmux_pane", lambda _: target)
    monkeypatch.setattr(system, "LINUX", True)
    monkeypatch.setattr(system, "program", lambda _: "codex")
    owner, terminal = codex._process()
    assert owner == os.getppid() and terminal == pane_pid


def test_private_app_server_uses_its_parent_codex_terminal(monkeypatch):
    monkeypatch.setattr(system, "LINUX", True)
    monkeypatch.setattr(codex.os, "getppid", lambda: 10)
    monkeypatch.setattr(system, "program", lambda pid: "sh" if pid == 10 else "codex")
    monkeypatch.setattr(system, "parent", lambda pid: {10: 20, 20: 30, 30: 1}[pid])
    monkeypatch.setattr(system, "tty", lambda pid: "/dev/pts/1" if pid == 30 else None)
    monkeypatch.setattr(system, "tmux_pane", lambda _: None)
    assert codex._process() == (20, 30)
