"""Snapshots of tmux and the Claude Code sessions in it, to bring them back after a restart.

While it's switched on, CTally notes every couple of minutes each tmux session: its windows,
their layouts and sizes, and each pane's folder and what Claude Code runs there, the session
and the options it was started with, or the background job the pane shows. A snapshot the same
as the last isn't saved again. They go after three days, except the newest from before the
last restart: that's the one to bring back, however long the computer was off.

Restoring makes each tmux session of a snapshot that isn't running now, lays out its windows
and panes as they were, and in each pane resumes its Claude Code session with the options it
had (`claude --resume <id> ...`), or attaches to its background job again (`claude attach
<job>`). A folder that isn't there yet, on a disk still being mounted, is waited for a few
minutes; a session whose folders never come is left out. Until something has been restored
since the computer started, the snapshot to restore is the newest from before then: the
snapshots taken while it came up are of nothing much.

The snapshots are ccsnap's, in the same place and the same form, so either reads the other's:
~/.local/state/ccsnap/snapshots (~/.ccsnap/snapshots on macOS), or $CCSNAP_HOME/snapshots.
Switching CTally's on switches off ccsnap's own timer, which would only do the same again.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

from . import system

EVERY = 120                 # seconds between snapshots
KEEP_FOR = 3 * 86400        # how far back they go
WAIT = 300                  # how long a restore waits for a missing folder

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


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

    def __call__(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        # -u: UTF-8 whatever the locale, so names with accents come out as they are.
        command = [self.binary, "-u"] + (["-L", self.socket] if self.socket else []) + [str(a) for a in args]
        try:
            done = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            done = subprocess.CompletedProcess(command, 127, "", f"can't run tmux: {error}")
        if check and done.returncode != 0:
            raise RuntimeError(f"`{shlex.join(command)}` failed: {done.stderr.strip()}")
        return done

    def rows(self, args: list[str], fields: list[str]) -> list[dict] | None:
        """A list command's rows, or None with no server running."""
        done = self(*args, "-F", "\t".join("#{%s}" % f for f in fields), check=False)
        if done.returncode != 0:
            return None
        return [dict(zip(fields, line.split("\t"))) for line in done.stdout.splitlines()]

    def sessions(self) -> set[str]:
        return {row["session_name"] for row in self.rows(["list-sessions"], ["session_name"]) or []}


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
        "-w", "--worktree", "--tmux"}
VALUE = {"--model", "--permission-mode", "--effort", "--agent", "--settings", "--setting-sources",
         "--append-system-prompt", "--system-prompt", "--system-prompt-file",
         "--append-system-prompt-file", "--fallback-model", "--output-format", "--input-format",
         "--max-turns", "--max-budget-usd", "--session-id", "-n", "--name", "--tools",
         "--json-schema", "--agents", "--permission-prompt-tool", "--plugin-url", "--environment"}
VARIADIC = {"--add-dir", "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools",
            "--disallowed-tools", "--plugin-dir", "--betas"}
OPTIONAL = {"-r", "--resume", "-d", "--debug", "--remote-control", "-w", "--worktree",
            "--from-pr", "--teleport", "--cloud"}


def options(arguments: list[str]) -> list[str]:
    """The options a session was started with that it should be resumed with too
    (--dangerously-skip-permissions, --model X): not those that pick a session, nor a prompt,
    which would be sent again."""
    out, i = [], 0
    while i < len(arguments):
        argument = arguments[i]
        if argument == "--":
            break
        if not argument.startswith("-"):
            i += 1
            continue
        name = argument.split("=", 1)[0]
        values = []
        if not (argument.startswith("--") and "=" in argument):
            if name in VARIADIC:
                j = i + 1
                while j < len(arguments) and not arguments[j].startswith("-"):
                    values.append(arguments[j])
                    j += 1
            elif name in VALUE and i + 1 < len(arguments):
                values.append(arguments[i + 1])
            elif name in OPTIONAL and i + 1 < len(arguments) and not arguments[i + 1].startswith("-"):
                values.append(arguments[i + 1])
        i += 1 + len(values)
        if name not in DROP:
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
                      "args": options(system.claude_arguments(processes.get(pid, (0, []))[1]) or []),
                      "had_transcript": places.has_transcript(sid)}
            if record.get("parkedJobId"):
                job = places.job(record["parkedJobId"])
                claude.update(job_id=record["parkedJobId"],
                              job_session_id=job.get("resumeSessionId") or job.get("sessionId"),
                              job_name=job.get("name"))
            pane["claude"] = claude
        elif (pid := _below(shell, children, attaching)):
            job_id, job = places.find_job(system.claude_arguments(processes[pid][1])[1])
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

