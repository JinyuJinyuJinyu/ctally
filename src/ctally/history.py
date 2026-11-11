"""Your history with Claude Code, for `ctally stats`: how long Claude worked for you and how
long it waited on you, how many prompts and finished turns, and the usage limits you ran
into, day by day.

The hook keeps the history in ~/.claude/ctally.d/.history/<date>: a line "<Unix time>
<session id> <state>" whenever a session's state changes, for each prompt ("... working
prompt"), every five minutes or so while a state lasts and Claude Code keeps calling, and
"end" when the session ends. A session is in a state from one of its lines to its next, for no
longer than a state is taken to last with nothing heard. After its last line, a session still
running stays in that state until now, for no longer than that either; one that has gone
without a word, a few minutes at most. Nothing in it but times, session ids and states.
"""
from __future__ import annotations

import math
import os
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from . import system
from .sessions import STATE_DIR

HISTORY_DIR = STATE_DIR / ".history"
KEEP_DAYS = 90
BEAT = 300                  # the hook notes a lasting state again after five minutes
# The longest a state is taken to have lasted between two lines. Longer than that working, the
# computer was more likely asleep than Claude busy; waiting, you were away from your desk.
LONGEST = {"working": 3600, "waiting": 12 * 3600, "limited": 6 * 3600}
BACK_WITHIN = 2 * 3600      # a finished turn followed by a prompt within this: you came back to it


class Line(NamedTuple):
    when: float
    session: str
    state: str              # working, waiting, done, limited, end
    prompt: bool = False


class Stretch(NamedTuple):
    """A session in one state, from one time to another."""
    session: str
    state: str
    start: float
    end: float
    settled: bool           # ended by the session's next line, rather than by its going quiet


@dataclass
class Day:
    day: date
    working: float = 0.0    # seconds, all sessions added up
    waiting: float = 0.0
    limited: float = 0.0
    prompts: int = 0
    turns: int = 0          # turns finished
    limits: int = 0         # times the usage limit stopped a session
    sessions: set = field(default_factory=set)


@dataclass
class Stats:
    days: list[Day]
    busy: float = 0.0               # seconds with at least one session working
    peak: int = 0                   # the most sessions working at once
    peak_at: float | None = None
    answered: float | None = None   # median seconds to answer a session waiting on you
    came_back: float | None = None  # median seconds to send a prompt after a finished turn
    since: date | None = None       # the oldest history there is

    @property
    def total(self) -> Day:
        whole = Day(self.days[-1].day if self.days else date.today())
        for day in self.days:
            whole.working += day.working
            whole.waiting += day.waiting
            whole.limited += day.limited
            whole.prompts += day.prompts
            whole.turns += day.turns
            whole.limits += day.limits
            whole.sessions |= day.sessions
        return whole


# MARK: Reading

