"""ctally: CTally from the shell and from tmux.

  ctally run                    start the indicator (setup arranges this at login)
  ctally setup [--tmux]         install the Claude Code hooks and status line, start CTally at
                                login, and start it now; --tmux also binds prefix + J and puts
                                the counts in tmux's status line
  ctally uninstall              undo all of that (then: pipx uninstall ctally)

  ctally status [--tmux]        counts of live sessions by state, most urgent first: "!1 ▶2 ✓5 ·1"
                                (waiting, working, done, idle); --tmux adds status-line colours
                                and your plan's session and weekly usage: "5h 71% 7d 49%"
  ctally list                   one line per live session: state, tmux pane, subagents running,
                                project, id
  ctally usage [--json]         your plan's usage limits, as Claude Code's /usage shows them:
                                the session, the week, each model's week, and when each resets
  ctally jump [pane client socket]
                                switch a tmux client to the session that needs you: waiting first,
                                then finished, longest-waiting first. Run it again to move on to
                                the next one. A key binding passes '#{pane_id}' '#{client_name}'
                                '#{socket_path}'.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import NamedTuple

from . import __version__, system
from . import usage as limits
from .sessions import PROJECTS_DIR, STATE_DIR, AgentWatcher, Names, Registry, read_limit, reconcile, stopped_by_hand

GLYPH = {"waiting": "!", "working": "▶", "done": "✓", "idle": "·"}
COLOUR = {"waiting": "#ffb329", "working": "#4accf2", "done": "#45e087", "idle": "#99a6bd"}
# Stopped by the usage limit: shown as waiting, but nothing there for me to do.
RANK = {"waiting": 0, "done": 1, "working": 2, "limited": 2, "idle": 3}
LEVEL_COLOUR = ("#99a6bd", "#ffb329", "#ff6661")    # usage: fine, getting close, nearly out


class Live(NamedTuple):
    rank: int
    modified: float
    state: str
    pid: int
    id: str
    project: str


def live_sessions() -> list[Live]:
    """Each live session, most urgent first and, within a state, longest-waiting first.
    Sessions whose claude process is gone are skipped; those the hooks haven't heard from
    come from Claude Code's list of running sessions."""
    found = []
    running = Registry().poll()
    try:
        names = [n for n in os.listdir(STATE_DIR) if not n.startswith(".")]
    except OSError:
        names = []
    limit = read_limit(STATE_DIR)
    transcripts, watcher = Names(PROJECTS_DIR), AgentWatcher(STATE_DIR / ".agents")
    for name in names:
        path = STATE_DIR / name
        try:
            fields = path.read_text(errors="replace").split()
            modified = path.stat().st_mtime
        except OSError:
            continue
        if len(fields) < 2 or not fields[1].isdigit():
            continue
        pid = int(fields[1])
        if not system.is_alive(pid):
            continue
        state = fields[0] if fields[0] in RANK else "idle"
        if state == "limited" and not (limit and limit.holds(time.time())):
            state = "done"                  # the limit is over; the session just sits
        if state in ("working", "waiting"):
            transcripts.info(name)
            if stopped_by_hand(transcripts, watcher, name, modified):
                state = "done"
        found.append(Live(RANK[state], modified, state, pid, name, " ".join(fields[2:]) or "?"))
    unheard, left = reconcile(running, {s.id: (s.pid, s.modified) for s in found})
    found = [s for s in found if s.id not in left]
    for r in unheard:
        found.append(Live(RANK[r.state.value], r.updated, r.state.value, r.pid, r.id,
                          os.path.basename(r.cwd.rstrip("/")) or "?"))
    return sorted(found)


def agents_running(sid: str) -> int:
    """Not counting those stopped without a word to the hooks."""
    transcripts = Names(PROJECTS_DIR)
    transcripts.info(sid)
    activity = AgentWatcher(STATE_DIR / ".agents").activity(sid, transcripts.transcript(sid))
    return activity.running if activity else 0


def status(tmux: bool) -> None:
    counts: dict[str, int] = {}
    for session in live_sessions():
        state = "waiting" if session.state == "limited" else session.state
        counts[state] = counts.get(state, 0) + 1
    items = []
    for state in ("waiting", "working", "done", "idle"):
        if counts.get(state):
            item = f"{GLYPH[state]}{counts[state]}"
            items.append(f"#[fg={COLOUR[state]}]{item}" if tmux else item)
    if tmux:
        items += usage_items()
    out = " ".join(items)
    print(out + "#[default]" if tmux and out else out)


def usage_items() -> list[str]:
    """The session and the week for the status line, coloured as they fill up; none if
    they're switched off in the settings or not known."""
    from .prefs import Prefs
    found = limits.read() if Prefs()["usage"] else None
    if found is None:
        return []
    return [f"#[fg={LEVEL_COLOUR[limit.level]}]{limit.tag or limit.short} {limits.percent_text(limit)}"
            for limit in found.at(time.time()).compact]


