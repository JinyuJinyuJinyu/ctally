"""Snapshots of tmux and the Claude Code sessions in it, to bring them back after a restart.

While it's switched on, CTally notes every half hour (or as often as it's set to, up to every
eight hours) each tmux session: its windows, their layouts and sizes, and each pane's folder
and what Claude Code runs there, the session and the options it was started with, or the
background job the pane shows. A snapshot the same as the last isn't saved again. They go
after three days (or as many as they're set to be kept), except the newest from before the
last restart: that's the one to bring back, however long the computer was off.

Restoring makes each tmux session of a snapshot that isn't running now, lays out its windows
and panes as they were, and in each pane resumes its Claude Code session with the options it
had (`claude --resume <id> ...`), or attaches to its background job again (`claude attach
<job>`). A Claude Code session is never started in the wrong folder: one whose folder isn't
there yet, on a disk still being mounted, is waited for a few minutes by its tmux session (the
others are made at once), and if it never comes, or has been deleted, the session is typed
into its pane and not started. Which of the two is told by what's mounted: see _may_come(). A
plain shell whose folder is gone opens in the nearest one above it. Until something
has been restored since the computer started, the snapshot to restore is the newest from
before then: the snapshots taken while it came up are of nothing much.

The snapshots are ccsnap's, in the same place and the same form, so either reads the other's:
~/.local/state/ccsnap/snapshots (~/.ccsnap/snapshots on macOS), or $CCSNAP_HOME/snapshots.
Switching CTally's on switches off ccsnap's own timer, which would only do the same again.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

from . import system

EVERY = 30 * 60             # seconds between snapshots, unless set otherwise
KEEP_FOR = 3 * 86400        # how far back they go, unless set otherwise
MINUTES = (30, 8 * 60)      # what the time between them can be set to, in minutes: half an hour to eight
DAYS = (1, 30)              # and how long they're kept, in days
WAIT = 300                  # how long a restore waits for a folder a Claude Code session needs
MOUNTS = ("/Volumes", "/media", "/run/media", "/mnt")       # where disks are put, on macOS and Linux
FSTAB = Path("/etc/fstab")  # and where each of the computer's own goes

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def minutes(value) -> int:
    """The time between snapshots, in minutes, from a setting: what's stored is a file anyone
    can edit, so anything else is the default and too much or too little the nearest allowed."""
    return _within(value, MINUTES, EVERY // 60)


def days(value) -> int:
    """How many days snapshots are kept, from a setting, as for minutes()."""
    return _within(value, DAYS, KEEP_FOR // 86400)


def _within(value, bounds: tuple[int, int], default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return int(min(max(value, bounds[0]), bounds[1]))


def home() -> Path:
    if os.environ.get("CCSNAP_HOME"):
        return Path(os.environ["CCSNAP_HOME"])
    if system.MAC:
        return Path.home() / ".ccsnap"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ccsnap"


# MARK: tmux

class Tmux:
    """The tmux server to note and to restore into: the default one, or `tmux -L <name>`."""

    def __init__(self, socket: str | None = None):
        self.socket = socket or os.environ.get("CCSNAP_TMUX_SOCKET") or None
        self.binary = system.tmux_binary() or "tmux"

    def __call__(self, *args, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess:
        # -u: UTF-8 whatever the locale, so names with accents come out as they are.
        command = [self.binary, "-u"] + (["-L", self.socket] if self.socket else []) + [str(a) for a in args]
        try:
            done = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            done = subprocess.CompletedProcess(command, 127, "", f"can't run tmux: {error}")
        if check and done.returncode != 0:
            raise RuntimeError(f"`{shlex.join(command)}` failed: {done.stderr.strip()}")
        return done

    def rows(self, args: list[str], fields: list[str], timeout: float = 10) -> list[dict] | None:
        """A list command's rows, or None with no server running."""
        done = self(*args, "-F", "\t".join("#{%s}" % f for f in fields), check=False, timeout=timeout)
        if done.returncode != 0:
            return None
        return [dict(zip(fields, line.split("\t"))) for line in done.stdout.splitlines()]

    def sessions(self, timeout: float = 10) -> set[str]:
        return {row["session_name"] for row in self.rows(["list-sessions"], ["session_name"], timeout) or []}


# MARK: What runs where

