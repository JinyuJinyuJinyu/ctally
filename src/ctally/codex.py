"""Codex lifecycle hooks and their reader. No approvals or model input are returned.

Hooks keep small JSON records in $CODEX_HOME/ctally.d. The optional context gauge reads
token_count events from the rollout, whose format is version-dependent. Missing or
unrecognized transcript data leaves the gauge blank; lifecycle hooks supply the state.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import system
from .codex_hooks import EVENTS, home
from .sessions import Ask, Context, Session, State, TmuxLocator


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _process() -> tuple[int, int]:
    """The owning Codex process and a process in its terminal, if one is known.

    With a shared daemon the hook's ancestors have no terminal. Codex replays the
    session environment for hooks, so its tmux pane still gives the correct terminal.
    Outside tmux we leave the terminal unknown instead of focusing the daemon's parent.
    """
    owner, terminal = 0, 0
    pid = os.getppid()
    for _ in range(16):
        program = system.program(pid) if system.LINUX else system.run(["ps", "-o", "comm=", "-p", str(pid)])
        if program and Path(program.strip()).name in ("codex", "codex-cli"):
            owner = owner or pid
            if system.tty(pid):
                terminal = pid
                break
        parent = system.parent(pid)
        if not parent or parent <= 1 or parent == pid:
            break
        pid = parent
    target = system.tmux_pane(os.getpid())
    if target:
        pane_pid = system.run([target.binary, "-S", target.socket, "display-message", "-p",
                               "-t", target.pane, "#{pane_pid}"], timeout=1)
        if pane_pid and pane_pid.strip().isdigit():
            return owner, int(pane_pid.strip())
    return owner, terminal


def _tool_key(payload: dict) -> str:
    inputs = payload.get("tool_input")
    if isinstance(inputs, dict) and ("command" in inputs or "cmd" in inputs):
        # Command approvals may add a display-only description.
        inputs = {key: value for key, value in inputs.items() if key != "description"}
    data = [payload.get("agent_id"), payload.get("tool_name"), payload.get("cwd"), inputs]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _question(payload: dict) -> str:
    inputs = payload.get("tool_input")
    inputs = inputs if isinstance(inputs, dict) else {}
    questions = inputs.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        return str(questions[0].get("question") or questions[0].get("title") or "Waiting for your answer")[:500]
    description = inputs.get("description") or inputs.get("justification")
    if description:
        return str(description)[:500]
    tool = payload.get("tool_name") or "tool"
    command = inputs.get("command") or inputs.get("cmd")
    return f"Allow {tool}" + (f"({str(command)[:400]})?" if command else "?")


def record(payload: dict, directory: Path) -> None:
    sid, event = payload.get("session_id"), payload.get("hook_event_name")
    if not isinstance(sid, str) or not sid or sid.startswith(".") or "/" in sid or "\\" in sid or event not in EVENTS:
        return
    # A child's Stop or SessionStart must never replace its parent's state.
    if payload.get("agent_id"):
        return
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.json"
    owner, terminal = _process() if event != "SessionEnd" else (0, 0)
    # Parallel tool hooks must not lose another tool's outstanding approval.
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if event == "SessionEnd":
            path.unlink(missing_ok=True)
            return
        previous = _read(path)
        now = time.time()
        value = {**previous, "id": sid, "owner_pid": owner, "owner_start": system.started(owner) if owner else None,
                 "pid": terminal, "proc_start": system.started(terminal) if terminal else None,
                 "cwd": payload.get("cwd", "") if event == "SessionStart" else
                        previous.get("cwd") or payload.get("cwd", ""),
                 "transcript_path": payload.get("transcript_path") or previous.get("transcript_path"),
                 "started": previous.get("started", now), "updated": now}
        pending = value.setdefault("pending", {})
        state = previous.get("state", "idle")
        tool = str(payload.get("tool_name", "")).rsplit(".", 1)[-1]
        if event == "SessionStart":
            if payload.get("source") != "compact":
                state, pending = "idle", {}
        elif event == "UserPromptSubmit":
            state, pending = "working", {}
        elif event == "PreModelRequest":
            state = "working"
            pending = {key: question for key, question in pending.items() if key.startswith("async:")}
        elif event in ("Stop", "Interrupt"):
            state, pending = "done", {}
        elif event == "PermissionRequest" or (event == "PreToolUse" and
                tool in ("request_user_input", "request_user_input_async")):
            # Permission hooks have no call ID; keep each identical request.
            base = key = _tool_key(payload)
            index = 0
            while key in pending or "async:" + key in pending:
                index += 1
                key = f"{base}:{index}"
            pending[key] = _question(payload)
        else:
            state = "working"
            if event == "PostToolUse":
                base = _tool_key(payload)
                key = next((key for key in pending if key.split(":", 1)[0] == base), base)
                question = pending.pop(key, None)
                if tool == "request_user_input_async" and question is not None:
                    # Only a successful submission gets PostToolUse. Failed calls
                    # stay temporary and are cleared by the next model request.
                    pending["async:" + key] = question
        value.update(state="waiting" if pending else state, pending=pending)
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".state.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)


def context_from(payload: dict) -> Context | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    size, tokens = info.get("model_context_window"), usage.get("total_tokens")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (size, tokens)) or size <= 0 or tokens < 0:
        return None
    return Context(100 * tokens / size, int(size), int(tokens))


class Transcript:
    """Read only appended rollout data, at most 1 MiB per poll, keeping partial lines."""

    def __init__(self):
        self.stamp = None
        self.offset = 0
        self.context = None
        self.state = None
        self.changed = 0.0

    def poll(self, path: Path) -> None:
        try:
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if stamp == self.stamp:
                return
            if self.stamp and (stamp[0] != self.stamp[0] or stamp[1] <= self.stamp[1]):
                self.offset, self.context, self.state, self.changed = 0, None, None, 0.0
            start = max(self.offset, stat.st_size - 1024 * 1024)
            with path.open("rb") as stream:
                stream.seek(start)
                data = stream.read(1024 * 1024)
        except OSError:
            return
        last = data.rfind(b"\n")
        if last < 0:
            return
        lines = data[:last].split(b"\n")
        if start > self.offset:
            lines = lines[1:]
        self.offset, self.stamp = start + last + 1, stamp
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("type") != "event_msg":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "token_count":
                context = context_from(payload)
                if context is not None:
                    self.context = context
            state = {"task_started": State.WORKING, "task_complete": State.DONE,
                     "turn_aborted": State.DONE}.get(kind)
            if state is not None:
                try:
                    changed = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                self.state, self.changed = state, changed


class CodexReader:
    def __init__(self, directory: Path | None = None):
        self.dir = directory if directory is not None else home() / "ctally.d"
        self.transcripts: dict[str, tuple[str, Transcript]] = {}
        self.records: dict[str, tuple[tuple, dict]] = {}
        self.locator = TmuxLocator()
        self.updated: dict[str, float] = {}
        self.titles: dict[str, str] = {}
        self.title_stamp = None

    def _read_titles(self) -> None:
        """Codex's saved names, refreshed even when no lifecycle hook has fired."""
        path = self.dir.parent / "session_index.jsonl"
        try:
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if stamp == self.title_stamp:
                return
            titles = {}
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    sid, title = item.get("id"), item.get("thread_name")
                    if isinstance(sid, str) and isinstance(title, str) and title.strip():
                        titles[sid] = title.strip()   # a later entry is a rename
        except OSError:
            titles, stamp = {}, None
        self.titles, self.title_stamp = titles, stamp

    def poll(self) -> list[Session]:
        self._read_titles()
        found = []
        started = {}
        files = set()
        for path in sorted(self.dir.glob("*.json")):
            files.add(path.name)
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = self.records.get(path.name)
            value = cached[1] if cached and cached[0] == stamp else _read(path)
            self.records[path.name] = (stamp, value)
            sid = value.get("id")
            if not isinstance(sid, str) or path.name != f"{sid}.json":
                continue
            owner, pid = value.get("owner_pid"), value.get("pid")
            if any(type(p) is not int or p < 0 for p in (owner, pid)):
                continue
            if owner and (not system.is_alive(owner) or (value.get("owner_start") and
                    system.started(owner) not in (None, value["owner_start"]))):
                continue
            # A closed or reused terminal only loses navigation; its daemon may
            # still have a live task. Without an owner, the terminal is our fallback.
            if pid and (not system.is_alive(pid) or (value.get("proc_start") and
                    system.started(pid) not in (None, value["proc_start"]))):
                pid = 0
            if not owner and not pid:
                continue
            cwd = value.get("cwd")
            if not isinstance(cwd, str):
                continue
            updated, began = value.get("updated"), value.get("started")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (updated, began)):
                continue
            state = State.parse(value.get("state", "idle")) if isinstance(value.get("state"), str) else State.IDLE
            context = None
            transcript = value.get("transcript_path")
            if isinstance(transcript, str) and transcript:
                cached = self.transcripts.get(sid)
                reader = cached[1] if cached and cached[0] == transcript else Transcript()
                self.transcripts[sid] = (transcript, reader)
                reader.poll(Path(transcript))
                context = reader.context
                if reader.state and reader.changed > updated:
                    state, updated = reader.state, reader.changed
            pending = value.get("pending")
            questions = list(pending.values()) if isinstance(pending, dict) else []
            ask = Ask("question", questions[0]) if state is State.WAITING and questions and isinstance(questions[0], str) else None
            identity = "codex:" + sid
            self.updated[identity] = updated
            started[identity] = began
            found.append(Session(identity, state, Path(cwd).name or "Codex", pid=pid,
                                 title=self.titles.get(sid), path=cwd,
                                 tmux=self.locator.location(identity, pid) if pid else None,
                                 ask=ask, context=context, source="codex", owner_pid=owner))
        keep = {s.id for s in found}
        self.records = {k: v for k, v in self.records.items() if k in files}
        self.transcripts = {k: v for k, v in self.transcripts.items() if "codex:" + k in keep}
        self.updated = {k: v for k, v in self.updated.items() if k in keep}
        self.locator.forget(keep)
        return sorted(found, key=lambda s: (started[s.id], s.id))


def main() -> None:
    # Hooks are observational: even malformed input or an unwritable folder cannot
    # deny a tool, continue a turn, or add anything to the conversation.
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record(payload, (Path(sys.argv[1]) if len(sys.argv) > 1 else home()) / "ctally.d")
    except (OSError, ValueError, TypeError):
        pass
    print("{}")


if __name__ == "__main__":
    main()
