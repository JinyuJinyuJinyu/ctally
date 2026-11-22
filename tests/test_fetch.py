"""Asking Anthropic for the usage limits, as /usage does: Claude Code's sign-in, the request,
when to ask and when to hold off, and the answer winning over Claude Code's older copy. The
only server reached is one of the test's own, on this machine."""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ctally import cli, fetch, usage
from ctally.fetch import EVERY, QUIET_EVERY, Fetcher, Refused, SignIn
from ctally.usage import UsageReader

ACCOUNT = "00000000-0000-4000-8000-000000000001"
RESETS = "2099-01-03T22:00:00Z"
KEYCHAIN = fetch._keychain          # the real one: the test setup puts a stand-in in its place


def utilization(week: float, fable: float) -> dict:
    return {"five_hour": None, "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": week, "resets_at": RESETS, "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": fable, "resets_at": RESETS,
         "scope": {"model": {"id": None, "display_name": "Fable"}}}]}


def claude_json(home, week=64, fable=88, age=14 * 3600) -> None:
    """~/.claude.json with a /usage from `age` seconds ago."""
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": ACCOUNT},
        "cachedUsageUtilization": {"fetchedAtMs": (time.time() - age) * 1000, "accountUuid": ACCOUNT,
                                   "utilization": utilization(week, fable)}}))


def credentials(folder, token="token-1", expires_in=3600, scopes=("user:inference", "user:profile")) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "never-used", "expiresAt": (time.time() + expires_in) * 1000,
        "scopes": list(scopes), "subscriptionType": "max"}}))


def percents(found) -> list:
    return [(limit.short, limit.percent) for limit in found.limits]


# MARK: The sign-in

def test_the_sign_in_is_claude_codes(tmp_path):
    assert fetch.sign_in(tmp_path) is None                         # signed out, or an API key
    credentials(tmp_path, token="abc", expires_in=600)
    found = fetch.sign_in(tmp_path)
    assert found.token == "abc" and abs(found.expires_at - (time.time() + 600)) < 5
    credentials(tmp_path, scopes=("user:inference",))
    assert fetch.sign_in(tmp_path) is None                         # may not read usage
    (tmp_path / ".credentials.json").write_text("{not json")
    assert fetch.sign_in(tmp_path) is None


def test_the_keychain_item_is_named_as_claude_code_names_it(monkeypatch):
    """macOS: the default item, or one per config folder, as Claude Code 2.1 names them."""
    import hashlib
    calls = []

    def run(command, **_):
        calls.append(command)
        return type("Done", (), {"returncode": 0, "stdout": '{"claudeAiOauth": {}}\n'})()
    monkeypatch.setattr(fetch.subprocess, "run", run)
    monkeypatch.setenv("USER", "someone")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    assert KEYCHAIN() == '{"claudeAiOauth": {}}'
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/someone/.claude-work")
    KEYCHAIN()
    assert [c[c.index("-s") + 1] for c in calls] == [
        "Claude Code-credentials",
        "Claude Code-credentials-" + hashlib.sha256(b"/Users/someone/.claude-work").hexdigest()[:8]]
    assert all(c[:2] == ["security", "find-generic-password"] and c[c.index("-a") + 1] == "someone" for c in calls)


# MARK: Asking

