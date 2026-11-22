"""Your usage limits straight from Anthropic, the way Claude Code's /usage asks for them.

Claude Code hears every limit with each reply, a model's week (Fable's, say) among them, but
saves only the session and the week where CTally can see them. A model's week is saved only
when /usage runs, so it can be hours old. With the "Exact usage" switch on (the default),
CTally asks the same place /usage does:

- As Claude Code's own sign-in to claude.ai: ~/.claude/.credentials.json on Linux, the
  "Claude Code-credentials" keychain item on macOS. CTally reads the access token there each
  time it asks and sends it only to api.anthropic.com. It never saves, prints or renews it:
  once it has run out, CTally waits for Claude Code to renew it.
- At most every five minutes, the time Claude Code itself trusts a copy for, and then only if
  Claude Code has replied since the last answer, or that answer is an hour old. Nothing
  waits on it: the app asks in the background, `ctally usage` asks when it's run.
- Sooner when a reply shows the limits were reset (less of the week used than the last answer
  said, say): a model's week in that answer is from before it. Then it asks at once, half a
  minute after the last ask at the soonest, and early like that once in five minutes.
- The answer goes in ~/.claude/ctally.d/.fetched/<account id>, in the shape ~/.claude.json
  keeps it in, and usage.py reads both, the newer winning.
- Told to slow down, it waits as long as it's told, or a quarter of an hour, and `ctally
  usage` run again meanwhile doesn't ask either. A token turned away isn't sent again.
- The account signed in can change under it (a tool that swaps accounts, say), and the
  sign-in a moment after the account's name: it doesn't ask for a few seconds after a change,
  and an answer that was on its way while the account changed isn't kept.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, NamedTuple

from . import __version__, system

URL = "https://api.anthropic.com/api/oauth/usage"
EVERY = 300             # as long as Claude Code trusts its own copy of the numbers
QUIET_EVERY = 3600      # with no replies in between, which is all that moves them
SLOW_DOWN = 900         # told to wait, without being told how long
SETTLE = 10             # after the account changes, for its sign-in to be in place as well
SOON = 30               # after the last ask, before asking again because of a reset
TIMEOUT = 10
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
KEYCHAIN = "Claude Code-credentials"


class SignIn(NamedTuple):
    token: str
    expires_at: float | None    # Unix time


class Refused(Exception):
    """An ask that didn't bring the numbers back, and how long to leave it, if that was said."""

    def __init__(self, why: str, status: int | None = None, wait: float | None = None):
        super().__init__(why)
        self.why, self.status, self.wait = why, status, wait


# MARK: The sign-in

def sign_in(claude_dir: Path | None = None) -> SignIn | None:
    """Claude Code's sign-in to claude.ai, if it has one that may read usage."""
    text = _keychain() if system.MAC else None
    if text is None:
        try:
            text = ((claude_dir or CLAUDE_DIR) / ".credentials.json").read_text()
        except OSError:
            return None
    try:
        found = json.loads(text)
    except ValueError:
        return None
    found = found.get("claudeAiOauth") if isinstance(found, dict) else None
    token = found.get("accessToken") if isinstance(found, dict) else None
    if not isinstance(token, str) or not token:
        return None
    scopes = found.get("scopes")
    if isinstance(scopes, list) and "user:profile" not in scopes:
        return None                     # a token for inference only: /usage needs the profile
    expires = found.get("expiresAt")
    expires = expires / 1000 if isinstance(expires, (int, float)) and not isinstance(expires, bool) else None
    return SignIn(token, expires)