def _day(name: str) -> date | None:
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def read(directory: Path | None = None, first: date | None = None) -> list[Line]:
    """Every line from the day `first` on (all of them without), oldest first."""
    directory = HISTORY_DIR if directory is None else directory
    try:
        names = [n for n in os.listdir(directory) if (d := _day(n)) and (first is None or d >= first)]
    except OSError:
        return []
    lines = []
    for name in names:
        try:
            text = (directory / name).read_text(errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            parts = raw.split()
            if len(parts) >= 3 and parts[0].isdigit():
                lines.append(Line(float(parts[0]), parts[1], parts[2], "prompt" in parts[3:]))
    lines.sort(key=lambda line: line.when)
    return lines


def oldest(directory: Path | None = None) -> date | None:
    directory = HISTORY_DIR if directory is None else directory
    try:
        days = [d for n in os.listdir(directory) if (d := _day(n))]
    except OSError:
        return None
    return min(days, default=None)


def live(directory: Path | None = None) -> dict[str, str]:
    """The sessions running now, and the state the hooks last left each in."""
    directory = STATE_DIR if directory is None else directory
    found = {}
    try:
        names = [n for n in os.listdir(directory) if not n.startswith(".")]
    except OSError:
        return found
    for name in names:
        try:
            fields = (directory / name).read_text(errors="replace").split()
        except OSError:
            continue
        if len(fields) >= 2 and fields[1].isdigit() and system.is_alive(int(fields[1])):
            found[name] = fields[0]
    return found


def prune(directory: Path | None = None, now: float | None = None) -> None:
    """Days older than KEEP_DAYS go, and the marks of sessions not heard from in a day."""
    directory = HISTORY_DIR if directory is None else directory
    now = time.time() if now is None else now
    cutoff = datetime.fromtimestamp(now).date() - timedelta(days=KEEP_DAYS)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = directory / name
        try:
            day = _day(name)
            if (day and day < cutoff) or (name.startswith(".") and now - path.stat().st_mtime > 86400):
                path.unlink()
        except OSError:
            pass


# MARK: Working it out

def stretches(lines: list[Line], now: float, running: dict[str, str]) -> list[Stretch]:
    """Each session's states, one after another. Working, waiting or stopped by the limit last
    no longer than LONGEST from one line to the next; after its last line, a session that's
    still running in that state stays in it until now, for no longer than LONGEST either (its
    file can say working for hours after I've stopped it by hand, and what counts now mustn't
    shrink once its next line comes), and one that isn't, BEAT at most. Lines that only repeat
    the state, every few minutes, join what they continue."""
    by_session: dict[str, list[Line]] = defaultdict(list)
    for line in lines:
        by_session[line.session].append(line)
    found = []
    for sid, own in by_session.items():
        mine: list[Stretch] = []
        for here, after in zip(own, own[1:] + [None]):
            longest = LONGEST.get(here.state)
            if longest is None:
                continue                            # done, or ended: nothing to add up
            if after is not None:
                end, settled = min(after.when, here.when + longest), after.state != here.state
            elif running.get(sid) == here.state:
                end, settled = min(max(now, here.when), here.when + longest), False
            else:
                end, settled = min(now, here.when + BEAT), False
            last = mine[-1] if mine else None
            if last and last.state == here.state and last.end == here.when and not last.settled:
                mine[-1] = last._replace(end=end, settled=settled)
            else:
                mine.append(Stretch(sid, here.state, here.when, end, settled))
        found += mine
    return found


def _by_day(start: float, end: float):
    """A stretch of time cut at local midnights: (day, seconds) for each day it touches."""
    while start < end:
        day = datetime.fromtimestamp(start).date()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        cut = min(end, midnight)
        yield day, cut - start
        start = cut


def stats(days: int = 7, now: float | None = None, directory: Path | None = None,
          running: dict[str, str] | None = None) -> Stats:
    """The last `days` days, today included."""
    directory = HISTORY_DIR if directory is None else directory
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, datetime.min.time()).timestamp()
    # A day further back, to know what each session was doing as the first day began.
    lines = read(directory, first - timedelta(days=1))
    running = live(directory.parent) if running is None else running
    table = {first + timedelta(days=i): Day(first + timedelta(days=i)) for i in range(days)}

    # Counts: prompts, and the turns finished and limits hit, as changes, not as the lines
    # repeating a state.
    previous: dict[str, str] = {}
    came_back, finished = [], {}
    for line in lines:
        before = previous.get(line.session)
        previous[line.session] = line.state
        if line.state == "done" and before != "done":
            finished[line.session] = line.when
        if line.prompt and line.session in finished:
            gap = line.when - finished.pop(line.session)
            if gap <= BACK_WITHIN and line.when >= start:
                came_back.append(gap)
        if line.when < start:
            continue
        day = table.get(datetime.fromtimestamp(line.when).date())
        if day is None:
            continue
        if line.state != "end":
            day.sessions.add(line.session)
        day.prompts += line.prompt
        day.turns += line.state == "done" and before != "done"
        day.limits += line.state == "limited" and before != "limited"

    # Time: each stretch, inside the days asked for, added to the day it fell on.
    spans = stretches(lines, now, running)
    answered = []
    working = []
    for span in spans:
        lo, hi = max(span.start, start), min(span.end, now)
        if hi <= lo:
            continue
        if span.state == "waiting" and span.settled:
            answered.append(span.end - span.start)
        if span.state == "working":
            working.append((lo, hi))
        for day, seconds in _by_day(lo, hi):
            if day in table:
                setattr(table[day], span.state, getattr(table[day], span.state) + seconds)

    found = Stats(list(table.values()), since=oldest(directory))
    found.answered = statistics.median(answered) if answered else None
    found.came_back = statistics.median(came_back) if came_back else None
    found.busy, found.peak, found.peak_at = _overlap(working)
    return found