class Anthropic:
    """A stand-in for the usage endpoint, on this machine: answers each GET with the next of
    `replies` (status, headers, body), and keeps the headers it was sent."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, self.headers))     # names in any case, as HTTP has them
                status, headers, body = outer.replies.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/oauth/usage"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def anthropic(monkeypatch):
    servers = []

    def start(*replies):
        server = Anthropic(replies)
        servers.append(server)
        monkeypatch.setattr(fetch, "URL", server.url)
        return server
    yield start
    for server in servers:
        server.close()


def test_asking_sends_the_sign_in_and_reads_the_answer(anthropic):
    server = anthropic((200, {}, utilization(31, 42)))
    assert fetch.ask("abc") == utilization(31, 42)
    path, headers = server.seen[0]
    assert path == "/api/oauth/usage"
    assert headers["Authorization"] == "Bearer abc"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["User-Agent"].startswith("ctally/")


def test_a_refusal_says_why_and_how_long_to_wait(anthropic):
    anthropic((429, {"Retry-After": "120"}, {"error": {"type": "rate_limit_error"}}),
              (429, {"Retry-After": "Wed, 18 Nov 2026 07:28:00 GMT"}, {"error": {"type": "rate_limit_error"}}),
              (401, {}, {"error": {"type": "authentication_error"}}),
              (403, {}, {"error": {"type": "permission_error"}}),
              (200, {}, {"error": {"type": "rate_limit_error"}}),
              (200, {}, ["not", "usage"]))
    with pytest.raises(Refused) as told:
        fetch.ask("abc")
    assert (told.value.status, told.value.wait, told.value.why) == (429, 120.0, "asked too often")
    with pytest.raises(Refused) as told:
        fetch.ask("abc")
    assert (told.value.status, told.value.wait) == (429, None)     # a date: the fetcher waits its own while
    with pytest.raises(Refused) as told:
        fetch.ask("abc")
    assert told.value.status == 401
    with pytest.raises(Refused) as told:
        fetch.ask("abc")
    assert (told.value.status, told.value.why) == (403, "the sign-in may not read usage")
    with pytest.raises(Refused) as told:
        fetch.ask("abc")
    assert told.value.status == 429                                 # said in the answer instead
    with pytest.raises(Refused):
        fetch.ask("abc")


def test_offline_or_signed_out_it_says_so(monkeypatch, tmp_path):
    with socket.socket() as taken:                                  # a port nothing listens on
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    monkeypatch.setattr(fetch, "URL", f"http://127.0.0.1:{port}/api/oauth/usage")
    with pytest.raises(Refused) as told:
        fetch.ask("abc", timeout=5)
    assert told.value.why.startswith("couldn't reach Anthropic (") and told.value.status is None

    said = []
    fetcher = Fetcher(lambda: None, log=said.append)
    assert not fetcher.once(ACCOUNT, tmp_path / "fetched") and not (tmp_path / "fetched").exists()
    assert fetcher.problem == said[0] == "no claude.ai sign-in for Claude Code to ask with"


# MARK: When to ask

def test_asks_when_the_numbers_are_old_and_claude_code_has_replied_since():
    fetcher, now = Fetcher(), 1_000_000.0
    assert fetcher.due(None, None, now)                                     # nothing yet
    assert not fetcher.due(now - EVERY + 5, now - 1, now)                   # still new
    assert fetcher.due(now - EVERY - 5, now - 1, now)                       # old, and replies since
    assert not fetcher.due(now - EVERY - 5, now - EVERY - 10, now)          # old, but nothing's moved
    assert fetcher.due(now - QUIET_EVERY - 5, None, now)                    # an hour on, ask anyway


def test_a_reset_is_told_by_the_week():
    """A week only fills up until it ends: less of it used than the last answer said, by a
    reply since, or the week ending later, means the weeks were reset."""
    from ctally.usage import Limit, Usage, reset_since

    def week(percent, resets=2_000_000.0):
        return Limit(("week",), "Current week (all models)", "Week", "7d", percent, resets)

    def session(percent):
        return Limit(("session",), "Current session", "Session", "5h", percent, 1_500_000.0)
    answer = Usage((session(70), week(65)), 1000.0)
    assert reset_since(answer, Usage((week(15),), 1100.0))
    assert reset_since(Usage((week(3),), 1000.0), Usage((week(0, 2_600_000.0),), 1100.0))   # little used, a new end
    assert not reset_since(answer, Usage((week(62),), 1100.0))                 # a reply a moment behind
    assert not reset_since(answer, Usage((week(65, 2_000_001.0),), 1100.0))    # its end as given, to the second
    assert not reset_since(answer, Usage((session(40), week(65)), 1100.0))     # a long reply's session: not the week
    assert not reset_since(answer, Usage((week(15),), 900.0))                  # from before the answer
    assert not reset_since(Usage((week(65, 1050.0),), 1000.0), Usage((week(15),), 1100.0))  # a new week
    assert not reset_since(None, Usage((week(15),), 1100.0)) and not reset_since(answer, None)


def test_a_week_from_before_a_reset_says_so_and_the_rest_dont():
    """Only the weeks go with a reset: credits, say, aren't dimmed. A limit hit noted on top
    keeps the reset as well."""
    from ctally.sessions import LimitHit
    from ctally.usage import Limit, Usage, describe, merge, with_hit
    end = time.time() + 3 * 86400
    answer = Usage((Limit(("week",), "Current week (all models)", "Week", "7d", 65, end, seen=1000.0),
                    Limit(("week", "fable"), "Current week (Fable)", "Week · Fable", "", 99, end, seen=1000.0),
                    Limit(("credits",), "Usage credits", "Credits", "", 20, None, detail="$10.00 of $50.00",
                          seen=1000.0)), 1000.0)
    reply = Usage((Limit(("week",), "Current week (all models)", "Week", "7d", 15, end, seen=1100.0),), 1100.0)
    found = merge(answer, reply)
    assert [(l.short, l.percent, found.behind(l), found.before_reset(l)) for l in found.limits] == [
        ("Week", 15, False, False), ("Week · Fable", 99, True, True), ("Credits", 20, False, False)]
    assert "Current week (Fable): 99% used · resets" in "\n".join(describe(found, 1200.0))
    assert describe(found, 1200.0)[1].endswith("before a reset")
    hit = with_hit(found, LimitHit(end - 3600, "You've hit your session limit", 1150.0), 1200.0)
    fable = next(limit for limit in hit.limits if limit.key == ("week", "fable"))
    assert ("session",) in [limit.key for limit in hit.limits] and hit.before_reset(fable)


class Asks:
    """Stands in for sign_in() and ask(): one sign-in, and one answer or refusal per ask."""

    def __init__(self, *answers, token="token-1", expires_in=3600):
        self.answers, self.tokens = list(answers), []
        self.signed_in = SignIn(token, time.time() + expires_in)

    def sign_in(self):
        return self.signed_in

    def ask(self, token):
        self.tokens.append(token)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_the_answer_is_saved_and_beats_claude_codes_older_copy(own_usage_and_prefs):
    home = own_usage_and_prefs
    claude_json(home)                                  # Fable 88%, as of 14 hours ago
    assert percents(usage.read()) == [("Week", 64), ("Week · Fable", 88)]
    asks = Asks(utilization(31, 42))
    fetcher = Fetcher(asks.sign_in, asks.ask, log=lambda _: None)
    assert fetcher.once(ACCOUNT, usage.fetched_file(usage.STATUS_LINE, ACCOUNT))
    found = usage.read()
    assert percents(found) == [("Week", 31), ("Week · Fable", 42)]
    assert time.time() - found.fetched_at < 5 and not any(found.behind(limit) for limit in found.limits)
    claude_json(home, week=33, fable=45, age=-1)       # a /usage a second later: the newer wins
    assert percents(usage.read()) == [("Week", 33), ("Week · Fable", 45)]


def test_an_answer_for_another_account_is_not_shown(own_usage_and_prefs):
    home = own_usage_and_prefs
    claude_json(home)
    fetch.save(usage.fetched_file(usage.STATUS_LINE, "someone-else"), utilization(5, 5), "someone-else", time.time())
    assert percents(usage.read()) == [("Week", 64), ("Week · Fable", 88)]


def test_turned_away_it_waits_for_a_new_sign_in_and_told_to_wait_it_waits(own_usage_and_prefs):
    target = usage.fetched_file(usage.STATUS_LINE, ACCOUNT)
    said = []
    asks = Asks(Refused("the sign-in was turned away", 401), utilization(31, 42))
    fetcher = Fetcher(asks.sign_in, asks.ask, log=said.append)
    assert not fetcher.once(ACCOUNT, target) and fetcher.problem == "the sign-in was turned away"
    assert not fetcher.once(ACCOUNT, target) and asks.tokens == ["token-1"]     # not sent again
    asks.signed_in = SignIn("token-2", time.time() + 3600)                     # Claude Code renewed it
    assert fetcher.once(ACCOUNT, target) and asks.tokens == ["token-1", "token-2"]
    assert fetcher.problem is None and said == ["the sign-in was turned away"]

    asks.answers = [Refused("asked too often", 429, wait=1200)]
    assert not fetcher.once(ACCOUNT, target)
    assert not fetcher.due(None, None, time.time() + 1100) and fetcher.due(None, None, time.time() + 1300)

    expired = Asks(utilization(1, 1), expires_in=-10)
    fetcher = Fetcher(expired.sign_in, expired.ask, log=lambda _: None)
    assert not fetcher.once(ACCOUNT, target) and expired.tokens == []           # left to Claude Code
    assert "run out" in fetcher.problem


def test_after_the_account_changes_it_waits_for_the_sign_in_to_follow(tmp_path):
    """A tool that swaps accounts changes the name in ~/.claude.json and the sign-in one after
    the other: asked in between, the first account's numbers would be kept as the second's."""
    asks = Asks(utilization(1, 1), utilization(2, 2))
    fetcher = Fetcher(asks.sign_in, asks.ask, log=lambda _: None)
    fetcher.poll("account-a", tmp_path / "a", None, None)       # the first look: nothing to wait for
    fetcher._asking.result(timeout=5)
    assert asks.tokens == ["token-1"]
    fetcher._not_before = 0.0                                   # five minutes on
    fetcher.poll("account-b", tmp_path / "b", None, None)
    assert asks.tokens == ["token-1"] and not (tmp_path / "b").exists()
    now = time.time()
    assert not fetcher.due(None, None, now + fetch.SETTLE - 2) and fetcher.due(None, None, now + fetch.SETTLE + 1)
    fetcher._not_before = 0.0                                   # ten seconds on
    fetcher.poll("account-b", tmp_path / "b", None, None)
    fetcher._asking.result(timeout=5)
    assert asks.tokens == ["token-1", "token-1"] and (tmp_path / "b").exists()


def test_an_answer_on_its_way_while_the_account_changed_is_not_kept(tmp_path):
    asks = Asks(utilization(1, 1), utilization(2, 2))
    said = []
    fetcher = Fetcher(asks.sign_in, asks.ask, log=said.append)
    assert not fetcher.once("account-a", tmp_path / "a", lambda who: False)
    assert not (tmp_path / "a").exists() and said == ["the account signed in changed while asking"]
    assert not fetcher.due(None, None, time.time() + fetch.SETTLE - 2)
    assert fetcher.once("account-a", tmp_path / "a", lambda who: who == "account-a") and (tmp_path / "a").exists()

    signed_in = tmp_path / ".claude.json"
    assert usage.still_signed_in(signed_in, "account-a")                # nothing there to say it isn't
    signed_in.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a"}}))
    assert usage.still_signed_in(signed_in, "account-a") and not usage.still_signed_in(signed_in, "account-b")
    signed_in.write_text('{"oauthAccount": {"accountU')                 # caught halfway through being written
    assert usage.still_signed_in(signed_in, "account-b")


def test_an_answer_with_no_limits_in_it_is_an_answer_all_the_same(tmp_path):
    """A plan without them leaves nothing to read, which isn't a reason to ask every five minutes."""
    asks = Asks({}, {})
    fetcher = Fetcher(asks.sign_in, asks.ask, log=lambda _: None)
    fetcher.poll(ACCOUNT, tmp_path / "f", None, None)
    fetcher._asking.result(timeout=5)
    fetcher._not_before = 0.0
    fetcher._heard[ACCOUNT] -= EVERY + 60                       # six minutes on, and no replies
    fetcher.poll(ACCOUNT, tmp_path / "f", None, None)
    assert asks.tokens == ["token-1"]
    fetcher.poll(ACCOUNT, tmp_path / "f", None, time.time())    # Claude Code has replied since
    fetcher._asking.result(timeout=5)
    assert asks.tokens == ["token-1", "token-1"]


