"""Reading the plan's usage limits: Claude Code's cached /usage numbers in ~/.claude.json, the
status line's fresher session and week, and putting them into words."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from ctally import usage
from ctally.usage import Limit, Usage, UsageReader, merge, parse, parse_live, with_hit

ACCOUNT = "00000000-0000-4000-8000-000000000001"
FETCHED = 1_791_352_784_726            # ms
SESSION_RESET = "2026-10-07T08:30:00.491511+00:00"
WEEK_RESET = "2026-10-10T11:00:00.491531+00:00"


def epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def window(utilization, resets_at):
    return {"utilization": utilization, "resets_at": resets_at, "limit_dollars": None, "used_dollars": None}


def cached(**utilization) -> dict:
    """~/.claude.json as Claude Code 2.1 leaves it, trimmed to what matters here."""
    base = {
        "five_hour": window(71, SESSION_RESET),
        "seven_day": window(49, WEEK_RESET),
        "seven_day_oauth_apps": None, "seven_day_opus": None, "seven_day_sonnet": None,
        "iguana_necktie": {"utilization": 0, "resets_at": "2026-11-05T07:59:00+00:00", "limit_dollars": 250},
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
        "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2}, "limit": None, "percent": 0,
                  "severity": "normal", "enabled": False},
        "limits": [
            {"kind": "session", "group": "session", "percent": 71, "severity": "normal",
             "resets_at": SESSION_RESET, "scope": None, "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 49, "severity": "normal",
             "resets_at": WEEK_RESET, "scope": None, "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
             "resets_at": "2026-10-10T11:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False},
        ],
    }
    base.update(utilization)
    return {
        "numStartups": 80,
        "oauthAccount": {"accountUuid": ACCOUNT, "emailAddress": "someone@example.com"},
        "cachedUsageUtilization": {"fetchedAtMs": FETCHED, "accountUuid": ACCOUNT, "utilization": base},
    }


@pytest.fixture
def utc(monkeypatch):
    """Local time is UTC, so the words about times come out the same everywhere."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# MARK: Reading the cache

def test_every_limit_the_way_usage_names_them():
    found = parse(cached())
    assert found.fetched_at == FETCHED / 1000
    assert [(l.title, l.short, l.tag, l.percent) for l in found.limits] == [
        ("Current session", "Session", "5h", 71),
        ("Current week (all models)", "Week", "7d", 49),
        ("Current week (Fable)", "Week · Fable", "", 0),
    ]
    assert found.limits[0].resets_at == pytest.approx(epoch(SESSION_RESET))
    assert [l.title for l in found.compact] == ["Current session", "Current week (all models)"]


def test_codenamed_windows_and_switched_off_credits_stay_out():
    titles = [l.title for l in parse(cached()).limits]
    assert not any("iguana" in t.lower() or "credit" in t.lower() for t in titles)


def test_without_the_list_the_window_fields_do():
    found = parse(cached(limits=None, seven_day_sonnet=window(12.5, WEEK_RESET),
                         seven_day_opus=window(3, WEEK_RESET)))
    assert [(l.title, l.percent) for l in found.limits] == [
        ("Current session", 71), ("Current week (all models)", 49),
        ("Current week (Sonnet only)", 12.5), ("Current week (Opus only)", 3),
    ]


def test_the_list_wins_over_the_window_fields():
    data = cached(five_hour=window(10, SESSION_RESET))
    assert parse(data).limits[0].percent == 71


def test_a_model_listed_both_ways_shows_once():
    data = cached(seven_day_sonnet=window(20, WEEK_RESET))
    data["cachedUsageUtilization"]["utilization"]["limits"].append(
        {"kind": "weekly_scoped", "group": "weekly", "percent": 21, "resets_at": WEEK_RESET,
         "scope": {"model": {"display_name": "Sonnet"}}})
    sonnet = [l for l in parse(data).limits if "Sonnet" in l.title]
    assert [(l.title, l.percent) for l in sonnet] == [("Current week (Sonnet)", 21)]


def test_unknown_kinds_need_a_name_and_a_period():
    data = cached()
    data["cachedUsageUtilization"]["utilization"]["limits"] += [
        {"kind": "mystery", "group": "fortnightly", "percent": 5, "scope": {"model": {"display_name": "X"}}},
        {"kind": "mystery", "group": "daily", "percent": 5},
        {"kind": "surface_daily", "group": "daily", "percent": 7, "resets_at": None,
         "scope": {"surface": {"display_name": "Cowork"}}},
        "not even an object",
        {"kind": "session", "percent": "71"},
    ]
    titles = [l.title for l in parse(data).limits]
    assert titles == ["Current session", "Current week (all models)", "Current week (Fable)", "Current day (Cowork)"]