def _overlap(spans: list[tuple[float, float]]) -> tuple[float, int, float | None]:
    """Of some stretches of time: how long at least one was going, the most going at once, and
    when that first happened."""
    edges = sorted([(lo, 1) for lo, _ in spans] + [(hi, -1) for _, hi in spans], key=lambda e: (e[0], e[1]))
    busy, count, peak, peak_at, since = 0.0, 0, 0, None, None
    for moment, step in edges:
        if count and since is not None:
            busy += moment - since
        count += step
        since = moment
        if count > peak:
            peak, peak_at = count, moment
    return busy, peak, peak_at


# MARK: Saying it

def duration(seconds: float) -> str:
    """"5h 12m", "18m", "45s", or "-" for none."""
    if seconds < 1:
        return "-"
    if seconds < 60:
        return f"{math.floor(seconds)}s"
    minutes = math.floor(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def report(found: Stats, now: float) -> list[str]:
    """The table, a line a day and one for them all, then what stands out."""
    from .usage import clock

    today = datetime.fromtimestamp(now).date()
    header = ("", "working", "waiting on you", "prompts", "turns", "sessions", "limit hits")
    rows = []
    for day in found.days:
        name = "Today" if day.day == today else f"{day.day:%a %b} {day.day.day}"
        rows.append(_row(name, day))
    rows.append(_row(f"Last {len(found.days)} days" if len(found.days) > 1 else "In all", found.total))
    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    out = ["  ".join(str(cell).ljust(widths[i]) if i == 0 else str(cell).rjust(widths[i])
                     for i, cell in enumerate(row)).rstrip() for row in [header] + rows]

    total = found.total
    out.append("")
    if total.working:
        line = f"Claude worked {duration(total.working)} in all"
        if found.peak > 1 and found.peak_at is not None:
            at = datetime.fromtimestamp(found.peak_at)
            when = "today" if at.date() == today else f"{at:%a}"
            line += f", with up to {found.peak} sessions at once ({when} {clock(found.peak_at)})"
            line += f"; something was working for {duration(found.busy)}"
        out.append(line + ".")
    if total.limited:
        out.append(f"The usage limit stopped sessions for {duration(total.limited)}.")
    answers = []
    if found.answered is not None:
        answers.append(f"answered what it asked in {duration(found.answered)}")
    if found.came_back is not None:
        answers.append(f"came back to finished turns in {duration(found.came_back)}")
    if answers:
        out.append("You " + ", and ".join(answers) + " (medians).")
    if found.since:
        out.append(f"History since {found.since:%b} {found.since.day}.")
    return out


def _row(name: str, day: Day) -> tuple:
    def count(n: int) -> str:
        return str(n) if n else "-"
    return (name, duration(day.working), duration(day.waiting), count(day.prompts), count(day.turns),
            count(len(day.sessions)), count(day.limits))


def as_json(found: Stats) -> dict:
    def day(d: Day) -> dict:
        return {"day": d.day.isoformat(), "working": round(d.working), "waiting": round(d.waiting),
                "limited": round(d.limited), "prompts": d.prompts, "turns": d.turns, "limit_hits": d.limits,
                "sessions": len(d.sessions)}
    return {"days": [day(d) for d in found.days], "total": day(found.total), "busy": round(found.busy),
            "peak": found.peak, "peak_at": found.peak_at, "answered_median": found.answered,
            "came_back_median": found.came_back, "since": found.since.isoformat() if found.since else None}