def test_the_reader_asks_in_the_background_and_shows_the_answer(own_usage_and_prefs, monkeypatch):
    home = own_usage_and_prefs
    claude_json(home)
    (home / ".statusline").write_text(json.dumps({"rate_limits": {
        "seven_day": {"used_percentage": 31, "resets_at": 4070939400}}}))
    monkeypatch.setattr(UsageReader, "EVERY", 0.0)
    asks = Asks(utilization(31, 42))
    reader = UsageReader(home / ".claude.json", home / ".statusline",
                         fetcher=Fetcher(asks.sign_in, asks.ask, log=lambda _: None))
    first = reader.poll()
    assert [(l.short, l.percent, first.behind(l)) for l in first.limits] == [
        ("Week", 31, False), ("Week · Fable", 88, True)]
    reader.fetcher._asking.result(timeout=5)
    later = reader.poll()
    assert [(l.short, l.percent, later.behind(l)) for l in later.limits] == [
        ("Week", 31, False), ("Week · Fable", 42, False)]
    reader.poll()
    assert asks.tokens == ["token-1"]                  # once is enough for five minutes


def test_a_reset_shown_by_a_reply_is_asked_about_at_once(own_usage_and_prefs, monkeypatch):
    """The limits are reset: the next reply has the week at 15% where the answer a minute before
    had 65%, and that answer's 99% of Fable's week is from before the reset. It's shown as that
    old, and Anthropic is asked at once rather than five minutes on."""
    home = own_usage_and_prefs
    claude_json(home, week=65, fable=99, age=60)
    monkeypatch.setattr(UsageReader, "EVERY", 0.0)
    asks = Asks(utilization(15, 0))
    reader = UsageReader(home / ".claude.json", home / ".statusline",
                         fetcher=Fetcher(asks.sign_in, asks.ask, log=lambda _: None))
    first = reader.poll()
    assert percents(first) == [("Week", 65), ("Week · Fable", 99)] and asks.tokens == []
    (home / ".statusline").write_text(json.dumps({"rate_limits": {
        "seven_day": {"used_percentage": 15, "resets_at": 4070939400}}}))
    reset = reader.poll()
    assert [(l.short, l.percent, reset.behind(l)) for l in reset.limits] == [
        ("Week", 15, False), ("Week · Fable", 99, True)]
    reader.fetcher._asking.result(timeout=5)
    later = reader.poll()
    assert [(l.short, l.percent, later.behind(l)) for l in later.limits] == [
        ("Week", 15, False), ("Week · Fable", 0, False)]
    assert asks.tokens == ["token-1"]


