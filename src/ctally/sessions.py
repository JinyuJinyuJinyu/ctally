"""What CTally knows about Claude Code sessions.

CTally's hooks keep one file per session in ~/.claude/ctally.d/<session id>, holding
"<state> <pid> <project name>" (working | done | waiting | limited; anything else means
idle), and a file per running subagent in ~/.claude/ctally.d/.agents/<session id>/. A session
stopped by the plan's usage limit shows as waiting until the limit resets (.limit says when).
A turn I stop by hand (Esc), and agents I kill, get no hook at all, so the transcripts are
what show those. Each session's name
and directory come from its transcript in ~/.claude/projects/, where it sits in tmux from
its process's environment, and what a workflow is up to from its journal.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from . import system

STATE_DIR = Path(os.environ.get("CTALLY_STATE_DIR") or Path.home() / ".claude" / "ctally.d")
PROJECTS_DIR = Path(os.environ.get("CTALLY_PROJECTS_DIR") or Path.home() / ".claude" / "projects")


class State(Enum):
    WORKING = "working"
    WAITING = "waiting"
    DONE = "done"
    IDLE = "idle"

    @property
    def urgency(self) -> int:
        """How loudly this state asks for me; decides who survives the row cap."""
        return {State.WAITING: 3,   # blocked on me; that session cannot proceed
                State.DONE: 2,      # finished, wants my next instruction
                State.WORKING: 1,   # busy, needs nothing
                State.IDLE: 0}[self]

    @classmethod
    def parse(cls, word: str) -> State:
        try:
            return cls(word.lower())
        except ValueError:
            return cls.IDLE


# Most urgent first: the order counts are listed in.
BY_URGENCY = [State.WAITING, State.WORKING, State.DONE, State.IDLE]


@dataclass(frozen=True)
class Agents:
    """A session's subagents while they run, its own and a workflow's."""
    running: int
    started: int = 0            # a workflow's agents set off so far
    done: int = 0               # and of those, finished
    detail: str | None = None   # the workflow's phase, or what a lone agent was asked to do

    @property
    def summary(self) -> str:
        """"4 agents · Verify\""""
        count = "1 agent" if self.running == 1 else f"{self.running} agents"
        return f"{count} · {self.detail}" if self.detail else count

    @property
    def progress(self) -> str | None:
        """"12/16": a workflow's progress, as far as it has got. A workflow decides as it
        goes how many agents to set off, so the total grows as each phase starts."""
        return f"{min(self.done, self.started)}/{self.started}" if self.started else None


@dataclass(frozen=True)
class Session:
    """One session as the indicator needs to see it."""
    id: str
    state: State
    label: str                      # project folder name, from the hook
    pid: int = 0                    # the claude process, from the hook
    title: str | None = None        # session name, from the transcript
    path: str | None = None         # directory the session started in, from the transcript
    tmux: str | None = None         # where it sits in tmux, "report:0.2", if it runs there
    agents: Agents | None = None    # its subagents, while any run
    limited: bool = False           # waiting on the plan's usage limit, not on me


# MARK: Stopping by hand

# What Claude Code writes in a transcript when I press Esc: "[Request interrupted by user]", or
# "…by user for tool use]" when a tool was running. A subagent's transcript gets it too.
INTERRUPTED = "[Request interrupted by user"
# What a command I run in Claude Code leaves, /compact or a "!" shell command, say: nothing
# that starts Claude on a turn (and a command that does, starts it with a hook).
COMMANDS = ("/", "<command-", "<local-command-", "<bash-")


def _records(tail: bytes):
    """A transcript's records, newest first. The first line of a stretch read from the
    middle may be cut short, and simply won't parse."""
    for line in reversed(tail.split(b"\n")):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _texts(record: dict) -> list[str]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part.get("text") or "" for part in content if isinstance(part, dict) and part.get("type") == "text"]
    return []


def _when(record: dict) -> float | None:
    """A record's time ("2026-10-07T10:28:25.991Z"), in seconds since the epoch."""
    try:
        return datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None