def test_credits_when_switched_on():
    spend = {"used": {"amount_minor": 1240, "currency": "USD", "exponent": 2},
             "limit": {"amount_minor": 5000, "currency": "USD", "exponent": 2},
             "percent": 24.8, "severity": "normal", "enabled": True}
    credits = parse(cached(spend=spend)).limits[-1]
    assert (credits.title, credits.percent, credits.detail, credits.resets_at) == (
        "Usage credits", 24.8, "$12.40 of $50.00", None)
    extra = {"is_enabled": True, "monthly_limit": 5000, "used_credits": 600, "utilization": 12}
    credits = parse(cached(extra_usage=extra)).limits[-1]
    assert (credits.title, credits.percent, credits.detail) == ("Usage credits", 12, "")


def test_another_accounts_numbers_are_not_yours():
    data = cached()
    data["oauthAccount"]["accountUuid"] = "00000000-0000-4000-8000-000000000002"
    assert parse(data) is None
    del data["oauthAccount"]                     # signed out: nothing to compare with
    assert parse(data) is not None


@pytest.mark.parametrize("config", [
    {}, [], "text", {"cachedUsageUtilization": None},
    {"cachedUsageUtilization": {"fetchedAtMs": FETCHED}},
    {"cachedUsageUtilization": {"utilization": {"five_hour": window(1, SESSION_RESET)}}},
    {"cachedUsageUtilization": {"fetchedAtMs": FETCHED, "utilization": {"five_hour": None, "limits": []}}},
])
def test_nothing_usable_is_none(config):
    assert parse(config) is None


def test_read_copes_with_no_file_and_bad_json(tmp_path):
    assert usage.read(tmp_path / "missing.json", tmp_path / "missing") is None
    (tmp_path / "bad.json").write_text('{"cachedUsageUtil')
    assert usage.read(tmp_path / "bad.json", tmp_path / "missing") is None


@pytest.mark.parametrize("text, expected", [
    ("2026-10-07T08:30:00.491511+00:00", 1791361800.491511),
    ("2026-10-07T08:30:00Z", 1791361800),
    ("2026-10-07T08:30:00.5Z", 1791361800.5),
    ("2026-10-07T08:30:00.123456789+00:00", 1791361800.123456),
    ("2026-10-07T18:30:00+10:00", 1791361800),
    ("2026-10-07T08:30:00", 1791361800),                 # no zone: UTC
    (1791361800, 1791361800), (1791361800000, 1791361800),
    ("soon", None), ("", None), (None, None), (True, None),
])
def test_reset_times(text, expected):
    found = usage._seconds(text)
    assert found == (pytest.approx(expected) if expected is not None else None)


# MARK: Levels and resets

@pytest.mark.parametrize("percent, severity, level", [
    (0, "normal", 0), (74.9, "normal", 0), (75, "normal", 1), (89, "normal", 1), (90, "normal", 2),
    (10, "warning", 1), (10, "critical", 2), (95, "warning", 2), (10, "whatever", 0),
])
def test_levels(percent, severity, level):
    assert Limit(("session",), "", "", "", percent, None, severity).level == level


def test_a_window_past_its_reset_starts_from_nothing():
    found = parse(cached())
    after = found.at(epoch(SESSION_RESET) + 1)
    assert [(l.percent, l.resets_at) for l in after.limits][0] == (0.0, None)
    assert after.limits[1].percent == 49                    # the week hasn't reset yet
    assert found.at(epoch(SESSION_RESET) - 1).limits[0].percent == 71


def test_stale_after_an_hour():
    found = parse(cached())
    assert not found.is_stale(found.fetched_at + 3599)
    assert found.is_stale(found.fetched_at + 3601)


# MARK: The status line's session and week

def status_line(five=79, week=50, **extra) -> dict:
    return {"session_id": "abc", "model": {"id": "claude-haiku"}, **extra, "rate_limits": {
        "five_hour": {"used_percentage": five, "resets_at": 1791361800},
        "seven_day": {"used_percentage": week, "resets_at": 1791630000}}}