def boot_time() -> float:
    if system.MAC:
        answer = system.run(["sysctl", "-n", "kern.boottime"]) or ""
        try:
            return float(answer.split("sec =")[1].split(",")[0])
        except (IndexError, ValueError):
            return 0.0
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return str(int(boot_time()))        # none on macOS: when it started says which boot


class Snapshots:
    """The snapshot files: <when>.json, {"version": 1, "saved_at": <Unix time>, "sessions": [...]}."""

    def __init__(self, folder: Path | None = None):
        self.home = folder or home()
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
        """Deletes the snapshots more than three days old, but never the newest from before the
        computer last started. Returns those deleted."""
        now = time.time() if now is None else now
        boot = boot_time() if boot is None else boot
        entries = self.entries()
        keep = next((path for path, data in entries if data["saved_at"] < boot), None)
        gone = []
        for path, data in entries:
            if path != keep and data["saved_at"] < now - KEEP_FOR:
                try:
                    path.unlink()
                    gone.append(path)
                except OSError:
                    pass
                self._loaded.pop(path.name, None)
        return gone

    def restored_this_boot(self) -> bool:
        try:
            return self.marker.read_text().strip() == boot_id()
        except OSError:
            return False

    def pick(self, running: set[str], boot: float | None = None) -> Path | None:
        """The snapshot to restore: until a restore since the computer started, the newest from
        before then; after it, the newest. Of those, the newest with a tmux session not running
        now, else the newest."""
        entries = self.entries()
        if not self.restored_this_boot():
            boot = boot_time() if boot is None else boot
            entries = [(path, data) for path, data in entries if data["saved_at"] < boot] or entries
        for path, data in entries:
            if any(s["name"] not in running for s in data["sessions"]):
                return path
        return entries[0][0] if entries else None


# MARK: Bringing them back

def launch(pane: dict, places: Places = Places(), claude: str = "claude") -> tuple[str | None, str]:
    """What to type into a restored pane, and a word on what it is: None for a plain shell."""
    c = pane.get("claude")
    if not c:
        return None, ""
    if c.get("job_id"):
        if (places.jobs / c["job_id"]).is_dir():
            return f"{claude} attach {shlex.quote(c['job_id'])}", f"background job {c.get('job_name') or ''}".strip()
        sid = c.get("job_session_id") or c.get("session_id")
        if not sid:                 # a job it can't place: let `claude attach` make what it can of it
            return f"{claude} attach {shlex.quote(c['job_id'])}", "background job"
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
    return " ".join(parts), note