def _processes() -> dict[int, tuple[int, list[str]]]:
    """Every process: its parent and its command line."""
    found = {}
    if system.LINUX:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    parent = int(f.read().rsplit(b")", 1)[1].split()[1])
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    raw = f.read()
            except (OSError, IndexError, ValueError):
                continue
            found[int(name)] = (parent, [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")])
        return found
    for line in (system.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "args="]) or "").splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[0].isdigit() and parts[1].isdigit():
            found[int(parts[0])] = (int(parts[1]), parts[2:])
    return found


def _running(registry: Path) -> dict[int, dict]:
    """pid -> Claude Code's record of the session that process runs, ~/.claude/sessions/<pid>.json,
    for processes still running (and not another given the same pid since)."""
    found = {}
    for path in registry.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            pid = int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(record.get("sessionId"), str) or not system.is_alive(pid) or pid <= 0:
            continue
        began, now = record.get("procStart"), system.started(pid)
        if began is not None and now is not None and str(began) != now:
            continue
        found[pid] = record
    return found


def _below(pid: int, children: dict[int, list[int]], hit: Callable[[int], bool]) -> int | None:
    """The first process at or below a pane's shell that hit() picks, nearest first."""
    queue = [pid]
    while queue:
        pid = queue.pop(0)
        if hit(pid):
            return pid
        queue.extend(children.get(pid, []))
    return None


# Options not to start a resumed session with: they pick or make a session, or run just once.
DROP = {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session", "-n", "--name",
        "-p", "--print", "--bg", "--background", "--from-pr", "--teleport", "--cloud",
        "-w", "--worktree", "--tmux", "--desktop", "-h", "--help", "-v", "--version"}
# Claude Code's options, as `claude --help` lists them (2.1.326) and a few it doesn't: those
# that take a value, those that take as many as follow, those that may take one, and those
# that take none.
VALUE = {"--model", "--permission-mode", "--effort", "--agent", "--settings", "--setting-sources",
         "--append-system-prompt", "--system-prompt", "--system-prompt-file",
         "--append-system-prompt-file", "--system-prompt-snapshot", "--fallback-model",
         "--output-format", "--input-format", "--max-turns", "--max-budget-usd", "--session-id",
         "-n", "--name", "--json-schema", "--agents", "--permission-prompt-tool",
         "--permission-prompts", "--plugin-dir", "--plugin-url", "--environment", "--autocompact",
         "--debug-file", "--remote-control-session-name-prefix", "--max-thinking-tokens", "--thinking"}
VARIADIC = {"--add-dir", "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools",
            "--disallowed-tools", "--betas", "--tools", "--file", "--channels",
            "--dangerously-load-development-channels"}
OPTIONAL = {"-r", "--resume", "-d", "--debug", "--remote-control", "-w", "--worktree",
            "--from-pr", "--teleport", "--cloud", "--prompt-suggestions"}
FLAGS = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--ax-screen-reader",
         "--bare", "--brief", "--chrome", "--no-chrome", "--disable-slash-commands",
         "--exclude-dynamic-system-prompt-sections", "--forward-subagent-text", "--ide",
         "--include-hook-events", "--include-partial-messages", "--no-session-persistence",
         "--replay-user-messages", "--restricted", "--safe-mode", "--strict-mcp-config", "--verbose",
         "-c", "--continue", "--fork-session", "-p", "--print", "--bg", "--background", "--tmux",
         "--desktop", "-h", "--help", "-v", "--version"}


def options(arguments: list[str]) -> list[str]:
    """The options a session was started with that it should be resumed with too
    (--dangerously-skip-permissions, --model X): not those that pick a session, nor a prompt,
    which would be sent again."""
    out, i = [], 0
    while i < len(arguments):
        argument = arguments[i]
        if argument == "--":
            break
        if not argument.startswith("-") or argument == "-":
            i += 1
            continue
        name = argument.split("=", 1)[0]
        values, keep = [], name not in DROP
        if not (argument.startswith("--") and "=" in argument):
            after = arguments[i + 1] if i + 1 < len(arguments) else None
            if name in VARIADIC:
                j = i + 1
                while j < len(arguments) and not arguments[j].startswith("-"):
                    values.append(arguments[j])
                    j += 1
            elif name in VALUE:
                values += [after] if after is not None else []
            elif name in OPTIONAL:
                values += [after] if after is not None and not after.startswith("-") else []
            elif name not in FLAGS and after is not None and not after.startswith("-"):
                # One this doesn't know (Claude Code gains them), with something after it:
                # what it takes, or a prompt, and there's no telling which. Kept without it,
                # the option would take the next one for its own; kept with it, a prompt would
                # be sent again. So neither is kept.
                values, keep = [after], False
        i += 1 + len(values)
        if keep:
            out += [argument] + values
    return out