def usage(as_json: bool) -> int:
    found = limits.read()
    if found is None:
        print("ctally: no usage limits known yet. Claude Code saves them once a session on a Pro, Max or "
              "Team plan has run.", file=sys.stderr)
        return 1
    now = time.time()
    current = found.at(now)
    if as_json:
        print(json.dumps({"updated": found.fetched_at, "stale": found.is_stale(now), "limits": [
            {"title": limit.title, "percent": limit.percent, "resets_at": limit.resets_at,
             "level": ("ok", "warning", "critical")[limit.level], **({"detail": limit.detail} if limit.detail else {})}
            for limit in current.limits]}, indent=1))
        return 0
    width = max(len(limit.title) for limit in current.limits)
    for limit in current.limits:
        filled = min(10, max(0, round(limit.percent / 10)))
        bar = "█" * filled + "░" * (10 - filled)
        after = limit.detail or limits.resets(limit.resets_at, now)
        print(f"{limit.title:<{width}}  {bar} {limits.percent_text(limit):>4}  {after}".rstrip())
    print(limits.describe(found, now)[-1])
    return 0


def list_sessions() -> None:
    for session in live_sessions():
        target = system.tmux_pane(session.pid)
        location = (system.tmux_location(target) if target else None) or "-"
        n = agents_running(session.id)
        agents = "-" if n == 0 else "1 agent" if n == 1 else f"{n} agents"
        print(f"{session.state:<8} {location:<16} {agents:<9} {session.project:<24} {session.id}")


def jump(current: str | None, client: str | None, here: str | None) -> int:
    current = current or os.environ.get("TMUX_PANE", "")
    here = here or os.environ.get("TMUX", "").split(",", 1)[0]
    binary = system.tmux_binary() or "tmux"

    def say(message: str) -> None:
        if client and here:
            system.run([binary, "-S", here, "display-message", "-c", client, message])
        else:
            print(message)

    # Sessions that need me, in tmux, in order.
    queue = []
    for session in live_sessions():
        if session.rank > 1:
            continue
        target = system.tmux_pane(session.pid)
        if target:
            queue.append((target, session))
    if not queue:
        say("CTally: nothing needs you")
        return 0

    # The one after the pane I'm on, so pressing again moves along the queue.
    at = next((i for i, (target, _) in enumerate(queue) if target.pane == current), None)
    target, session = queue[at + 1] if at is not None and at + 1 < len(queue) else queue[0]

    if not here:
        print(f"ctally jump: run it inside tmux (or bind it to a key); next up is "
              f"{session.project} ({session.state})", file=sys.stderr)
        return 1
    if target.socket != here:
        say(f"CTally: {session.project} is on another tmux server")
        return 1
    switch = [binary, "-S", target.socket, "switch-client"] + (["-c", client] if client else []) + ["-t", target.pane]
    if system.run(switch) is None:
        say(f"CTally: couldn't switch to {session.project}")
        return 1
    say(f"CTally: {session.project} · {session.state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctally", description=__doc__.split("\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n".join(__doc__.split("\n")[2:]))
    parser.add_argument("--version", action="version", version=f"ctally {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser("run", help="start the indicator")
    setup = commands.add_parser("setup", help="install hooks, start at login, start now")
    setup.add_argument("--tmux", action="store_true", help="also bind prefix + J and add tmux status-line counts")
    setup.add_argument("--no-hooks", action="store_true", help="leave ~/.claude/settings.json alone")
    setup.add_argument("--no-statusline", action="store_true",
                       help="no status line: usage limits then refresh only when Claude Code fetches them")
    setup.add_argument("--no-autostart", action="store_true", help="don't start CTally at login")
    setup.add_argument("--no-launch", action="store_true", help="don't start CTally now")
    commands.add_parser("uninstall", help="remove hooks, tmux setup, autostart and state")
    status_ = commands.add_parser("status", help="counts by state")
    status_.add_argument("--tmux", action="store_true", help="with tmux status-line colours")
    commands.add_parser("list", help="one line per live session")
    usage_ = commands.add_parser("usage", help="your plan's usage limits")
    usage_.add_argument("--json", action="store_true", help="as JSON, with reset times in Unix time")
    jump_ = commands.add_parser("jump", help="switch tmux to the session that needs you")
    jump_.add_argument("pane", nargs="?")
    jump_.add_argument("client", nargs="?")
    jump_.add_argument("socket", nargs="?")
    args = parser.parse_args(argv)

    if args.command == "run":
        from .gui.app import run          # Qt loads only for the indicator
        return run()
    if args.command == "setup":
        from . import install
        return install.setup(tmux=args.tmux, hooks=not args.no_hooks, statusline=not args.no_statusline,
                             autostart=not args.no_autostart, launch=not args.no_launch)
    if args.command == "uninstall":
        from . import install
        return install.uninstall()
    if args.command == "status":
        status(args.tmux)
        return 0
    if args.command == "list":
        list_sessions()
        return 0
    if args.command == "usage":
        return usage(args.json)
    if args.command == "jump":
        return jump(args.pane, args.client, args.socket)
    parser.print_help()
    return 2