def stop_in(tail: bytes) -> float | None:
    """When the session's turn was stopped by hand, if the end of its transcript says so and
    nothing has happened since: Esc pressed, its agents killed, or the conversation moved on
    to another session id (which this one will never hear of again). None otherwise."""
    for record in _records(tail):
        if record.get("isSidechain"):
            continue
        kind = record.get("type")
        if kind == "continued-in" or (kind == "system" and record.get("subtype") == "agents_killed"):
            return _when(record)
        if kind == "assistant":
            return None                     # Claude answered after any stop
        if kind == "user" and not record.get("isMeta") and not record.get("isVisibleInTranscriptOnly"):
            texts = _texts(record)
            if any(text.startswith(INTERRUPTED) for text in texts):
                return _when(record)
            if texts and all(text.lstrip().startswith(COMMANDS) for text in texts):
                continue
            return None                     # a prompt, or a tool's result: the turn went on
    return None


def cut_short(tail: bytes) -> bool:
    """Whether a subagent's transcript ends with the agent stopped: interrupted (killed, or
    Esc pressed while it ran), or given up on an API error such as the usage limit."""
    for record in _records(tail):
        kind = record.get("type")
        if kind == "assistant":
            return bool(record.get("isApiErrorMessage"))
        if kind == "user" and not record.get("isMeta"):
            return any(text.startswith(INTERRUPTED) for text in _texts(record))
    return False


# MARK: Session names

class Names:
    """Names a session the way Claude Code's own session list does: the title I gave it with
    /rename, else the one Claude generated, plus the directory it started in. Both live in
    the session's transcript, which can run to hundreds of megabytes, so only its ends are
    read: the start once for the directory, the last stretch for the title when it moves."""

    WINDOW = 256 * 1024
    CWD = re.compile(rb'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')

    @dataclass
    class Entry:
        transcript: Path | None = None
        path: str | None = None
        custom: str | None = None
        generated: str | None = None
        stopped: float | None = None        # when I last stopped its turn, if nothing came after
        modified: float = -1
        last_look: float = 0

    def __init__(self, projects: Path = PROJECTS_DIR):
        self.projects = projects
        self.entries: dict[str, Names.Entry] = {}

    def info(self, sid: str, busy: bool = False) -> tuple[str | None, str | None]:
        """Cheap to call every poll: the transcript is looked at every few seconds at most,
        and read only when it has changed since."""
        entry = self.entries.setdefault(sid, Names.Entry())
        # Claude names a session after its first prompt, so look harder until it has; and
        # while it's busy, so a turn I stop shows as over soon after.
        every = 2 if busy or (entry.custom or entry.generated) is None else 5
        now = time.monotonic()
        if now - entry.last_look >= every:
            entry.last_look = now
            self._refresh(entry, sid)
        return entry.custom or entry.generated, entry.path

    def transcript(self, sid: str) -> Path | None:
        """The session's transcript, once a look for its name has found it."""
        entry = self.entries.get(sid)
        return entry.transcript if entry else None

    def stopped(self, sid: str) -> float | None:
        """When I stopped the session's turn by hand, as its transcript's last look found:
        see stop_in."""
        entry = self.entries.get(sid)
        return entry.stopped if entry else None

    def forget(self, keep: set[str]) -> None:
        self.entries = {k: v for k, v in self.entries.items() if k in keep}

    def _refresh(self, entry: Names.Entry, sid: str) -> None:
        if entry.transcript is None:
            entry.transcript = self._locate(sid)
        if entry.transcript is None:
            return
        try:
            modified = entry.transcript.stat().st_mtime
            if modified == entry.modified:
                return
            entry.modified = modified
            with open(entry.transcript, "rb") as f:
                if entry.path is None:
                    entry.path = self._cwd(f.read(Names.WINDOW), last=False)
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - Names.WINDOW))
                tail = f.read()
        except OSError:
            return
        if entry.path is None:
            entry.path = self._cwd(tail, last=True)
        entry.stopped = stop_in(tail)

        # Newest first; the first line of the window may be cut, and simply won't parse.
        custom = generated = None
        for line in reversed(tail.split(b"\n")):
            if custom is not None and generated is not None:
                break
            if len(line) >= 4096 or b'-title"' not in line:     # title records are tiny
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "custom-title" and custom is None:
                custom = record.get("customTitle")
            elif record.get("type") == "ai-title" and generated is None:
                generated = record.get("aiTitle")
        # A window without a title record keeps what an earlier read found.
        if custom:
            entry.custom = custom
        if generated:
            entry.generated = generated

    def _locate(self, sid: str) -> Path | None:
        """Transcripts sit in ~/.claude/projects/<encoded directory>/<session id>.jsonl."""
        try:
            folders = list(self.projects.iterdir())
        except OSError:
            return None
        for folder in folders:
            candidate = folder / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _cwd(data: bytes, last: bool) -> str | None:
        """The "cwd" field of a transcript record, found by bytes rather than by parsing
        lines that can each be megabytes long."""
        found = list(Names.CWD.finditer(data))
        for match in (reversed(found) if last else found):
            try:
                path = json.loads(match.group(1))
            except ValueError:
                continue
            if path:
                return path
        return None