def test_a_reset_is_asked_about_once_in_five_minutes_and_never_while_told_to_wait(tmp_path):
    asks = Asks(utilization(65, 99), utilization(15, 0), utilization(15, 0))
    fetcher = Fetcher(asks.sign_in, asks.ask, log=lambda _: None)
    path = tmp_path / "f"
    fetcher.poll(ACCOUNT, path, None, None)
    fetcher._asking.result(timeout=5)
    have = time.time()
    fetcher.poll(ACCOUNT, path, have, time.time(), reset=True)
    assert len(asks.tokens) == 1                                # just asked: half a minute first
    fetcher._tried -= fetch.SOON                                # half a minute on
    fetcher.poll(ACCOUNT, path, have, time.time(), reset=True)
    fetcher._asking.result(timeout=5)
    assert len(asks.tokens) == 2
    fetcher._tried -= fetch.SOON                                # a reset again, but it's asked early once
    fetcher.poll(ACCOUNT, path, have, time.time(), reset=True)
    assert len(asks.tokens) == 2
    fetcher._tried, fetcher._early = -EVERY, -EVERY             # five minutes on, told to wait
    fetcher._hold(time.time() + 60)
    assert not fetcher.due(have, time.time(), time.time(), reset=True)


def test_while_asking_fails_a_reset_is_asked_about_once_in_five_minutes(tmp_path):
    """Each ask about it counts, the regular one as well, so failing asks don't come in twos."""
    down = Refused("couldn't reach Anthropic")
    asks = Asks(down, down, down)
    fetcher = Fetcher(asks.sign_in, asks.ask, log=lambda _: None)
    path, have = tmp_path / "f", time.time() - 60

    def later(seconds):
        fetcher._tried -= seconds
        fetcher._early -= seconds
        fetcher._not_before -= seconds

    def look():
        fetcher.poll(ACCOUNT, path, have, time.time(), reset=True)
        if fetcher._asking is not None:
            fetcher._asking.result(timeout=5)
    look()
    assert len(asks.tokens) == 1
    later(EVERY)
    look()
    assert len(asks.tokens) == 2
    later(fetch.SOON)
    look()
    assert len(asks.tokens) == 2