def _keychain() -> str | None:
    """The keychain item Claude Code keeps its sign-in in on macOS, named as it names it."""
    secure = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    base = secure if secure is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    service = KEYCHAIN
    if base:
        service += "-" + hashlib.sha256(unicodedata.normalize("NFC", base).encode()).hexdigest()[:8]
    try:
        user = os.environ.get("USER") or getpass.getuser()
    except Exception:
        user = ""
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", user):
        user = "claude-code-user"
    try:
        done = subprocess.run(["security", "find-generic-password", "-a", user, "-s", service, "-w"],
                              capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


# MARK: Asking

def ask(token: str, timeout: float = TIMEOUT) -> dict:
    """What /usage gets: the same dictionary ~/.claude.json keeps as "utilization"."""
    request = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"ctally/{__version__}",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = json.loads(reply.read(4_000_000))
    except urllib.error.HTTPError as error:
        status = error.code
        why = {401: "the sign-in was turned away", 403: "the sign-in may not read usage",
               429: "asked too often"}.get(status, f"HTTP {status}")
        raise Refused(why, status, _seconds(error.headers.get("Retry-After") if error.headers else None)) from None
    except urllib.error.URLError as error:
        raise Refused(f"couldn't reach Anthropic ({error.reason})") from None
    except (OSError, ValueError) as error:
        raise Refused(f"couldn't read the answer ({error.__class__.__name__})") from None
    if not isinstance(body, dict):
        raise Refused("the answer wasn't usage")
    if "error" in body:
        error = body["error"] if isinstance(body["error"], dict) else {}
        kind = str(error.get("type") or "an error")
        raise Refused(f"the answer was {kind}", 429 if kind == "rate_limit_error" else None)
    return body


def _seconds(value) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def save(path: Path, utilization: dict, who: str, now: float) -> None:
    """Kept as ~/.claude.json keeps it, so the one reader reads both."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.new")
    temp.write_text(json.dumps({"fetchedAtMs": round(now * 1000), "accountUuid": who, "utilization": utilization}))
    os.replace(temp, path)


def wait_file(path: Path) -> Path:
    """Beside an account's answer: until when `ctally usage` was told to wait, and why. The app
    remembers that itself; a command run again wouldn't."""
    return path.with_name(f".{path.name}.wait")


def told_to_wait(path: Path, now: float) -> str | None:
    """Why not to ask yet, if the last ask from the shell was told to wait and that isn't over."""
    try:
        until, _, why = wait_file(path).read_text().strip().partition(" ")
        return (why or "asked too often") if float(until) > now else None
    except (OSError, ValueError):
        return None


def note_wait(path: Path, until: float, why: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.wait.new")
        temp.write_text(f"{round(until)} {why}\n")
        os.replace(temp, wait_file(path))
    except OSError:
        pass


# MARK: Keeping up

class Fetcher:
    """Asks when it's time, on a thread of its own, and saves what it hears; usage.UsageReader
    calls poll() as it reads, and picks the answer up from the file."""

    def __init__(self, sign_in_: Callable[[], SignIn | None] | None = None,
                 ask_: Callable[[str], dict] | None = None, log: Callable[[str], None] | None = None):
        self._sign_in, self._ask = sign_in_, ask_     # sign_in() and ask(), unless told otherwise
        self._log = log or (lambda text: print(f"ctally usage: {text}", file=sys.stderr, flush=True))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctally-usage")
        self._asking: Future | None = None
        self._not_before = 0.0
        self._held = 0.0                        # told to slow down, or the account changed: not before then
        self._tried = -math.inf                 # when it last asked
        self._early = -math.inf                 # when it last asked early, for a reset
        self._turned_away: str | None = None    # a token refused, not to be sent again
        self._who: str | None = None            # the account at the last look,
        self._looked = False                    # if there has been one
        self._heard: dict[str, float] = {}      # when each account was last answered
        self.wait_until = 0.0                   # told to slow down: not before then
        self.problem: str | None = None         # why the last ask didn't work, if it didn't

    def due(self, have: float | None, replied: float | None, now: float, reset: bool = False) -> bool:
        """Whether to ask, given when the newest numbers are from (have) and when Claude Code
        last replied. `reset`: a reply since shows the limits were reset, so a model's week in
        them is old however new they are; that's asked about at once, if it hasn't been asked
        early in the last five minutes, and only half a minute after the last ask."""
        if reset and now >= self._held and now - self._tried >= SOON and now - self._early >= EVERY:
            return True
        if now < self._not_before:
            return False
        if have is None:
            return True
        age = now - have
        return age >= EVERY and ((replied is not None and replied > have) or age >= QUIET_EVERY)

    def poll(self, who: str | None, path: Path | None, have: float | None, replied: float | None,
             still: Callable[[str], bool] | None = None, reset: bool = False) -> None:
        now = time.time()
        if who != self._who or not self._looked:
            # Another account than a moment ago: its sign-in may not be in place yet, and the
            # one still there would bring the other account's numbers.
            if self._looked:
                self._hold(now + SETTLE)
            self._who, self._looked = who, True
        if who is None or path is None or (self._asking is not None and not self._asking.done()):
            return
        # An answer with no limits in it leaves nothing to read, and is an answer all the same.
        heard = self._heard.get(who)
        if heard is not None and (have is None or heard > have):
            have = heard
        if self.due(have, replied, now, reset):
            if now < self._not_before:
                self._early = now               # ahead of time, for a reset
            self._tried = now
            self._not_before = now + EVERY      # however it goes, not again straight away
            self._asking = self._pool.submit(self.once, who, path, still)

    def _hold(self, until: float) -> None:
        """Not before then, a reset or not."""
        self._held = max(self._held, until)
        self._not_before = max(self._not_before, until)

    def once(self, who: str, path: Path, still: Callable[[str], bool] | None = None) -> bool:
        """Asks now, and saves the answer; False, and why in .problem, if it didn't work out.
        still(who) says whether that account is still the one signed in."""
        found = (self._sign_in or sign_in)()
        if found is None:
            return self._failed("no claude.ai sign-in for Claude Code to ask with")
        if found.expires_at is not None and found.expires_at < time.time() + 60:
            return self._failed("Claude Code's sign-in has run out: it renews it on its next request")
        if found.token == self._turned_away:
            return False
        try:
            answer = (self._ask or ask)(found.token)
        except Refused as refusal:
            if refusal.status in (401, 403):
                self._turned_away = found.token
            if refusal.status == 429:
                self._not_before = self.wait_until = time.time() + (refusal.wait or SLOW_DOWN)
                self._held = max(self._held, self.wait_until)
            return self._failed(refusal.why)
        except Exception as error:      # whatever goes wrong, the app carries on
            return self._failed(f"couldn't ask ({error.__class__.__name__})")
        if still is not None and not still(who):
            # Whose numbers these are can't be told: the sign-in was read a moment after the
            # account's name, and one of the two has changed.
            self._not_before = time.time() + SETTLE
            self._held = max(self._held, self._not_before)
            return self._failed("the account signed in changed while asking")
        try:
            save(path, answer, who, time.time())
        except OSError as error:
            return self._failed(f"couldn't save the answer ({error.strerror})")
        self._heard[who] = time.time()
        self.problem = None
        return True

    def _failed(self, why: str) -> bool:
        if why != self.problem:
            self._log(why)
        self.problem = why
        return False