# MARK: tmux locations

class TmuxLocator:
    """Where each session sits in tmux ("report:0.2"), for its row. Asking tmux means running
    it, so that happens off the main thread, at most every few seconds per session, and the
    answer turns up on a later poll."""

    EVERY = 10      # panes move and get renumbered, rarely

    def __init__(self):
        self.entries: dict[str, list] = {}      # id -> [location, checked, asking]
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctally-tmux")

    def location(self, sid: str, pid: int) -> str | None:
        with self.lock:
            entry = self.entries.setdefault(sid, [None, 0.0, False])
            if not entry[2] and pid > 0 and time.monotonic() - entry[1] >= TmuxLocator.EVERY:
                entry[2] = True
                self.pool.submit(self._look_up, sid, pid)
            return entry[0]

    def forget(self, keep: set[str]) -> None:
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if k in keep}

    def _look_up(self, sid: str, pid: int) -> None:
        target = system.tmux_pane(pid)
        found = system.tmux_location(target) if target else None
        with self.lock:
            entry = self.entries.get(sid)
            if entry is not None:               # unless the session has ended meanwhile
                entry[:] = [found, time.monotonic(), False]


# MARK: Subagents

class AgentWatcher:
    """What each session's subagents are up to. The hooks keep a file per running agent in
    ~/.claude/ctally.d/.agents/<session id>/, holding the agent's type: that says how many
    run. Beside the session's transcript, a workflow keeps a journal with a line as each of
    its agents starts (and in which phase) and as each ends, and a plain agent leaves a note
    of what it was asked to do: that says what they're doing.

    An agent I kill, or one cut short by Esc or the usage limit, gets no SubagentStop, so its
    file stays. Its own transcript says it's over (and its note, when I killed it), and it
    counts as gone."""

    EVERY = 2   # a workflow's progress, between agents coming and going
    GENERIC = {"", "general-purpose", "workflow-subagent"}

    @dataclass
    class Journal:
        modified: float = -1
        size: int = -1
        phase: str | None = None
        started: int = 0
        done: int = 0

    @dataclass
    class Look:
        folder_modified: float = -1
        last_look: float = 0
        activity: Agents | None = None
        journals: dict = field(default_factory=dict)        # by path
        descriptions: dict = field(default_factory=dict)    # by agent id
        transcripts: dict = field(default_factory=dict)     # by agent id: its path
        over: dict = field(default_factory=dict)            # by agent id: (when read, whether over)
        abandoned: bool = False

    def __init__(self, directory: Path):
        self.dir = directory
        self.looks: dict[str, AgentWatcher.Look] = {}

    def activity(self, sid: str, transcript: Path | None) -> Agents | None:
        """Cheap to call every poll: the agents' folder is one stat, and looked into only
        when an agent has come or gone, or every few seconds while any run."""
        folder = self.dir / sid
        try:
            modified = folder.stat().st_mtime
        except OSError:
            self.looks.pop(sid, None)
            return None
        look = self.looks.setdefault(sid, AgentWatcher.Look())
        now = time.monotonic()
        if modified != look.folder_modified or now - look.last_look >= AgentWatcher.EVERY:
            look.folder_modified = modified
            look.last_look = now
            look.activity = self._examine(folder, transcript, look)
        return look.activity

    def abandoned(self, sid: str) -> bool:
        """Whether the session's turn ended leaving agents to run on, and those have all
        been stopped since: nothing will come back to start Claude again."""
        look = self.looks.get(sid)
        return bool(look and look.abandoned)

    def forget(self, keep: set[str]) -> None:
        self.looks = {k: v for k, v in self.looks.items() if k in keep}

    def _examine(self, folder: Path, transcript: Path | None, look: Look) -> Agents | None:
        look.abandoned = False
        # Dot files are the hook's own notes, not agents.
        try:
            listed = sorted(p for p in os.listdir(folder) if not p.startswith("."))
        except OSError:
            return None
        agents = listed
        if transcript is not None:
            base = transcript.with_suffix("")
            agents = [agent for agent in listed if not self._over(base, agent, look)]
            look.transcripts = {k: v for k, v in look.transcripts.items() if k in listed}
            look.over = {k: v for k, v in look.over.items() if k in listed}
            # The hook's marker: the turn is over, and only these agents kept it going.
            look.abandoned = bool(listed) and not agents and (folder / ".background").exists()
        if not agents:
            return None
        types, oldest = {}, float("inf")
        for agent in agents:
            path = folder / agent
            try:
                types[agent] = path.read_text(errors="replace").strip()
                oldest = min(oldest, path.stat().st_mtime)
            except OSError:
                types[agent] = ""
        look.descriptions = {k: v for k, v in look.descriptions.items() if k in types}

        running, started, done, detail = len(agents), 0, 0, None
        if transcript is None:
            return Agents(running)

        if "workflow-subagent" in types.values():
            # The workflows running now: a journal but no final record yet, and written to
            # since the oldest of these agents set off (a run a crash cut short never gets
            # its record).
            runs = base / "subagents" / "workflows"
            live, phase, seen = 0, None, set()
            try:
                names = sorted(n for n in os.listdir(runs) if n.startswith("wf_"))
            except OSError:
                names = []
            for run in names:
                journal = runs / run / "journal.jsonl"
                if (base / "workflows" / f"{run}.json").exists():
                    continue
                try:
                    stat = journal.stat()
                except OSError:
                    continue
                if stat.st_mtime - oldest <= -5:
                    continue
                read = self._read(journal, stat.st_mtime, stat.st_size, look.journals.get(str(journal)))
                look.journals[str(journal)] = read
                seen.add(str(journal))
                live += 1
                started += read.started
                done += read.done
                phase = read.phase or phase
            look.journals = {k: v for k, v in look.journals.items() if k in seen}
            detail = f"{live} workflows" if live > 1 else phase
        elif len(agents) == 1:
            agent = agents[0]
            if agent not in look.descriptions:
                description = ""
                try:
                    meta = json.loads((base / "subagents" / f"agent-{agent}.meta.json").read_bytes())
                    description = meta.get("description") or ""
                except (OSError, ValueError, AttributeError):
                    pass
                look.descriptions[agent] = description
            kind = types.get(agent, "")
            detail = look.descriptions[agent] or (None if kind in AgentWatcher.GENERIC else kind)
        return Agents(running, started, done, detail)

    TAIL = 16 * 1024

    def _over(self, base: Path, agent: str, look: Look) -> bool:
        """Whether an agent the hook still lists has in fact stopped; read again only when
        its transcript or note changes. base is <projects>/<directory>/<session id>/."""
        path = look.transcripts.get(agent)
        if path is None:
            path = base / "subagents" / f"agent-{agent}.jsonl"
            if not path.exists():               # a workflow's agents sit by its run
                path = next(iter((base / "subagents" / "workflows").glob(f"*/agent-{agent}.jsonl")), None)
                if path is None:
                    return False                # nothing written yet
            look.transcripts[agent] = path
        note = path.with_name(f"agent-{agent}.meta.json")
        try:
            stat = path.stat()
            noted = note.stat().st_mtime if note.exists() else 0
        except OSError:
            return False
        key = (stat.st_mtime, stat.st_size, noted)
        known = look.over.get(agent)
        if known and known[0] == key:
            return known[1]
        over = False
        try:
            if noted and json.loads(note.read_bytes()).get("stoppedByUser") is True:
                over = True
            else:
                with open(path, "rb") as f:
                    f.seek(max(0, stat.st_size - AgentWatcher.TAIL))
                    over = cut_short(f.read())
        except (OSError, ValueError, AttributeError):
            pass
        look.over[agent] = (key, over)
        return over

    @staticmethod
    def _read(path: Path, modified: float, size: int, known: Journal | None) -> Journal:
        """A journal's phase (that of the agent started last), and how many agents have
        started and ended, read again only when it has grown. Each line is a record whose
        type comes first; those of agents that ended carry their whole result, so only the
        started ones are parsed."""
        if known and known.modified == modified and known.size == size:
            return known
        journal = AgentWatcher.Journal(modified, size)
        try:
            data = path.read_bytes()
        except OSError:
            return journal
        for line in data.split(b"\n"):
            head = line[:32]
            if head.startswith((b'{"type":"result"', b'{"type":"failed"')):
                journal.done += 1
            elif head.startswith(b'{"type":"started"'):
                journal.started += 1
                try:
                    phase = json.loads(line).get("phase")
                except ValueError:
                    phase = None
                if phase:
                    journal.phase = phase
        return journal


