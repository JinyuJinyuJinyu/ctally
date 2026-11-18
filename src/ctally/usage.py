"""Your plan's usage limits, as Claude Code's /usage shows them: the current session (the
five-hour window, on a plan that has one), the week across all models, any per-model weekly
limits, and usage credits when they're switched on. Each is the share used and when it resets.

Three places, all only read here:

- ~/.claude.json holds everything /usage last fetched. Claude Code fetches it now and then
  (for /usage, say), not after every reply, so it can be hours old. It's Claude Code's own
  cache rather than a documented interface, so this expects to find nothing, or something
  new, and copes.
- CTally's own copy of the same, asked of Anthropic every few minutes while Claude Code is in
  use (~/.claude/ctally.d/.fetched/<account id>): see fetch.py, which writes it. Of the two,
  the newer wins.
- CTally's status line saves what Claude Code hands every status line after each reply: the
  session and the week, nothing else, the latest reply's for each account you use
  (~/.claude/ctally.d/.usage/<account id>). Where it's newer, it wins for those two. One of
  the two it says nothing of, which the cache lists with nothing used, isn't a limit of the
  plan's any more, and isn't shown. The rest, each model's week, stay as last fetched, and
  say how old they are once that's well behind the session and the week.

All are an account's. ~/.claude.json names the one signed in, and only its numbers are shown:
sign in to another, or swap accounts under the sessions that are running, and the limits are
that account's from then on, as last seen on it, or none until it has been.

Neither moves when a request is refused for the limit, so the hook's note of a limit hit
(~/.claude/ctally.d/.limit) marks that one used up until it resets.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import fetch
from .sessions import STATE_DIR, LimitHit, read_limit

CLAUDE_JSON = Path(os.environ.get("CTALLY_CLAUDE_JSON") or Path.home() / ".claude.json")
STATUS_LINE = STATE_DIR / ".statusline"
STALE_AFTER = 3600          # Claude Code itself stops trusting its copy after an hour
WARN, ALERT = 75, 90        # percent used: getting close, nearly out

# The older per-window fields, for whatever the server's list of limits doesn't cover.
WINDOWS = (
    ("five_hour", ("session",), "Current session", "Session", "5h"),
    ("seven_day", ("week",), "Current week (all models)", "Week", "7d"),
    ("seven_day_sonnet", ("week", "sonnet"), "Current week (Sonnet only)", "Week · Sonnet", ""),
    ("seven_day_opus", ("week", "opus"), "Current week (Opus only)", "Week · Opus", ""),
)
PERIODS = {"session": "session", "daily": "day", "weekly": "week", "monthly": "month"}
SEVERITY = {"warning": 1, "critical": 2, "exceeded": 2, "rejected": 2, "limited": 2}
SPANS = {("session",): 5 * 3600, ("week",): 7 * 86400}     # how long each window runs


class Limit(NamedTuple):
    key: tuple                  # what it limits: ("session",), ("week",), ("week", "fable")
    title: str                  # as /usage says it: "Current week (all models)"
    short: str                  # for a narrow column: "Week"
    tag: str                    # for the tightest spots: "5h", "7d", or none
    percent: float              # used, 0–100
    resets_at: float | None     # Unix time
    severity: str = "normal"    # the server's own word for it, when it gives one
    detail: str = ""            # "$12.40 of $50.00", for credits
    seen: float | None = None   # when this number was read: the cache's fetch, or a reply's

    @property
    def level(self) -> int:
        """0 fine, 1 getting close, 2 nearly out: the server's word or the percentage,
        whichever is more worried."""
        by_percent = 2 if self.percent >= ALERT else 1 if self.percent >= WARN else 0
        return max(SEVERITY.get(self.severity, 0), by_percent)


class Usage(NamedTuple):
    limits: tuple[Limit, ...]
    fetched_at: float           # Unix time

    def at(self, now: float) -> Usage:
        """As things stand at `now`: a window whose reset time has passed starts again from
        nothing."""
        return self._replace(limits=tuple(
            limit._replace(percent=0.0, resets_at=None, severity="normal", detail="", seen=None)
            if limit.resets_at is not None and limit.resets_at <= now else limit
            for limit in self.limits))

    def is_stale(self, now: float) -> bool:
        return now - self.fetched_at > STALE_AFTER

    def behind(self, limit: Limit) -> bool:
        """Whether a limit's number is much older than the rest: a model's week, say, which
        only /usage brings up to date, while every reply brings the session and the week."""
        return limit.seen is not None and self.fetched_at - limit.seen > STALE_AFTER

    @property
    def compact(self) -> tuple[Limit, ...]:
        """The two that matter most, for places with room for little: the session and the
        week."""
        tagged = tuple(limit for limit in self.limits if limit.tag)
        return (tagged or self.limits)[:2]


# MARK: Reading

def read(path: Path | None = None, live: Path | None = None) -> Usage | None:
    """The limits as last seen, or None if they haven't been (an API key rather than a plan,
    or no session since signing in). Nothing is asked for here: the tmux status bar calls this
    every few seconds."""
    config = _load(path or CLAUDE_JSON)
    live = live or STATUS_LINE
    who = account(config)
    now = time.time()
    latest = read_live(live_file(live, who))
    cached = newest(parse(config), read_fetched(fetched_file(live, who)))
    return with_hit(merge(in_force(cached, latest, now), latest), read_limit(live.parent), now, who)


def refresh(path: Path | None = None, live: Path | None = None,
            fetcher: fetch.Fetcher | None = None) -> str | None:
    """`ctally usage`: asks Anthropic first, if the newest numbers are a few minutes old. Why
    it couldn't, if it couldn't."""
    source = path or CLAUDE_JSON
    config = _load(source)
    who = account(config)
    target = fetched_file(live or STATUS_LINE, who)
    if who is None or target is None:
        return None
    now = time.time()
    have = newest(parse(config), read_fetched(target))
    if have is not None and now - have.fetched_at < fetch.EVERY:
        return None
    waiting = fetch.told_to_wait(target, now)
    if waiting:
        return waiting
    fetcher = fetcher or fetch.Fetcher(log=lambda _: None)
    if fetcher.once(who, target, lambda who: still_signed_in(source, who)):
        fetch.wait_file(target).unlink(missing_ok=True)
        return None
    if fetcher.wait_until > now:
        fetch.note_wait(target, fetcher.wait_until, fetcher.problem or "asked too often")
    return fetcher.problem


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def still_signed_in(path: Path, who: str) -> bool:
    """Whether an account is still the one ~/.claude.json names. A file caught halfway through
    being written doesn't say it isn't."""
    config = _load(path)
    return config is None or account(config) == who


def account(config) -> str | None:
    """The account signed in, as ~/.claude.json names it: the one whose limits these are."""
    found = config.get("oauthAccount") if isinstance(config, dict) else None
    uuid = found.get("accountUuid") if isinstance(found, dict) else None
    return uuid if isinstance(uuid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", uuid) else None


def live_file(status_line: Path, who: str | None) -> Path:
    """Where CTally's status line keeps the latest for an account: .usage/<account id> beside
    .statusline ("-" with nobody signed in), which may not be there yet. A status line from
    before it told accounts apart kept only .statusline, the latest from any."""
    store = status_line.parent / ".usage"
    return store / (who or "-") if store.is_dir() else status_line


def fetched_file(status_line: Path, who: str | None) -> Path | None:
    """Where fetch.py keeps what it last heard for an account: .fetched/<account id> beside
    .statusline; nowhere with nobody signed in."""
    return status_line.parent / ".fetched" / who if who else None


def read_fetched(path: Path | None) -> Usage | None:
    """What fetch.py last heard, kept as ~/.claude.json keeps it."""
    return from_cache(_load(path), None) if path is not None else None


def newest(*found: Usage | None) -> Usage | None:
    return max((usage for usage in found if usage is not None), key=lambda usage: usage.fetched_at, default=None)


def read_live(path: Path | None = None) -> Usage | None:
    """The session and the week from CTally's status line, as of when it last wrote them."""
    path = path or STATUS_LINE
    try:
        modified = path.stat().st_mtime
        return parse_live(json.loads(path.read_text()), modified)
    except (OSError, ValueError):
        return None