def _folders(session: dict) -> set[str]:
    return {p["claude"]["cwd"] if "claude" in p else p["cwd"] for w in session["windows"] for p in w["panes"]}


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

    # Never Claude Code in the wrong folder: wait for one that isn't there yet.
    def missing() -> set[str]:
        return {d for s in todo for d in _folders(s) if not os.path.isdir(d)}
    gone = missing()
    if gone and wait > 0 and not dry_run:
        say(f"waiting up to {int(wait)} s for {len(gone)} folder(s): {', '.join(sorted(gone)[:3])}")
        deadline = time.monotonic() + wait
        while gone and time.monotonic() < deadline:
            time.sleep(2)
            gone = missing()
    skipped += [f"{s['name']}: no folder {', '.join(sorted(_folders(s) & gone))}" for s in todo if _folders(s) & gone]
    todo = [s for s in todo if not _folders(s) & gone]
    if dry_run or not todo:
        for s in todo:
            for w in s["windows"]:
                for p in w["panes"]:
                    command, note = launch(p, places, claude)
                    say(f"{s['name']}:{w['index']}.{p['index']}  {command or 'shell'}  {note}".rstrip())
        return Restored([s["name"] for s in todo] if dry_run else [], [], skipped)

    live = {record.get("sessionId") for record in _running(places.registry).values()}
    launches = []       # (pane id, command, whether to press Enter, label)
    for s in todo:
        chosen = None
        for n, w in enumerate(s["windows"]):
            folders = [p["claude"]["cwd"] if "claude" in p else p["cwd"] for p in w["panes"]]
            fmt = "#{window_id}\t#{pane_id}"
            if n == 0:
                out = tmux("new-session", "-d", "-P", "-F", fmt, "-s", s["name"], "-n", w["name"],
                           "-c", folders[0], "-x", w["width"], "-y", w["height"]).stdout
                window, first = out.strip().split("\t")
                if tmux("display", "-p", "-t", window, "#{window_index}").stdout.strip() != str(w["index"]):
                    tmux("move-window", "-s", window, "-t", f"={s['name']}:{w['index']}")
            else:
                out = tmux("new-window", "-d", "-P", "-F", fmt, "-t", f"={s['name']}:{w['index']}",
                           "-n", w["name"], "-c", folders[0]).stdout
                window, first = out.strip().split("\t")
            tmux("resize-window", "-t", window, "-x", w["width"], "-y", w["height"], check=False)
            made, last = [first], first
            for folder in folders[1:]:
                # Split the last pane each time, so the panes come in the order they were.
                last = tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", last, "-c", folder).stdout.strip()
                made.append(last)
                tmux("select-layout", "-t", window, "tiled", check=False)
            tmux("select-layout", "-t", window, w["layout"], check=False)
            tmux("set-option", "-w", "-u", "-t", window, "window-size", check=False)
            if w.get("auto_rename"):
                tmux("set-option", "-w", "-t", window, "automatic-rename", "on", check=False)
            for p, pane in zip(w["panes"], made):
                if p.get("active"):
                    tmux("select-pane", "-t", pane, check=False)
                command, note = launch(p, places, claude)
                if command:
                    # A session already running elsewhere is typed but not started: two of one is trouble.
                    twice = not p["claude"].get("job_id") and p["claude"].get("session_id") in live
                    label = f"{s['name']}:{w['index']}.{p['index']}  {note}".rstrip()
                    launches.append((pane, command, not twice, label + ("  (running elsewhere: not started)" if twice else "")))
            if w.get("active"):
                chosen = window
        if chosen:
            tmux("select-window", "-t", chosen, check=False)
        say(f"made {s['name']}: {len(s['windows'])} window(s), {sum(len(w['panes']) for w in s['windows'])} pane(s)")

    if launches:
        time.sleep(settle)          # the new shells start before anything is typed into them
        for pane, command, enter, label in launches:
            tmux("send-keys", "-t", pane, "-l", command)
            if enter:
                tmux("send-keys", "-t", pane, "Enter")
                time.sleep(stagger)
            say(("started " if enter else "typed ") + label)
    try:
        store.home.mkdir(parents=True, exist_ok=True)
        store.marker.write_text(boot_id())
    except OSError:
        pass
    return Restored([s["name"] for s in todo], [label for _, _, enter, label in launches if enter], skipped)


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
    """For the app: a snapshot when asked (every couple of minutes, while switched on), and a
    restore, each off the main thread and one at a time. What they do goes to the log."""

    def __init__(self, store: Snapshots | None = None, tmux: Tmux | None = None, places: Places = Places(),
                 log: Callable[[str], None] | None = None):
        from concurrent.futures import ThreadPoolExecutor
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
        if self.restoring:
            return None
        running = self.tmux.sessions()
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