# MARK: The state directory

class LimitHit(NamedTuple):
    """The usage limit a turn last ran into, as the hook noted it in .limit: what Claude Code
    said, and when the limit resets if the status line knew."""
    resets_at: float | None
    message: str            # "You've hit your session limit · resets 7:30pm (Australia/Sydney)"
    noted: float            # when it was hit

    @property
    def key(self) -> tuple | None:
        """Which of the usage limits it is, as usage.Limit.key has it, if that's clear."""
        text = self.message.lower()
        return ("session",) if "session limit" in text else ("week",) if "weekly limit" in text else None

    def holds(self, now: float) -> bool:
        """Until it resets; not knowing when, until the hook sees a turn get through."""
        return self.resets_at is None or now < self.resets_at


def stopped_by_hand(names: Names, watcher: AgentWatcher, sid: str, modified: float) -> bool:
    """Whether a session the hooks last left working, or waiting on me, is in fact sitting
    idle because I stopped it: Esc pressed, or its agents killed, after the hooks last wrote
    (modified, give or take the moment between a hook and the transcript), or left only
    stopped agents to wait for. Claude Code runs no hook for either. Look at the session's
    name first, which reads its transcript."""
    if watcher.activity(sid, names.transcript(sid)) is not None:
        return False                    # agents still at work, so the session is too
    if watcher.abandoned(sid):
        return True
    stopped = names.stopped(sid)
    return stopped is not None and stopped >= modified - 2