def parse_live(status, modified: float) -> Usage | None:
    """{"rate_limits": {"five_hour": {"used_percentage", "resets_at"}, "seven_day": ...}}"""
    limits = status.get("rate_limits") if isinstance(status, dict) else None
    if not isinstance(limits, dict):
        return None
    found = []
    for field, key, title, short, tag in WINDOWS[:2]:
        window = limits.get(field)
        percent = _number(window.get("used_percentage")) if isinstance(window, dict) else None
        if percent is not None:
            found.append(Limit(key, title, short, tag, percent, _seconds(window.get("resets_at")), seen=modified))
    return Usage(tuple(found), modified) if found else None


def in_force(cached: Usage | None, live: Usage | None, now: float) -> Usage | None:
    """The cache's limits, less a session or a week the plan no longer has: one the account's
    latest reply said nothing of, with nothing used in it by now. A reply gives each window it
    counts toward, and the cache can go on listing an empty one after a plan drops its
    five-hour limit, say. One with some of it used stays until it runs out."""
    if cached is None or live is None:
        return cached
    said = {limit.key for limit in live.limits}
    kept = tuple(limit for limit in cached.limits if limit.key not in SPANS or limit.key in said
                 or (limit.percent > 0 and (limit.resets_at is None or limit.resets_at > now)))
    return cached._replace(limits=kept) if kept else None