def test_status_line_limits():
    live = parse_live(status_line(), 1791353000)
    assert [(l.title, l.percent, l.resets_at) for l in live.limits] == [
        ("Current session", 79, 1791361800), ("Current week (all models)", 50, 1791630000)]
    assert live.fetched_at == 1791353000
    assert parse_live({"rate_limits": {"five_hour": None}}, 1) is None
    assert parse_live({"model": {}}, 1) is None
    assert parse_live([], 1) is None


def test_newer_status_line_wins_for_session_and_week():
    cache = parse(cached())
    merged = merge(cache, parse_live(status_line(), cache.fetched_at + 600))
    assert [(l.title, l.percent) for l in merged.limits] == [
        ("Current session", 79), ("Current week (all models)", 50), ("Current week (Fable)", 0)]
    assert merged.fetched_at == cache.fetched_at + 600
    assert merge(cache, parse_live(status_line(), cache.fetched_at - 600)) == cache
    assert merge(None, parse_live(status_line(), 5)).limits[0].percent == 79
    assert merge(cache, None) == cache
    assert merge(None, None) is None


def test_read_merges_both_files(tmp_path):
    claude_json, live = tmp_path / ".claude.json", tmp_path / ".statusline"
    claude_json.write_text(json.dumps(cached()))
    live.write_text(json.dumps(status_line(five=83)))
    os.utime(live, (FETCHED / 1000 + 60, FETCHED / 1000 + 60))
    assert [l.percent for l in usage.read(claude_json, live).limits] == [83, 50, 0]
    os.utime(live, (FETCHED / 1000 - 60, FETCHED / 1000 - 60))
    assert [l.percent for l in usage.read(claude_json, live).limits] == [71, 49, 0]


def test_reader_follows_changes_and_rides_out_half_written_files(tmp_path, monkeypatch):
    monkeypatch.setattr(UsageReader, "EVERY", 0)
    claude_json, live = tmp_path / ".claude.json", tmp_path / ".statusline"
    reader = UsageReader(claude_json, live)
    assert reader.poll() is None
    claude_json.write_text(json.dumps(cached()))
    assert reader.poll().limits[0].percent == 71
    claude_json.write_text('{"cachedUsageUtilization": {"fetch')            # caught mid-write
    assert reader.poll().limits[0].percent == 71
    data = cached()
    data["cachedUsageUtilization"]["utilization"]["limits"][0]["percent"] = 72
    claude_json.write_text(json.dumps(data))
    assert reader.poll().limits[0].percent == 72
    live.write_text(json.dumps(status_line(five=90)))
    assert reader.poll().limits[0].percent == 90
    claude_json.unlink()
    assert [l.percent for l in reader.poll().limits] == [90, 50]            # the status line alone
    live.unlink()
    assert reader.poll() is None


def test_reader_looks_only_every_so_often(tmp_path):
    claude_json = tmp_path / ".claude.json"
    reader = UsageReader(claude_json, tmp_path / ".statusline")
    assert reader.poll() is None
    claude_json.write_text(json.dumps(cached()))
    assert reader.poll() is None                     # within the couple of seconds
    reader._checked -= UsageReader.EVERY
    assert reader.poll() is not None


# MARK: A limit hit

def hit(resets_at=None, message="You've hit your session limit · resets 7:30pm (Australia/Sydney)", noted=1791360000):
    from ctally.sessions import LimitHit
    return LimitHit(resets_at, message, noted)


def test_a_hit_limit_is_used_up_whatever_the_numbers_said():
    live = parse_live(status_line(five=99.4), 1791359000)               # the last reply before the hit
    now = 1791360100
    full = with_hit(live, hit(1791361800), now)
    session = full.limits[0]
    assert (session.percent, session.resets_at, session.level, usage.percent_text(session)) == (100, 1791361800, 2, "100%")
    assert full.limits[1] == live.limits[1] and full.fetched_at == live.fetched_at
    assert with_hit(parse_live(status_line(five=109), 1), hit(1791361800), now).limits[0].percent == 109
    assert with_hit(live, hit(1791361800), 1791361800) == live          # it has reset
    weekly = with_hit(live, hit(1791630000, "You've hit your weekly limit"), now)
    assert [l.percent for l in weekly.limits] == [99.4, 100]
    assert with_hit(live, hit(1791361800, "You've hit your Fable limit"), now) == live
    assert with_hit(live, None, now) == live