def read_limit(directory: Path = STATE_DIR) -> LimitHit | None:
    path = directory / ".limit"
    try:
        noted = path.stat().st_mtime
        resets, _, message = path.read_text(errors="replace").strip().partition(" ")
    except OSError:
        return None
    seconds = int(resets) if resets.isdigit() else 0
    return LimitHit(float(seconds) if seconds else None, message, noted)


class StateReader:
    """Watches one state file per session, and the folder of running agents beside it."""

    STALE_AFTER = 24 * 60 * 60

    @dataclass(frozen=True)
    class Entry:
        state: State
        pid: int
        label: str
        modified: float
        size: int
        limited: bool = False

        @property
        def alive(self) -> bool:
            return system.is_alive(self.pid)

    def __init__(self, directory: Path = STATE_DIR, projects: Path = PROJECTS_DIR):
        self.dir = directory
        self.names = Names(projects)
        self.locator = TmuxLocator()
        self.watcher = AgentWatcher(directory / ".agents")
        self.cache: dict[str, StateReader.Entry] = {}
        self.first_seen: dict[str, float] = {}
        self.last_prune = 0.0
        self.limit: LimitHit | None = None

    def poll(self) -> list[Session]:
        try:
            names = {n for n in os.listdir(self.dir) if not n.startswith(".")}
        except OSError:
            names = set()
        # One timestamp for the whole sweep: sessions found together tie, and the tie-break
        # on name keeps their order stable instead of set-iteration order.
        sweep = time.time()
        for name in names:
            path = self.dir / name
            try:
                stat = path.stat()
            except OSError:
                continue
            known = self.cache.get(name)
            # Stat first: only re-read a file that actually moved.
            if known and known.modified == stat.st_mtime and known.size == stat.st_size:
                continue
            entry = self._read(path, stat.st_mtime, stat.st_size)
            if entry is None:
                self.cache.pop(name, None)
            else:
                self.cache[name] = entry
            self.first_seen.setdefault(name, sweep)
        self.cache = {k: v for k, v in self.cache.items() if k in names}
        self.first_seen = {k: v for k, v in self.first_seen.items() if k in names}

        self._prune()
        self.limit = read_limit(self.dir)
        keep = set(self.cache)
        self.names.forget(keep)
        self.locator.forget(keep)
        self.watcher.forget(keep)

        # Oldest session first, so rows never shuffle under me.
        ordered = sorted(self.cache, key=lambda n: (self.first_seen.get(n, 0), n))
        live = [n for n in ordered if self.cache[n].alive]
        if live:
            return [self._snapshot(n) for n in live]
        # Nothing is running. A finished turn still deserves to be on screen when I come
        # back; a dead session's "working" means only that it was killed.
        return [self._snapshot(n) for n in ordered if self.cache[n].state is State.DONE]

    def _snapshot(self, name: str) -> Session:
        entry = self.cache[name]
        state, limited = entry.state, entry.limited
        busy = state in (State.WORKING, State.WAITING) and not limited
        title, path = self.names.info(name, busy=busy)
        if limited and not (self.limit and self.limit.holds(time.time())):
            state, limited = State.DONE, False      # the limit is over; the session just sits
        if busy and stopped_by_hand(self.names, self.watcher, name, entry.modified):
            state = State.DONE
        return Session(id=name, state=state, label=entry.label, pid=entry.pid,
                       title=title, path=path,
                       tmux=self.locator.location(name, entry.pid),
                       agents=self.watcher.activity(name, self.names.transcript(name)),
                       limited=limited)

    @staticmethod
    def _read(path: Path, modified: float, size: int) -> StateReader.Entry | None:
        try:
            fields = path.read_text(errors="replace").split()
        except OSError:
            return None
        if not fields:
            return None
        # "<word> <pid> <project name>": the name may itself contain spaces.
        pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
        limited = fields[0] == "limited"
        state = State.WAITING if limited else State.parse(fields[0])
        return StateReader.Entry(state, pid, " ".join(fields[2:]), modified, size, limited)

    def _prune(self) -> None:
        """Sessions killed without a SessionEnd hook leave their file behind, and any
        agents' folder."""
        now = time.time()
        if now - self.last_prune < 60:
            return
        self.last_prune = now
        for name, entry in list(self.cache.items()):
            if not entry.alive and now - entry.modified > StateReader.STALE_AFTER:
                try:
                    (self.dir / name).unlink()
                except OSError:
                    pass
                self.cache.pop(name, None)
                self.first_seen.pop(name, None)
        agents = self.dir / ".agents"
        try:
            folders = os.listdir(agents)
        except OSError:
            return
        for name in folders:
            if name in self.cache:
                continue
            folder = agents / name
            try:
                if now - folder.stat().st_mtime > StateReader.STALE_AFTER:
                    for item in folder.iterdir():
                        item.unlink()
                    folder.rmdir()
            except OSError:
                pass