def merge(cached: Usage | None, live: Usage | None) -> Usage | None:
    """The cache's limits, with the session and week from the status line where it's newer."""
    if live is None or (cached is not None and live.fetched_at <= cached.fetched_at):
        return cached
    if cached is None:
        return live
    fresh = {limit.key: limit for limit in live.limits}
    limits = [fresh.pop(limit.key, limit) for limit in cached.limits] + list(fresh.values())
    return Usage(_ordered(limits), live.fetched_at)


def with_hit(usage: Usage | None, hit: LimitHit | None, now: float, who: str | None = None) -> Usage | None:
    """The limit a turn just ran into, used up whatever the numbers said: the refused request
    doesn't update them, so they'd sit at 99%, say, until Claude Code next fetched /usage.
    Not one hit on another account than the one signed in (who), when both are known."""
    key = hit.key if hit else None
    if key is None or (who and hit.account and hit.account != who):
        return usage
    current = next((limit for limit in usage.limits if limit.key == key), None) if usage else None
    resets = hit.resets_at
    if (resets is None and current is not None and current.resets_at is not None
            and hit.noted >= current.resets_at - SPANS[key]):
        resets = current.resets_at              # it was hit in the window these numbers are for
    if resets is None or now >= resets:
        return usage
    if current is not None and current.resets_at is not None and current.resets_at > resets + 60:
        return usage                            # reset early: these numbers are a new window's
    if current is None:
        _, _, title, short, tag = next(window for window in WINDOWS if window[1] == key)
        current = Limit(key, title, short, tag, 100.0, resets)
        usage = Usage(_ordered((usage.limits if usage else ()) + (current,)),
                      usage.fetched_at if usage else hit.noted)
    full = current._replace(percent=max(current.percent, 100.0), resets_at=resets, severity="exceeded",
                            seen=max(current.seen or hit.noted, hit.noted))
    return usage._replace(limits=tuple(full if limit.key == key else limit for limit in usage.limits))


def parse(config) -> Usage | None:
    if not isinstance(config, dict):
        return None
    signed_in = config.get("oauthAccount")
    return from_cache(config.get("cachedUsageUtilization"),
                      signed_in.get("accountUuid") if isinstance(signed_in, dict) else None)


def from_cache(cached, who: str | None) -> Usage | None:
    """{"fetchedAtMs", "accountUuid", "utilization"}: ~/.claude.json's copy, or CTally's."""
    if not isinstance(cached, dict):
        return None
    # Numbers saved under another account, before a switch, aren't yours.
    if who and cached.get("accountUuid") and cached["accountUuid"] != who:
        return None
    utilization = cached.get("utilization")
    fetched = _number(cached.get("fetchedAtMs"))
    if not isinstance(utilization, dict) or fetched is None:
        return None
    limits = tuple(limit._replace(seen=fetched / 1000) for limit in _limits(utilization))
    return Usage(limits, fetched / 1000) if limits else None


def _limits(utilization: dict) -> tuple[Limit, ...]:
    found: dict[tuple, Limit] = {}
    # The server's own list first, when it sends one: it names each window itself.
    listed = utilization.get("limits")
    for item in listed if isinstance(listed, list) else ():
        limit = _listed(item)
        if limit is not None:
            found.setdefault(limit.key, limit)
    for field, key, title, short, tag in WINDOWS:
        window = utilization.get(field)
        percent = _number(window.get("utilization")) if isinstance(window, dict) else None
        if percent is not None and key not in found:
            found[key] = Limit(key, title, short, tag, percent, _seconds(window.get("resets_at")))
    credits = _credits(utilization)
    if credits is not None:
        found.setdefault(credits.key, credits)
    return _ordered(found.values())