class Places(NamedTuple):
    """Where Claude Code keeps what a snapshot needs: its list of running sessions, the
    background jobs, and the transcripts."""
    registry: Path = CLAUDE_DIR / "sessions"
    jobs: Path = CLAUDE_DIR / "jobs"
    projects: Path = CLAUDE_DIR / "projects"

    def has_transcript(self, sid: str | None) -> bool:
        return bool(sid) and any(self.projects.glob(f"*/{sid}.jsonl"))

    def job(self, job: str) -> dict:
        try:
            found = json.loads((self.jobs / job / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def find_job(self, named: str) -> tuple[str, dict]:
        """What `claude attach` was given, its id or part of its name, as a job and its state."""
        if (self.jobs / named).is_dir():
            return named, self.job(named)
        for path in sorted(self.jobs.glob("*/state.json")):
            state = self.job(path.parent.name)
            if named.lower() in str(state.get("name") or "").lower():
                return path.parent.name, state
        return named, {}


def capture(tmux: Tmux, places: Places = Places()) -> list[dict] | None:
    """Every tmux session as it is now, or None with no tmux running."""
    windows = tmux.rows(["list-windows", "-a"],
                        ["session_name", "window_index", "window_name", "window_layout",
                         "window_active", "window_width", "window_height", "automatic-rename"])
    panes = tmux.rows(["list-panes", "-a"],
                      ["session_name", "window_index", "pane_index", "pane_pid",
                       "pane_current_path", "pane_current_command", "pane_active"])
    names = tmux.rows(["list-sessions"], ["session_name"])
    if windows is None or panes is None or names is None:
        return None

    processes, running = _processes(), _running(places.registry)
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in processes.items():
        children.setdefault(parent, []).append(pid)

    def attaching(pid: int) -> bool:
        arguments = system.claude_arguments(processes[pid][1]) if pid in processes else None
        return bool(arguments) and len(arguments) > 1 and arguments[0] == "attach"

    def given(pid: int) -> list[str]:
        """What Claude Code was started with there, each argument whole: `ps` gives one with
        spaces in it (a system prompt, a folder) in pieces."""
        pieces = processes.get(pid, (0, []))[1]
        return system.claude_arguments(system.arguments(pid) or pieces) or system.claude_arguments(pieces) or []

    sessions = {row["session_name"]: {"name": row["session_name"], "windows": {}} for row in names}
    for w in windows:
        sessions.setdefault(w["session_name"], {"name": w["session_name"], "windows": {}})["windows"][w["window_index"]] = {
            "index": int(w["window_index"]), "name": w["window_name"], "layout": w["window_layout"],
            "active": w["window_active"] == "1", "width": int(w["window_width"]), "height": int(w["window_height"]),
            "auto_rename": w["automatic-rename"] == "1", "panes": []}
    for p in panes:
        pane = {"index": int(p["pane_index"]), "cwd": p["pane_current_path"] or str(Path.home()),
                "active": p["pane_active"] == "1", "command": p["pane_current_command"]}
        shell = int(p["pane_pid"]) if p["pane_pid"].isdigit() else 0
        pid = _below(shell, children, lambda q: q in running and running[q].get("kind", "interactive") == "interactive")
        if pid:
            record = running[pid]
            sid = record["sessionId"]
            claude = {"session_id": sid, "name": record.get("name"), "name_source": record.get("nameSource"),
                      "cwd": record.get("cwd") or pane["cwd"],
                      "args": options(given(pid)),
                      "had_transcript": places.has_transcript(sid)}
            if record.get("parkedJobId"):
                job = places.job(record["parkedJobId"])
                claude.update(job_id=record["parkedJobId"],
                              job_session_id=job.get("resumeSessionId") or job.get("sessionId"),
                              job_name=job.get("name"))
            pane["claude"] = claude
        elif (pid := _below(shell, children, attaching)):
            named = given(pid)
            job_id, job = places.find_job((named if len(named) > 1 else system.claude_arguments(processes[pid][1]))[1])
            pane["claude"] = {"job_id": job_id, "job_name": job.get("name"),
                              "job_session_id": job.get("resumeSessionId") or job.get("sessionId"),
                              "session_id": None, "name": job.get("name"),
                              "cwd": job.get("cwd") or pane["cwd"], "args": [], "attach": True}
        window = sessions.get(p["session_name"], {}).get("windows", {}).get(p["window_index"])
        if window is not None:
            window["panes"].append(pane)

    out = []
    for session in sessions.values():
        ordered = sorted(session["windows"].values(), key=lambda w: w["index"])
        for window in ordered:
            window["panes"].sort(key=lambda pane: pane["index"])
        out.append({"name": session["name"], "windows": ordered})
    return out


def counts(sessions: list[dict]) -> tuple[int, int]:
    """Panes, and the Claude Code sessions in them."""
    panes = [p for s in sessions for w in s["windows"] for p in w["panes"]]
    return len(panes), sum(1 for p in panes if "claude" in p)


# MARK: Keeping them

def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return str(int(system.boot_time()))        # none on macOS: when it started says which boot


def same_boot(marked: str, now: str) -> bool:
    """Whether a boot noted earlier is this one. Where it's told by when the computer started,
    that moves a little each time the clock is put right, so a minute either way is the same."""
    if marked == now:
        return True
    return marked.isdigit() and now.isdigit() and abs(int(marked) - int(now)) <= 60


class Snapshots:
    """The snapshot files: <when>.json, {"version": 1, "saved_at": <Unix time>, "sessions": [...]}."""

    def __init__(self, folder: Path | None = None, keep_for: float = KEEP_FOR):
        self.home = folder or home()
        self.keep_for = keep_for            # seconds
        self.dir = self.home / "snapshots"
        self.marker = self.home / "restored-boot"
        self._loaded: dict[str, tuple[float, dict | None]] = {}

    def load(self, path: Path) -> dict | None:
        """A snapshot, or None for a file that isn't one (the first ccsnap wrote another kind)."""
        try:
            modified = path.stat().st_mtime
        except OSError:
            return None
        known = self._loaded.get(path.name)
        if known and known[0] == modified:
            return known[1]
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        good = data if isinstance(data, dict) and isinstance(data.get("sessions"), list) \
            and isinstance(data.get("saved_at"), (int, float)) else None
        self._loaded[path.name] = (modified, good)
        return good

    def entries(self) -> list[tuple[Path, dict]]:
        """The snapshots and what's in them, newest first. Read in one go, so one deleted
        meanwhile is simply missing."""
        found = []
        try:
            for path in self.dir.glob("*.json"):
                data = self.load(path)
                if data:
                    found.append((data["saved_at"], path.name, path, data))
        except OSError:
            return []
        return [(path, data) for _, _, path, data in sorted(found, key=lambda e: e[:2], reverse=True)]

    def paths(self) -> list[Path]:
        return [path for path, _ in self.entries()]

    def save(self, sessions: list[dict], now: float | None = None) -> Path | None:
        """Writes a snapshot, unless it's the same as the last. Returns it, or None."""
        now = time.time() if now is None else now
        newest = self.entries()
        if newest and newest[0][1]["sessions"] == sessions:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        path, n = self.dir / f"{stem}.json", 2
        while path.exists():                # never over another: two in a second, or a stranger's
            path, n = self.dir / f"{stem}-{n}.json", n + 1
        temp = path.with_name(f".{path.name}.new")
        temp.write_text(json.dumps({"version": 1, "saved_at": now, "sessions": sessions}, indent=1))
        os.replace(temp, path)
        return path

    def prune(self, now: float | None = None, boot: float | None = None) -> list[Path]:
        """Deletes the snapshots older than they're kept for (three days, unless set otherwise),
        but never the newest from before the computer last started. Returns those deleted."""
        now = time.time() if now is None else now
        boot = system.boot_time() if boot is None else boot
        entries = self.entries()
        keep = next((path for path, data in entries if data["saved_at"] < boot), None)
        gone = []
        for path, data in entries:
            if path != keep and data["saved_at"] < now - self.keep_for:
                try:
                    path.unlink()
                    gone.append(path)
                except OSError:
                    pass
                self._loaded.pop(path.name, None)
        return gone

    def restored_this_boot(self) -> bool:
        try:
            return same_boot(self.marker.read_text().strip(), boot_id())
        except OSError:
            return False

    def pick(self, running: set[str], boot: float | None = None) -> Path | None:
        """The snapshot to restore: until a restore since the computer started, the newest from
        before then; after it, the newest. Of those, the newest with a tmux session not running
        now, else the newest."""
        entries = self.entries()
        if not self.restored_this_boot():
            boot = system.boot_time() if boot is None else boot
            entries = [(path, data) for path, data in entries if data["saved_at"] < boot] or entries
        for path, data in entries:
            if any(s["name"] not in running for s in data["sessions"]):
                return path
        return entries[0][0] if entries else None


# MARK: Bringing them back

def launch(pane: dict, places: Places = Places(), claude: str = "claude") -> tuple[str | None, str]:
    """What to type into a restored pane, and a word on what it is: None for a plain shell."""
    return _launch(pane, places, claude)[:2]


def _launch(pane: dict, places: Places, claude: str) -> tuple[str | None, str, bool]:
    """launch(), and whether what it starts has to be in its own folder: a session is resumed
    where it was, while a background job can be looked at from anywhere."""
    c = pane.get("claude")
    if not c:
        return None, "", False
    if c.get("job_id"):
        if (places.jobs / c["job_id"]).is_dir():
            return (f"{claude} attach {shlex.quote(c['job_id'])}", f"background job {c.get('job_name') or ''}".strip(),
                    False)
        sid = c.get("job_session_id") or c.get("session_id")
        if not sid:                 # a job it can't place: let `claude attach` make what it can of it
            return f"{claude} attach {shlex.quote(c['job_id'])}", "background job", False
    else:
        sid = c.get("session_id")
    parts, note = [claude], c.get("name") or ""
    if places.has_transcript(sid):
        parts += ["--resume", shlex.quote(sid)]
    else:
        note = f"{note} (nothing said yet: a new session)".strip()
        if c.get("name") and c.get("name_source") == "user":
            parts += ["-n", shlex.quote(c["name"])]     # a resumed one has its name in its transcript
    parts += [shlex.quote(x) for x in c.get("args", [])]
    return " ".join(parts), note, True


class _Pane(NamedTuple):
    """How a pane of a snapshot comes back."""
    folder: str             # where it opens: its own folder, or the nearest above it that's there
    command: str | None     # what to type into it: None for a plain shell
    start: bool             # whether to press Return after it
    note: str               # a word on what it is: "cb5", "background job cb7"
    why: str                # why it isn't started, or where it opens instead
    needs: str | None       # its folder, if Claude Code can't start anywhere else
    plain: str | None       # what's typed when that folder is there

    @property
    def label(self) -> str:
        return "  ".join(part for part in (self.note, self.why) if part)


def _nearest(folder: str) -> str:
    """A folder, or the nearest above it that's there, home rather than the top of the disk:
    where a pane opens when its own is gone."""
    if os.path.isdir(folder):
        return folder
    parent = os.path.dirname(os.path.normpath(folder))
    while parent and parent != os.path.dirname(parent):
        if os.path.isdir(parent):
            return parent
        parent = os.path.dirname(parent)
    return str(Path.home())


def _listed_disks() -> list[str]:
    """Where /etc/fstab says the computer's disks go. One still being checked or mounted as
    the computer comes up isn't on its place yet."""
    try:
        lines = FSTAB.read_text(errors="replace").splitlines()
    except OSError:
        return []
    places = []
    for line in lines:
        parts = line.split()
        if len(parts) > 1 and not parts[0].startswith("#") and parts[1].startswith("/"):
            # A space in a name is written \040, and so on.
            place = os.path.normpath(re.sub(r"\\([0-7]{3})", lambda found: chr(int(found.group(1), 8)), parts[1]))
            if place != "/":
                places.append(place)
    return places


def _disk_not_on(folder: str) -> bool:
    """Whether a folder is where one of the computer's disks goes, or on it, and the disk
    isn't there yet. What's there meanwhile, an empty folder as a rule, isn't the folder meant."""
    path = os.path.normpath(folder)
    return any((path == place or path.startswith(place + "/")) and not os.path.ismount(place)
               for place in _listed_disks())


def _may_come(folder: str) -> bool:
    """Whether a folder that isn't there may yet be, on a disk still being mounted, rather than
    deleted: one on a disk of the computer's that isn't on its place, or under where disks are
    put (/Volumes, /media, /mnt) with no disk on the way down to it. No folder is looked into
    for this: only what's mounted is asked."""
    path = os.path.normpath(folder)
    if _disk_not_on(path):
        return True
    for root in MOUNTS:
        if path.startswith(root + "/"):
            parent = os.path.dirname(path)
            while len(parent) > len(root):
                if os.path.ismount(parent):
                    return False        # its disk is there, and the folder isn't on it
                parent = os.path.dirname(parent)
            return True
    return False


def _awaited(folder: str) -> bool:
    """Whether a folder a Claude Code session needs isn't ready for it, and may yet be."""
    return _disk_not_on(folder) if os.path.isdir(folder) else _may_come(folder)


def _plan(pane: dict, places: Places, claude: str, live: frozenset | set = frozenset()) -> _Pane:
    """Never Claude Code in the wrong folder: a session is resumed where it was, so with its
    folder gone, or its disk not on it yet, it's typed into its pane, after a cd there, and
    not started; one that may yet come is worth waiting for first. A plain shell can open
    anywhere, and so can a look at a background job."""
    wanted = pane["claude"]["cwd"] if "claude" in pane else pane["cwd"]
    there = os.path.isdir(wanted)
    folder = wanted if there else _nearest(wanted)
    command, note, needs = _launch(pane, places, claude)
    if command is None:
        return _Pane(folder, None, False, "", "" if there else f"(no folder {wanted}: opens in {folder})", None, None)
    if needs and not (there and not _disk_not_on(wanted)):
        why = f"(no folder {wanted}: not started)" if not there else f"(no disk on {wanted} yet: not started)"
        return _Pane(folder, f"cd {shlex.quote(wanted)} && {command}", False, note, why, wanted, command)
    if not pane["claude"].get("job_id") and pane["claude"].get("session_id") in live:
        # A session already running elsewhere is typed but not started: two of one is trouble.
        return _Pane(folder, command, False, note, "(running elsewhere: not started)", wanted if needs else None, command)
    return _Pane(folder, command, True, note, "", wanted if needs else None, command)


class Restored(NamedTuple):
    made: list[str]                 # the tmux sessions made
    started: list[str]              # the Claude Code sessions started: "cb:0.1  cb5"
    skipped: list[str]              # why each one left out was


def restore(store: Snapshots, tmux: Tmux, path: Path | None = None, places: Places = Places(),
            wait: float = WAIT, claude: str = "claude", settle: float = 1.0, stagger: float = 0.4,
            say: Callable[[str], None] = lambda _: None, dry_run: bool = False) -> Restored:
    """Makes the snapshot's tmux sessions that aren't running now, and starts Claude Code in
    their panes. The snapshot is pick()'s unless given."""
    running = tmux.sessions()
    path = path or store.pick(running)
    snapshot = store.load(path) if path else None
    if snapshot is None:
        return Restored([], [], ["no snapshot to restore"])
    say(f"snapshot {path.name}, saved {datetime.fromtimestamp(snapshot['saved_at']):%Y-%m-%d %H:%M:%S}")
    skipped = [f"{s['name']}: a tmux session of that name is running" for s in snapshot["sessions"]
               if s["name"] in running]
    todo = [s for s in snapshot["sessions"] if s["name"] not in running]

    if dry_run:
        for s in todo:
            for w in s["windows"]:
                for p in w["panes"]:
                    plan = _plan(p, places, claude)
                    shown, why = plan.command, plan.why
                    if plan.needs and not plan.start and _awaited(plan.needs):
                        # One waited for is started as ever if it comes.
                        what = "disk on" if os.path.isdir(plan.needs) else "folder"
                        shown, why = plan.plain, f"(no {what} {plan.needs} yet: waited for)"
                    say("  ".join(part for part in (f"{s['name']}:{w['index']}.{p['index']}", shown or "shell",
                                                    plan.note, why) if part))
        return Restored([s["name"] for s in todo], [], skipped)

    # The folders each tmux session's Claude Code sessions can't start without, worked out once.
    needed: dict[str, set[str]] = {}
    for s in todo:
        try:
            needed[s["name"]] = {plan.needs for w in s["windows"] for p in w["panes"]
                                 if (plan := _plan(p, places, claude)).needs}
        except Exception:               # not as a snapshot should be: making it says what's wrong
            needed[s["name"]] = set()

    def awaited(s: dict) -> set[str]:
        """Those of them that aren't ready, and may yet be."""
        return {folder for folder in needed[s["name"]] if _awaited(folder)}

    made, started = [], []

    def bring(sessions: list[dict], again: bool = False) -> None:
        """Makes these tmux sessions, and starts Claude Code in their panes."""
        if again and sessions:
            # One started by hand while this waited is left as it is.
            now = tmux.sessions()
            skipped.extend(f"{s['name']}: a tmux session of that name is running" for s in sessions if s["name"] in now)
            sessions = [s for s in sessions if s["name"] not in now]
        if not sessions:
            return
        live = {record.get("sessionId") for record in _running(places.registry).values()}
        launches = []       # (pane id, command, whether to press Enter, label)
        for s in sessions:
            try:
                launches += _make(tmux, s, places, claude, live, say)
            except Exception as error:      # one that can't be made is no reason to leave the others
                skipped.append(f"{s['name']}: couldn't be made ({error})")
                continue
            made.append(s["name"])
            say(f"made {s['name']}: {len(s['windows'])} window(s), {sum(len(w['panes']) for w in s['windows'])} pane(s)")
        if launches:
            time.sleep(settle)          # the new shells start before anything is typed into them
            for pane, command, enter, label in launches:
                # A pane closed since is no reason not to start the rest.
                if tmux("send-keys", "-t", pane, "-l", command, check=False).returncode != 0:
                    say(f"couldn't type into {label}")
                    continue
                if enter:
                    tmux("send-keys", "-t", pane, "Enter", check=False)
                    started.append(label)
                    time.sleep(stagger)
                say(("started " if enter else "typed ") + label)

    # A tmux session with a Claude Code session whose folder isn't there yet (a disk still
    # being mounted) waits for it. The rest are made at once, those with one whose folder is
    # gone for good among them.
    deadline, told = time.monotonic() + wait, False
    late = [s for s in todo if awaited(s)]
    waiting = {s["name"] for s in late}
    bring([s for s in todo if s["name"] not in waiting])
    while late:
        here = [s for s in late if not awaited(s)]
        if here:
            names = {s["name"] for s in here}
            late = [s for s in late if s["name"] not in names]
            bring(here, again=True)
            continue
        if time.monotonic() >= deadline:
            break
        if not told:
            gone = sorted(set().union(*(awaited(s) for s in late)))
            if gone:
                say(f"waiting up to {int(wait)} s for {len(gone)} folder(s): {', '.join(gone[:3])}")
                told = True
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    # One whose folder never came is made all the same, that session typed and not started.
    bring(late, again=True)
    if made:
        try:
            store.home.mkdir(parents=True, exist_ok=True)
            store.marker.write_text(boot_id())
        except OSError:
            pass
    return Restored(made, started, skipped)


def _make(tmux: Tmux, s: dict, places: Places, claude: str, live: set,
          say: Callable[[str], None] = lambda _: None) -> list[tuple[str, str, bool, str]]:
    """Makes one tmux session as a snapshot has it, and returns what to type into its panes.
    One that can't be finished isn't left half-made, so a later restore can make it whole."""
    windows = [w for w in s["windows"] if w["panes"]]       # one caught as it closed has none
    if not windows:
        raise RuntimeError("no window with a pane in it")
    planned = [[_plan(p, places, claude, live) for p in w["panes"]] for w in windows]
    launches, notes, chosen, fmt = [], [], None, "#{window_id}\t#{pane_id}"

    w = windows[0]
    out = tmux("new-session", "-d", "-P", "-F", fmt, "-s", s["name"], "-n", w["name"],
               "-c", planned[0][0].folder, "-x", w["width"], "-y", w["height"]).stdout
    try:
        for n, (w, plans) in enumerate(zip(windows, planned)):
            if n == 0:
                window, first = out.strip().split("\t")
                if tmux("display", "-p", "-t", window, "#{window_index}").stdout.strip() != str(w["index"]):
                    tmux("move-window", "-s", window, "-t", f"={s['name']}:{w['index']}")
            else:
                out = tmux("new-window", "-d", "-P", "-F", fmt, "-t", f"={s['name']}:{w['index']}",
                           "-n", w["name"], "-c", plans[0].folder).stdout
                window, first = out.strip().split("\t")
            tmux("resize-window", "-t", window, "-x", w["width"], "-y", w["height"], check=False)
            made, last = [first], first
            for plan in plans[1:]:
                # Split the last pane each time, so the panes come in the order they were.
                last = tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", last, "-c", plan.folder).stdout.strip()
                made.append(last)
                tmux("select-layout", "-t", window, "tiled", check=False)
            tmux("select-layout", "-t", window, w["layout"], check=False)
            tmux("set-option", "-w", "-u", "-t", window, "window-size", check=False)
            if w.get("auto_rename"):
                tmux("set-option", "-w", "-t", window, "automatic-rename", "on", check=False)
            for p, plan, pane in zip(w["panes"], plans, made):
                if p.get("active"):
                    tmux("select-pane", "-t", pane, check=False)
                label = f"{s['name']}:{w['index']}.{p['index']}  {plan.label}".rstrip()
                if plan.command:
                    launches.append((pane, plan.command, plan.start, label))
                elif plan.label:
                    notes.append(label)
            if w.get("active"):
                chosen = window
        if chosen:
            tmux("select-window", "-t", chosen, check=False)
    except Exception:
        tmux("kill-session", "-t", f"={s['name']}", check=False)
        raise
    for note in notes:
        say(note)
    return launches


def saved_when(moment: float, now: float) -> str:
    """"10:14 pm" today, "Mon 10:14 pm" within the week, "Nov 9" before."""
    from .usage import clock
    then, today = time.localtime(moment), time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return clock(moment)
    if now - moment < 6 * 86400:
        return f"{time.strftime('%a', then)} {clock(moment)}"
    return time.strftime("%b %-d", then)


class Keeper:
    """For the app: a snapshot when asked (every half hour, or as set, while switched
    on), and a restore, each off the main thread and one at a time. What they do goes to the log."""

    def __init__(self, store: Snapshots | None = None, tmux: Tmux | None = None, places: Places = Places(),
                 log: Callable[[str], None] | None = None):
        self.store = store or Snapshots()
        self.tmux = tmux or Tmux()
        self.places = places
        self.log = log or (lambda message: print(f"ctally snapshots: {message}", file=sys.stderr, flush=True))
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctally-snapshots")
        self.saving = False
        self.restoring = False

    def save_soon(self) -> None:
        if not self.saving:
            self.saving = True
            self.pool.submit(self._save)

    def _save(self) -> None:
        try:
            sessions = capture(self.tmux, self.places)
            if sessions is not None and self.store.save(sessions) is not None:
                self.store.prune()
        except Exception as error:      # one going wrong is no reason to stop the next
            self.log(f"couldn't take one: {error}")
        finally:
            self.saving = False

    def take_over(self) -> None:
        """Switches off ccsnap's own timer, in the background, and says so if it did."""
        def stop() -> None:
            stopped = stop_ccsnap()
            if stopped:
                self.log(f"switched off {stopped}: CTally takes the snapshots now")
        self.pool.submit(stop)

    def restorable(self) -> tuple[Path, float] | None:
        """The snapshot a restore would bring back, and when it was saved, if it has a tmux
        session that isn't running now."""
        if self.restoring or not self.store.entries():
            return None                 # and with none, tmux isn't even asked
        # The menu waits on this, so not for long.
        running = self.tmux.sessions(timeout=3)
        path = self.store.pick(running)
        data = self.store.load(path) if path else None
        if data is None or all(s["name"] in running for s in data["sessions"]):
            return None
        return path, data["saved_at"]

    def restore_soon(self, path: Path | None = None) -> None:
        if not self.restoring:
            self.restoring = True
            self.pool.submit(self._restore, path)

    def _restore(self, path: Path | None) -> None:
        try:
            done = restore(self.store, self.tmux, path, self.places, say=self.log)
            for why in done.skipped:
                self.log(f"left out {why}")
        except Exception as error:
            self.log(f"restore stopped: {error}")
        finally:
            self.restoring = False


# MARK: ccsnap's own timer

LAUNCHD_LABEL = "com.ccsnap.autosave"


def stop_ccsnap(run: Callable[[list[str]], str | None] = system.run) -> str | None:
    """Switches off ccsnap's own timer, if it has one on: CTally's snapshots are the same
    thing. Its files stay as they are, for `ccsnap auto on` to take up again. Says what it
    switched off."""
    if system.MAC:
        if not (Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").exists():
            return None
        target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        run(["launchctl", "disable", target])       # stays off at the next login too
        run(["launchctl", "bootout", target])
        return f"ccsnap's launchd agent ({LAUNCHD_LABEL})"
    if not shutil.which("systemctl"):
        return None
    on = run(["systemctl", "--user", "is-enabled", "ccsnap.timer"]) is not None or \
        run(["systemctl", "--user", "is-active", "ccsnap.timer"]) is not None
    if not on:
        return None
    run(["systemctl", "--user", "disable", "--now", "ccsnap.timer"])
    return "ccsnap's systemd timer (ccsnap.timer)"