# MARK: ctally usage

def test_ctally_usage_asks_at_once_after_a_reset(own_usage_and_prefs, monkeypatch, capsys):
    home = own_usage_and_prefs
    claude_json(home, week=65, fable=99, age=60)
    asks = Asks(Refused("HTTP 500", 500), utilization(15, 0))
    monkeypatch.setattr(fetch, "sign_in", asks.sign_in)
    monkeypatch.setattr(fetch, "ask", asks.ask)
    assert cli.main(["usage"]) == 0
    assert "99%" in capsys.readouterr().out and asks.tokens == []          # a minute old: no need
    usage.STATUS_LINE.write_text(json.dumps({"rate_limits": {
        "seven_day": {"used_percentage": 15, "resets_at": 4070939400}}}))
    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out                                          # it couldn't: says how old
    assert "Current week (Fable)       ██████████  99%  resets" in out and "before a reset" in out
    assert asks.tokens == ["token-1"]
    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "Current week (Fable)       ░░░░░░░░░░   0%" in out and asks.tokens == ["token-1", "token-1"]

def test_ctally_usage_asks_first_unless_switched_off(own_usage_and_prefs, monkeypatch, capsys):
    from ctally.prefs import Prefs
    home = own_usage_and_prefs
    claude_json(home)
    asks = Asks(Refused("asked too often", 429), utilization(31, 42))
    monkeypatch.setattr(fetch, "sign_in", asks.sign_in)
    monkeypatch.setattr(fetch, "ask", asks.ask)

    assert cli.main(["usage"]) == 0
    out, err = capsys.readouterr()
    assert "88%" in out and "couldn't ask Anthropic for the latest (asked too often)" in err
    # Told to wait, it does, run again or not: a command doesn't remember, so a file does.
    target = usage.fetched_file(usage.STATUS_LINE, ACCOUNT)
    assert cli.main(["usage"]) == 0
    out, err = capsys.readouterr()
    assert "88%" in out and "(asked too often)" in err and asks.tokens == ["token-1"]
    until, why = fetch.wait_file(target).read_text().split(None, 1)
    assert fetch.SLOW_DOWN - 5 < int(until) - time.time() <= fetch.SLOW_DOWN + 1 and why == "asked too often\n"
    fetch.note_wait(target, time.time() - 1, "asked too often")         # a quarter of an hour on
    assert cli.main(["usage"]) == 0
    out, err = capsys.readouterr()
    assert "Current week (Fable)       ████░░░░░░  42%" in out and err == ""
    assert asks.tokens == ["token-1", "token-1"] and not fetch.wait_file(target).exists()
    (usage.fetched_file(usage.STATUS_LINE, ACCOUNT)).unlink()
    Prefs()["exact_usage"] = False
    assert cli.main(["usage"]) == 0
    assert "88%" in capsys.readouterr().out and asks.tokens == ["token-1", "token-1"]