def _ordered(limits) -> tuple[Limit, ...]:
    """The session, then the week, then each model's week, then anything else, credits last;
    in the server's order within each."""
    rank = {("session",): 0, ("week",): 1}
    return tuple(sorted(limits, key=lambda limit: rank.get(
        limit.key, 4 if limit.key == ("credits",) else 2 if limit.key[0] == "week" else 3)))


def _listed(item) -> Limit | None:
    """One entry of the server's list: {kind, group, percent, severity, resets_at, scope}."""
    if not isinstance(item, dict):
        return None
    percent = _number(item.get("percent"))
    if percent is None:
        return None
    kind, group = str(item.get("kind") or ""), str(item.get("group") or "")
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    name = _display_name(scope.get("model")) or _display_name(scope.get("surface"))
    common = dict(percent=percent, resets_at=_seconds(item.get("resets_at")),
                  severity=str(item.get("severity") or "normal"))
    if kind == "session" or (group == "session" and not name):
        return Limit(("session",), "Current session", "Session", "5h", **common)
    if kind == "weekly_all" or (group == "weekly" and not name):
        return Limit(("week",), "Current week (all models)", "Week", "7d", **common)
    period = PERIODS.get(group)
    if name and period:
        return Limit((period, name.lower()), f"Current {period} ({name})",
                     f"{period.capitalize()} · {name}", "", **common)
    return None


def _credits(utilization: dict) -> Limit | None:
    """Usage credits, which carry on past the plan's limits, when they're switched on."""
    spend = utilization.get("spend")
    if isinstance(spend, dict) and spend.get("enabled"):
        percent = _number(spend.get("percent"))
        if percent is not None:
            used, limit = _money(spend.get("used")), _money(spend.get("limit"))
            return Limit(("credits",), "Usage credits", "Credits", "", percent, None,
                         str(spend.get("severity") or "normal"),
                         f"{used} of {limit}" if used and limit else used)
    extra = utilization.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        percent = _number(extra.get("utilization"))
        if percent is not None:
            return Limit(("credits",), "Usage credits", "Credits", "", percent, None)
    return None


# MARK: Words

def percent_text(limit: Limit) -> str:
    """Rounded down, as /usage does, so 99.6% doesn't read as all used up."""
    return f"{math.floor(limit.percent)}%"


def _minute(moment: float) -> float:
    """A reset time to the nearest minute: the server can put a window's end a moment before
    the hour."""
    return round(moment / 60) * 60


def clock(moment: float) -> str:
    """"6:30 pm", or "6 pm" on the hour, in local time."""
    t = time.localtime(moment)
    hour = t.tm_hour % 12 or 12
    minutes = f":{t.tm_min:02d}" if t.tm_min else ""
    return f"{hour}{minutes} {'am' if t.tm_hour < 12 else 'pm'}"


def until(moment: float, now: float) -> str:
    """"1h 12m", "35m", "2d 4h"."""
    minutes = max(0, math.ceil((moment - now) / 60))
    days, hours, mins = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m" if mins else "<1m"


def when(moment: float | None, now: float) -> str:
    """When a window resets, briefly: how long to go within a day, the day and time within a
    week, the date beyond that."""
    if moment is None:
        return ""
    if moment - now < 86400:
        return "in " + until(moment, now)
    moment = _minute(moment)
    if moment - now < 6 * 86400:
        return time.strftime("%a ", time.localtime(moment)) + clock(moment)
    return time.strftime("%b %-d", time.localtime(moment))


def resets(moment: float | None, now: float) -> str:
    """The longer form: "resets 6:30 pm (in 1h 12m)", "resets Sat 10 pm"."""
    if moment is None:
        return ""
    if moment - now < 86400:
        return f"resets {clock(_minute(moment))} (in {until(moment, now)})"
    moment = _minute(moment)
    return f"resets {time.strftime('%a %b %-d', time.localtime(moment))}, {clock(moment)}"


def ago(moment: float, now: float) -> str:
    seconds = max(0.0, now - moment)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def as_of(moment: float, now: float) -> str:
    """"as of 9:33 am" today, "as of Mon 9:33 am" within the week, "as of Nov 9" before."""
    then, today = time.localtime(moment), time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return f"as of {clock(moment)}"
    if now - moment < 6 * 86400:
        return f"as of {time.strftime('%a', then)} {clock(moment)}"
    return f"as of {time.strftime('%b %-d', then)}"