def test_a_hit_without_a_reset_time_borrows_the_window_it_was_hit_in():
    live = parse_live(status_line(five=97), 1791359000)                  # resets 1791361800
    assert with_hit(live, hit(None, noted=1791361800 - 3600), 1791360100).limits[0].percent == 100
    stale = hit(None, noted=1791361800 - 6 * 3600)                       # hit in an earlier window
    assert with_hit(live, stale, 1791360100) == live


def test_a_hit_with_nothing_else_known():
    full = with_hit(None, hit(1791361800), 1791360100)
    assert [(l.title, l.percent, l.tag) for l in full.limits] == [("Current session", 100, "5h")]
    assert full.fetched_at == 1791360000
    assert with_hit(None, hit(None), 1791360100) is None


def test_read_and_the_reader_see_the_hook_note(tmp_path, monkeypatch):
    monkeypatch.setattr(UsageReader, "EVERY", 0)
    live = tmp_path / ".statusline"
    data = status_line(five=99)
    data["rate_limits"]["five_hour"]["resets_at"] = int(time.time()) + 3600
    live.write_text(json.dumps(data))
    reader = UsageReader(tmp_path / ".claude.json", live)
    assert reader.poll().limits[0].percent == 99
    (tmp_path / ".limit").write_text("0 You've hit your session limit · resets 7:30pm\n")
    assert reader.poll().limits[0].percent == 100
    assert usage.read(tmp_path / ".claude.json", live).limits[0].percent == 100


# MARK: Words

def test_percentages_round_down():
    assert usage.percent_text(Limit((), "", "", "", 99.6, None)) == "99%"
    assert usage.percent_text(Limit((), "", "", "", 0, None)) == "0%"


def test_how_long_to_go():
    assert usage.until(1000 + 72 * 60, 1000) == "1h 12m"
    assert usage.until(1000 + 3600, 1000) == "1h"
    assert usage.until(1000 + 35 * 60 - 20, 1000) == "35m"
    assert usage.until(1000 + 20, 1000) == "1m"
    assert usage.until(1000, 1000) == "<1m"
    assert usage.until(1000 + 52 * 3600, 1000) == "2d 4h"
    assert usage.until(1000 + 48 * 3600, 1000) == "2d"


def test_when_and_resets(utc):
    now = datetime(2026, 10, 7, 17, 5, tzinfo=timezone.utc).timestamp()
    soon = datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc).timestamp()
    saturday = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc).timestamp()
    later = datetime(2026, 10, 20, 9, 15, tzinfo=timezone.utc).timestamp()
    assert usage.when(soon, now) == "in 2h 25m"
    assert usage.when(saturday, now) == "Sat 10 pm"
    assert usage.when(later, now) == "Oct 20"
    assert usage.when(None, now) == ""
    assert usage.resets(soon, now) == "resets 7:30 pm (in 2h 25m)"
    assert usage.resets(saturday, now) == "resets Sat Oct 10, 10 pm"
    assert usage.clock(datetime(2026, 10, 7, 0, 5, tzinfo=timezone.utc).timestamp()) == "12:05 am"
    assert usage.clock(datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).timestamp()) == "12 pm"


def test_ago():
    assert usage.ago(1000, 1030) == "just now"
    assert usage.ago(1000, 1000 + 13 * 60) == "13 min ago"
    assert usage.ago(1000, 1000 + 3 * 3600) == "3 h ago"
    assert usage.ago(1000, 1000 + 50 * 3600) == "2 d ago"


def test_describe(utc):
    found = parse(cached())
    now = found.fetched_at + 120
    assert usage.describe(found, now) == [
        "Current session: 71% used · resets 8:30 am (in 2h 29m)",
        "Current week (all models): 49% used · resets Sat Oct 10, 11 am",
        "Current week (Fable): 0% used · resets Sat Oct 10, 11 am",
        "Updated 2 min ago",
    ]
    assert usage.describe(found, found.fetched_at + 7200)[-1] == (
        "Updated 2 h ago: Claude Code refreshes it while a session runs")


def test_credits_described_with_their_amounts():
    credits = Limit(("credits",), "Usage credits", "Credits", "", 24.8, None, detail="$12.40 of $50.00")
    lines = usage.describe(Usage((credits,), 1000), 1030)
    assert lines[0] == "Usage credits: 24% used · $12.40 of $50.00"