def describe(usage: Usage, now: float) -> list[str]:
    """One line per limit, the way /usage puts it, and how old the numbers are: any much older
    than the rest says when it's from."""
    lines = []
    current = usage.at(now)
    for limit in current.limits:
        parts = [f"{percent_text(limit)} used"]
        if limit.detail:
            parts.append(limit.detail)
        if limit.resets_at is not None:
            parts.append(resets(limit.resets_at, now))
        if current.behind(limit):
            parts.append(as_of(limit.seen, now))
        lines.append(f"{limit.title}: {' · '.join(parts)}")
    lines.append(f"Updated {ago(usage.fetched_at, now)}" if not usage.is_stale(now) else
                 f"Updated {ago(usage.fetched_at, now)}: Claude Code refreshes it while a session runs")
    if any(current.behind(limit) for limit in current.limits):
        lines.append("Replies bring only the session and the week: run /usage in Claude Code for the rest")
    return lines


# MARK: Keeping up

class UsageReader:
    """Rereads each file only when it changes, and looks at most every couple of seconds. A
    file caught halfway through being written keeps the last good reading. Given a fetcher, it
    lets it know how old the numbers are as it looks, so it can ask Anthropic for new ones."""

    EVERY = 2.0

    def __init__(self, path: Path | None = None, live: Path | None = None,
                 fetcher: fetch.Fetcher | None = None):
        self._cache = _Watched(path or CLAUDE_JSON, lambda data, _: (parse(data), account(data)))
        self._status_line = live or STATUS_LINE
        self._live = _Watched(self._status_line, parse_live)
        self._fetched: _Watched | None = None
        self._hits = self._status_line.parent       # the hook's .limit sits beside it
        self._checked = -math.inf
        self._usage: Usage | None = None
        self.fetcher = fetcher

    def poll(self) -> Usage | None:
        now = time.monotonic()
        if now - self._checked >= UsageReader.EVERY:
            self._checked = now
            cached, who = self._cache.poll() or (None, None)
            # The account signed in can change under me, and with it the files to watch.
            path = live_file(self._status_line, who)
            if path != self._live.path:
                self._live = _Watched(path, parse_live)
            asked = fetched_file(self._status_line, who)
            if asked != (self._fetched.path if self._fetched else None):
                self._fetched = _Watched(asked, lambda data, _: from_cache(data, None)) if asked else None
            latest, at = self._live.poll(), time.time()
            cached = newest(cached, self._fetched.poll() if self._fetched else None)
            if self.fetcher is not None:
                self.fetcher.poll(who, asked, cached.fetched_at if cached else None,
                                  latest.fetched_at if latest else None,
                                  lambda who: still_signed_in(self._cache.path, who))
            self._usage = with_hit(merge(in_force(cached, latest, at), latest), read_limit(self._hits), at, who)
        return self._usage


class _Watched:
    def __init__(self, path: Path, parse_):
        self.path = path
        self.parse = parse_
        self.value = None
        self.stamp: tuple | None = None

    def poll(self):
        try:
            info = os.stat(self.path)
        except OSError:
            self.value = self.stamp = None
            return None
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self.stamp:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                return self.value           # mid-write: try again next time
            self.stamp = stamp
            self.value = self.parse(data, info.st_mtime)
        return self.value


# MARK: Pieces

def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _seconds(value) -> float | None:
    """A reset time, from an ISO 8601 string or a Unix time in seconds or milliseconds."""
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e11 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"[Zz]$", "+00:00", value.strip())
    # Python before 3.11 reads only three or six digits of fractional seconds.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _display_name(scope) -> str:
    name = scope.get("display_name") if isinstance(scope, dict) else None
    return name.strip() if isinstance(name, str) else ""


def _money(amount) -> str:
    """{"amount_minor": 1240, "currency": "USD", "exponent": 2} as "$12.40"."""
    if not isinstance(amount, dict):
        return ""
    minor, exponent = _number(amount.get("amount_minor")), _number(amount.get("exponent"))
    if minor is None or exponent is None or not 0 <= exponent <= 4:
        return ""
    currency = str(amount.get("currency") or "").upper()
    value = f"{minor / 10 ** int(exponent):,.{int(exponent)}f}"
    return f"${value}" if currency in ("USD", "") else f"{value} {currency}"
