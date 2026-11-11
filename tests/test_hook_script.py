"""The Claude Code hook script: what it writes for each event."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from conftest import DATA, HOOK

from ctally.claude_hooks import EVENTS

SID = "s1"


def payload(**fields) -> dict:
    return {"session_id": SID, **fields}


def test_states_and_end(hook):
    hook("working", payload(prompt="go"))
    assert (hook.dir / SID).read_text() == f"working {hook.pid} my project\n"
    hook("done", payload())
    assert hook.state(SID) == "done"
    hook("end", payload())
    assert not (hook.dir / SID).exists()


def test_waiting_never_overwrites_done(hook):
    hook("working", payload())
    hook("waiting", payload(notification_type="permission_prompt"))
    assert hook.state(SID) == "waiting"
    hook("done", payload())
    hook("waiting", payload())                   # older Claude Code: no notification_type
    assert hook.state(SID) == "done"
    hook("waiting", payload(notification_type="permission_prompt"))
    assert hook.state(SID) == "done"


def test_unknown_word_writes_nothing(hook):
    hook("bogus", payload())
    assert not (hook.dir / SID).exists()


def test_first_session_id_wins(hook):
    hook("working", {"session_id": "real", "tool_name": "mcp__x", "tool_input": {"session_id": "phantom"}})
    assert sorted(os.listdir(hook.dir)) == [".history", "real"]


def test_environment_session_id_wins_over_stdin(hook):
    hook("working", payload(), CLAUDE_CODE_SESSION_ID="from-env")
    assert sorted(os.listdir(hook.dir)) == [".history", "from-env"]


def test_missing_or_unsafe_session_id_falls_back_to_default(hook):
    hook("working", {"prompt": "no id"})
    hook("working", {"session_id": "../escape"})
    hook("working", {"session_id": ".hidden"})
    assert sorted(os.listdir(hook.dir)) == [".history", "default"]
    assert not (hook.home / ".claude" / "escape").exists()


def test_pretty_printed_json(hook):
    hook("working", '{\n  "session_id" : "spaced",\n  "prompt": "x"\n}')
    assert hook.state("spaced") == "working"


def test_agent_files(hook):
    hook("agent-start", payload(agent_id="a1", agent_type="Explore"))
    hook("agent-start", payload(agent_id="a2", agent_type="workflow-subagent", tool_input={"agent_id": "zzz"}))
    assert hook.agents(SID) == ["a1", "a2"]
    assert (hook.dir / ".agents" / SID / "a1").read_text().strip() == "Explore"
    assert (hook.dir / ".agents" / SID / "a2").read_text().strip() == "workflow-subagent"
    hook("agent-stop", payload(agent_id="a1", agent_type="Explore"))
    assert hook.agents(SID) == ["a2"]
    hook("agent-stop", payload(agent_id="a2"))
    assert hook.agents(SID) is None              # the empty folder goes too


def test_unsafe_agent_ids_are_refused(hook):
    hook("agent-start", payload(agent_id="../../evil", agent_type="x"))
    hook("agent-start", payload(agent_id=".hidden", agent_type="x"))
    hook("agent-start", payload(agent_type="no id"))
    assert hook.agents(SID) is None
    assert not (hook.home / ".claude" / "evil").exists()


def test_stop_with_background_agents_stays_working(hook):
    for kind in ("subagent", "workflow"):
        hook("working", payload())
        hook("agent-start", payload(agent_id="a1", agent_type="workflow-subagent"))
        tasks = [{"id": "t", "type": kind, "status": "running", "description": 'x {"status":"done"}'}]
        hook("done", payload(background_tasks=tasks))
        assert hook.state(SID) == "working", kind
        assert hook.agents(SID) == [".background", "a1"], kind
        hook("end", payload())


def test_stop_with_only_a_background_shell_is_done(hook):
    hook("working", payload())
    hook("agent-start", payload(agent_id="a1", agent_type="Explore"))
    hook("done", payload(background_tasks=[{"id": "b", "type": "shell", "status": "running"}]))
    assert hook.state(SID) == "done"
    assert hook.agents(SID) is None


def test_stop_with_finished_tasks_is_done(hook):
    hook("working", payload())
    hook("done", payload(background_tasks=[{"id": "w", "type": "workflow", "status": "completed"}]))
    assert hook.state(SID) == "done"


def test_idle_prompt_while_agents_run_is_ignored(hook):
    hook("working", payload())
    hook("agent-start", payload(agent_id="a1", agent_type="workflow-subagent"))
    hook("done", payload(background_tasks=[{"id": "w", "type": "workflow", "status": "running"}]))
    hook("waiting", payload(message="Claude is waiting for your input", notification_type="idle_prompt"))
    assert hook.state(SID) == "working"
    hook("agent-stop", payload(agent_id="a1"))
    assert hook.agents(SID) == [".background"]   # the marker keeps the folder
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "working"


def test_idle_prompt_after_an_interrupted_turn_says_done(hook):
    hook("working", payload(prompt="again"))     # Esc: no Stop follows
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "done"


def test_idle_prompt_leaves_an_open_question_waiting(hook):
    hook("asking", payload(hook_event_name="PermissionRequest"))
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "waiting"


def test_claude_back_at_work_clears_the_background_marker(hook):
    hook("working", payload())
    hook("agent-start", payload(agent_id="a1", agent_type="general-purpose"))
    hook("done", payload(background_tasks=[{"id": "a1", "type": "subagent", "status": "running"}]))
    assert hook.agents(SID) == [".background", "a1"]
    hook("working", payload(hook_event_name="PreToolUse", agent_id="a1"))    # the agent's own tool
    assert hook.agents(SID) == [".background", "a1"]
    hook("working", payload(hook_event_name="UserPromptSubmit"))            # Claude itself
    assert hook.agents(SID) == ["a1"]


def test_news_notifications_are_ignored(hook):
    hook("working", payload())
    for kind in ("agent_completed", "auth_success", "elicitation_complete", "elicitation_response",
                 "computer_use_enter", "computer_use_exit"):
        hook("waiting", payload(notification_type=kind))
        assert hook.state(SID) == "working", kind


def test_permission_prompts_say_waiting(hook):
    for kind in ("permission_prompt", "worker_permission_prompt"):
        hook("working", payload())
        hook("waiting", payload(notification_type=kind))
        assert hook.state(SID) == "waiting", kind


def test_post_tool_use_after_a_prompt_is_working(hook):
    hook("waiting", payload(notification_type="permission_prompt"))
    hook("working", payload(hook_event_name="PostToolUse"))
    assert hook.state(SID) == "working"


def test_end_removes_agents_too(hook):
    hook("working", payload())
    hook("agent-start", payload(agent_id="a1", agent_type="Explore"))
    hook("end", payload())
    assert not (hook.dir / SID).exists()
    assert hook.agents(SID) is None


def test_no_temporary_files_left_behind(hook):
    for state in ("working", "waiting", "done", "working"):
        hook(state, payload())
    hook("agent-start", payload(agent_id="a1", agent_type="Explore"))
    hook("agent-stop", payload(agent_id="a1"))
    leftovers = [name for name in os.listdir(hook.dir) if name not in (SID, ".agents", ".history")]
    assert leftovers == []
    assert sorted(os.listdir(hook.dir / ".history")) == ["." + SID, time.strftime("%Y-%m-%d")]


def test_always_exits_zero_and_silent(hook, tmp_path):
    readonly = tmp_path / "ro"
    (readonly / ".claude").mkdir(parents=True)
    os.chmod(readonly / ".claude", 0o500)
    try:
        for state in ("working", "done", "waiting", "agent-start", "end", "nonsense"):
            done = hook(state, payload(agent_id="a1"), HOME=str(readonly))
            assert done.returncode == 0 and done.stdout == "" and done.stderr == "", state
    finally:
        os.chmod(readonly / ".claude", 0o700)


def test_readers_never_see_a_partial_file(hook):
    """The state file is renamed into place, so a reader polling meanwhile never finds it
    empty or half-written."""
    hook("working", payload())
    path = hook.dir / SID
    seen_bad = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            try:
                text = path.read_text()
            except FileNotFoundError:
                seen_bad.append("missing")
                continue
            if not text.endswith("my project\n"):
                seen_bad.append(text)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for i in range(40):
            hook("working" if i % 2 else "waiting", payload(notification_type="permission_prompt"))
    finally:
        stop.set()
        reader.join()
    assert seen_bad == []


def test_replay_of_a_real_session(hook):
    """Events recorded from a real headless Claude Code session: a background subagent and a
    two-agent workflow. The session's turn ends while they run, and it must stay working
    until the last Stop that has nothing in the background."""
    sid = "7e28a3b0-f632-4e4a-8669-f6e04051221b"
    trace = []
    for line in (DATA / "hooks.log").read_text().splitlines():
        event, _, data = line.split("\t", 2)
        state = EVENTS.get(event)
        if state is None:
            continue
        assert hook(state, data).returncode == 0
        trace.append((event, hook.state(sid), len([a for a in hook.agents(sid) or [] if not a.startswith(".")])))

    stops = [i for i, (event, _, _) in enumerate(trace) if event == "Stop"]
    first_stop, settled = stops[0], stops[1]
    # Working from the first prompt until the Stop after the agents came back...
    assert all(state == "working" for _, state, _ in trace[:settled])
    # ...with three agents out when the turn first ended, and the marker set.
    assert trace[first_stop][2] == 3
    # Then done, a notification turn, done again, and gone at SessionEnd.
    assert [state for _, state, _ in trace[settled:]] == ["done", "working", "done", None]
    assert hook.agents(sid) is None
    assert json.loads((DATA / "hooks.log").read_text().splitlines()[0].split("\t", 2)[2])["session_id"] == sid


# MARK: Questions, and turns that fail

def test_asking_says_waiting_at_once_even_after_done(hook):
    hook("working", payload())
    hook("asking", payload(hook_event_name="PermissionRequest", tool_name="AskUserQuestion"))
    assert hook.state(SID) == "waiting"
    hook("working", payload(hook_event_name="PostToolUseFailure", tool_name="AskUserQuestion"))
    assert hook.state(SID) == "working"
    hook("done", payload())
    hook("asking", payload(tool_name="Bash"))      # a background agent wants a permission
    assert hook.state(SID) == "waiting"


SESSION_HIT = "You've hit your session limit · resets 7:30pm (Australia/Sydney)"


def failed(hook, error="rate_limit", message=SESSION_HIT, **fields):
    """StopFailure, as Claude Code sends it: UTF-8 as it is, not \\u escapes."""
    return hook("failed", json.dumps(payload(hook_event_name="StopFailure", error=error,
                                             last_assistant_message=message, **fields), ensure_ascii=False))


def seen_limits(hook, five_hour: int, seven_day: int) -> None:
    """What CTally's status line saved after the last reply."""
    hook.dir.mkdir(parents=True, exist_ok=True)
    (hook.dir / ".statusline").write_text(json.dumps({"session_id": "other", "rate_limits": {
        "five_hour": {"used_percentage": 99.4, "resets_at": five_hour},
        "seven_day": {"used_percentage": 61, "resets_at": seven_day}}}, separators=(",", ":")))


def noted(hook) -> str | None:
    try:
        return (hook.dir / ".limit").read_text()
    except OSError:
        return None


def test_the_usage_limit_leaves_the_session_limited_and_notes_when_it_resets(hook):
    later, much_later = int(time.time()) + 3600, int(time.time()) + 5 * 86400
    seen_limits(hook, later, much_later)
    hook("working", payload())
    failed(hook)
    assert hook.state(SID) == "limited"
    assert noted(hook) == f"{later} {SESSION_HIT}\n"
    failed(hook, message="You've hit your weekly limit · resets Oct 10, 10pm (Australia/Sydney)")
    assert noted(hook).startswith(f"{much_later} You've hit your weekly limit")
    failed(hook, message="You've hit your Fable limit")             # which window: not known
    assert noted(hook) == "0 You've hit your Fable limit\n"
    seen_limits(hook, int(time.time()) - 60, much_later)            # the status line is out of date
    failed(hook)
    assert noted(hook) == f"0 {SESSION_HIT}\n"
    assert sorted(os.listdir(hook.dir)) == [".history", ".limit", ".statusline", SID]


def test_a_prompt_while_the_limit_holds_stays_limited(hook):
    seen_limits(hook, int(time.time()) + 3600, int(time.time()) + 86400)
    failed(hook)
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="go on"))
    assert hook.state(SID) == "limited"
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "limited"
    hook("working", payload(hook_event_name="PreToolUse"))        # it got through after all
    assert hook.state(SID) == "working"


def claude_json(hook, account: str | None) -> None:
    """~/.claude.json, the way Claude Code writes it, saying who is signed in."""
    config = {"numStartups": 7, "cachedUsageUtilization": {"accountUuid": "someone-else"},
              "oauthAccount": account and {"accountUuid": account, "emailAddress": "me@example.com"}}
    (hook.home / ".claude.json").write_text(json.dumps(config, indent=2))


def test_the_limit_is_noted_for_the_account_signed_in(hook):
    soon, later, week = int(time.time()) + 1800, int(time.time()) + 3600, int(time.time()) + 4 * 86400
    seen_limits(hook, later, week)                                  # the latest of any account
    (hook.dir / ".usage").mkdir()
    (hook.dir / ".usage" / "acct-b").write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 99, "resets_at": soon}, "seven_day": {"used_percentage": 9, "resets_at": week}}}))
    claude_json(hook, "acct-b")
    failed(hook)
    assert noted(hook) == f"{soon} {SESSION_HIT}\naccount acct-b\n"         # its own window, not the other's
    claude_json(hook, "acct-c")                                     # one the status line hasn't seen
    failed(hook)
    assert noted(hook) == f"0 {SESSION_HIT}\naccount acct-c\n"
    claude_json(hook, None)
    failed(hook)
    assert noted(hook) == f"0 {SESSION_HIT}\n"


def test_a_prompt_after_changing_accounts_is_working(hook):
    """The limit was the other account's: on this one the prompt goes through."""
    seen_limits(hook, int(time.time()) + 3600, int(time.time()) + 86400)
    claude_json(hook, "acct-a")
    failed(hook)
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="go on"))
    assert hook.state(SID) == "limited"
    claude_json(hook, "acct-b")
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="go on"))
    assert hook.state(SID) == "working"


def test_a_prompt_once_the_limit_has_reset_is_working(hook):
    seen_limits(hook, int(time.time()) + 3600, int(time.time()) + 86400)
    failed(hook)
    (hook.dir / ".limit").write_text(f"{int(time.time()) - 5} {SESSION_HIT}\n")
    hook("working", payload(hook_event_name="UserPromptSubmit"))
    assert hook.state(SID) == "working"


def test_a_finished_turn_or_claude_carrying_on_ends_the_limit(hook):
    failed(hook)
    hook("done", payload())
    assert hook.state(SID) == "done" and noted(hook) is None
    failed(hook)
    hook("waiting", payload(notification_type="quota_auto_resume_fired"))
    assert hook.state(SID) == "working" and noted(hook) is None
    failed(hook)
    hook("waiting", payload(notification_type="quota_auto_resume_stale"))   # "press enter to continue"
    assert hook.state(SID) == "waiting" and noted(hook) is None


def aged(path, seconds: float) -> None:
    """As if written that long ago."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_a_reply_already_on_its_way_doesnt_end_the_limit(hook):
    """Another session's turn, begun before the limit was hit and ending after, got through
    nothing since. Its next turn does."""
    other = {"session_id": "s2"}
    hook("working", {**other, "hook_event_name": "PreToolUse"})
    aged(hook.dir / "s2", 30)
    failed(hook)                                                    # s1 runs into the limit
    aged(hook.dir / ".limit", 20)
    hook("done", {**other, "hook_event_name": "Stop"})
    assert hook.state("s2") == "done" and noted(hook) is not None and hook.state(SID) == "limited"
    hook("working", {**other, "hook_event_name": "UserPromptSubmit", "prompt": "next"})
    hook("done", {**other, "hook_event_name": "Stop"})
    assert noted(hook) is None


def test_a_turn_on_another_account_doesnt_end_the_limit(hook):
    claude_json(hook, "acct-a")
    failed(hook)
    aged(hook.dir / ".limit", 20)
    claude_json(hook, "acct-b")                                     # swapped, and working on
    other = {"session_id": "s2"}
    hook("working", {**other, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    hook("done", {**other, "hook_event_name": "Stop"})
    assert noted(hook) is not None
    claude_json(hook, "acct-a")                                     # back, and a turn gets through there
    hook("working", {**other, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    hook("done", {**other, "hook_event_name": "Stop"})
    assert noted(hook) is None


def test_other_failures(hook):
    for error, state in [("authentication_failed", "waiting"), ("billing_error", "waiting"),
                         ("overloaded", "done"), ("server_error", "done"), ("", "done")]:
        hook("working", payload())
        failed(hook, error=error, message="API Error")
        assert hook.state(SID) == state, error
    assert noted(hook) is None


def test_more_news_notifications_are_ignored(hook):
    hook("working", payload())
    for kind in ("quota_auto_resume_disabled", "push_notification", "model_refusal_fallback", "auth_storage_failure"):
        hook("waiting", payload(notification_type=kind))
        assert hook.state(SID) == "working", kind


# MARK: The status line script

STATUS_LINE_SCRIPT = HOOK.with_name("ctally-statusline.sh")
HOUR, WEEK = int(time.time()) + 3 * 3600, int(time.time()) + 4 * 86400     # windows still going
OTHER_WEEK = WEEK + 2 * 86400                                                # another account's


def status_line(home, payload, *args):
    import subprocess
    text = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**{k: v for k, v in os.environ.items() if k != "CTALLY_STATE_DIR"}, "HOME": str(home)}
    return subprocess.run(["sh", str(STATUS_LINE_SCRIPT), *args], input=text, text=True,
                          capture_output=True, env=env, timeout=10)


def started(home, *names) -> None:
    """A session's first status line, as it starts: its context window, and no limits before
    its first reply."""
    for name in names:
        status_line(home, {"session_id": name, "context_window": {"context_window_size": 200000}})


def usage_line(session: tuple | None, week: tuple | None = None, name: str = "s1") -> dict:
    """What one session hands its status line: (used, resets at) for the session and the week."""
    limits = {}
    if session:
        limits["five_hour"] = {"used_percentage": session[0], "resets_at": session[1]}
    if week:
        limits["seven_day"] = {"used_percentage": week[0], "resets_at": week[1]}
    return {"session_id": name, "rate_limits": limits}


def kept(home, account: str = "-") -> dict | None:
    """What's kept for an account; "-" with nobody signed in."""
    path = home / ".claude" / "ctally.d" / ".usage" / account
    return json.loads(path.read_text()) if path.exists() else None


def saved_usage(home, account: str = "-") -> tuple | None:
    found = kept(home, account)
    limits = found["rate_limits"] if found else None
    return limits and tuple(limits.get(window, {}).get("used_percentage") for window in ("five_hour", "seven_day"))


def sign_in(home, account: str | None, indent: int | None = 2) -> None:
    """~/.claude.json as Claude Code keeps it: much else in it, other accounts' ids among it."""
    config = {"numStartups": 7, "cachedUsageUtilization": {"accountUuid": "someone-else", "utilization": {}},
              "oauthAccount": account and {"accountUuid": account, "emailAddress": "me@example.com",
                                           "ccOnboardingFlags": {"seen": True}, "organizationUuid": "org-1"},
              "projects": {"/work": {"accountUuid": "not-this-one"}}}
    (home / ".claude.json").write_text(json.dumps(config, indent=indent))


LIMITS = usage_line((79, HOUR))


def test_status_line_saves_the_limits_and_prints_nothing(tmp_path):
    started(tmp_path, "s1")
    done = status_line(tmp_path, LIMITS)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    state = tmp_path / ".claude" / "ctally.d"
    assert json.loads((state / ".statusline").read_text()) == LIMITS == kept(tmp_path)
    assert sorted(os.listdir(state)) == [".status", ".statusline", ".usage"]        # no temporary file left
    assert (os.listdir(state / ".status"), os.listdir(state / ".usage")) == (["s1"], ["-"])


def test_status_line_without_limits_keeps_the_last_ones(tmp_path):
    started(tmp_path, "s1")
    status_line(tmp_path, LIMITS)
    status_line(tmp_path, {"session_id": "s2", "model": {"id": "x"}})   # before the first reply
    saved = tmp_path / ".claude" / "ctally.d" / ".statusline"
    assert json.loads(saved.read_text()) == LIMITS


def test_status_line_runs_the_one_you_had(tmp_path):
    started(tmp_path, "s1")
    theirs = "read line; printf 'mine: %s' \"$(printf '%s' \"$line\" | cut -c1-14)\"; exit 3"
    done = status_line(tmp_path, LIMITS, theirs)
    assert (done.returncode, done.stdout) == (3, 'mine: {"session_id":')
    assert (tmp_path / ".claude" / "ctally.d" / ".statusline").exists()


def test_status_line_writes_nothing_where_it_cant(tmp_path):
    (tmp_path / ".claude").write_text("a file where the folder should be")
    done = status_line(tmp_path, LIMITS, "echo still here")
    assert (done.returncode, done.stdout, done.stderr) == (0, "still here\n", "")


def test_status_line_waits_for_news_from_a_session_it_never_heard(tmp_path):
    """A session running since before CTally's status line: what it hands over first could be
    hours old, and there's nothing it said before to tell."""
    status_line(tmp_path, usage_line((60, HOUR), (8, WEEK)))
    status_line(tmp_path, usage_line((60, HOUR), (8, WEEK)))            # and again, redrawing
    assert kept(tmp_path) is None
    status_line(tmp_path, usage_line((61, HOUR), (8, WEEK)))            # a reply: it moved
    assert saved_usage(tmp_path) == (61, 8)


def test_status_line_never_steps_back_within_a_window(tmp_path):
    started(tmp_path, "busy", "idle")
    status_line(tmp_path, usage_line((60, HOUR), (8, WEEK), "busy"))
    status_line(tmp_path, usage_line((34, HOUR), (5, WEEK), "idle"))       # an idle session redraws, hours old
    assert saved_usage(tmp_path) == (60, 8)
    status_line(tmp_path, usage_line((60, HOUR + 30), (7, WEEK), "idle"))  # same session use, older week
    assert saved_usage(tmp_path) == (60, 8)
    status_line(tmp_path, usage_line((64.5, HOUR), (8, WEEK), "busy"))
    assert saved_usage(tmp_path) == (64.5, 8)
    status_line(tmp_path, usage_line((64.5, HOUR), (9, WEEK), "busy"))     # the week moved, the session not
    assert saved_usage(tmp_path) == (64.5, 9)


def test_status_line_takes_a_new_window(tmp_path):
    started(tmp_path, "s1", "idle")
    status_line(tmp_path, usage_line((90, HOUR), (50, WEEK), "idle"))
    status_line(tmp_path, usage_line((92, HOUR), (52, WEEK)))
    status_line(tmp_path, usage_line((2, HOUR + 7200), (0, WEEK)))         # reset early: a later window
    assert saved_usage(tmp_path) == (2, 0)
    status_line(tmp_path, usage_line((90, HOUR), (50, WEEK), "idle"))      # the window that was, redrawn
    assert saved_usage(tmp_path) == (2, 0)


def test_status_line_takes_no_older_week_once_the_session_window_is_over(tmp_path):
    """An idle session's window has run out, and Claude Code leaves it out: the week it still
    hands over is as old as its last reply."""
    started(tmp_path, "s1", "idle")
    over = int(time.time()) - 60
    status_line(tmp_path, usage_line((80, over), (50, WEEK)))              # that window is over: only the week counts
    assert saved_usage(tmp_path) == (80, 50)
    status_line(tmp_path, usage_line((10, over - 3600), (40, WEEK), "idle"))
    status_line(tmp_path, usage_line(None, (40, WEEK), "idle"))
    assert saved_usage(tmp_path) == (80, 50)
    status_line(tmp_path, usage_line(None, (51, WEEK), "idle"))            # further on in the same week
    assert saved_usage(tmp_path) == (None, 51)
    status_line(tmp_path, usage_line((3, HOUR), (51, WEEK)))               # a new session window
    assert saved_usage(tmp_path) == (3, 51)
    status_line(tmp_path, usage_line(None, (60, WEEK), "idle"))            # none, while one is going: older
    assert saved_usage(tmp_path) == (3, 51)


def test_a_window_running_out_is_no_news(tmp_path):
    """What an idle session hands over changes when its session window ends. For a week that
    isn't kept, on the account signed in before, that mustn't pass for a reply."""
    started(tmp_path, "s1")
    status_line(tmp_path, usage_line((12, HOUR), (30, WEEK)))
    notes = tmp_path / ".claude" / "ctally.d" / ".status"
    (notes / "idle").write_text(json.dumps(usage_line((90, int(time.time()) - 60), (89, OTHER_WEEK), "idle")))
    status_line(tmp_path, usage_line(None, (89, OTHER_WEEK), "idle"))
    assert saved_usage(tmp_path) == (12, 30)
    assert json.loads((notes / "idle").read_text()) == usage_line(None, (89, OTHER_WEEK), "idle")


# Accounts

def test_each_account_keeps_its_own(tmp_path):
    sign_in(tmp_path, "acct-a")
    started(tmp_path, "s1", "s2")
    status_line(tmp_path, usage_line((55, HOUR), (88, WEEK), "s2"))
    status_line(tmp_path, usage_line((92, HOUR), (89, WEEK)))
    latest = tmp_path / ".claude" / "ctally.d" / ".statusline"
    assert saved_usage(tmp_path, "acct-a") == (92, 89) and kept(tmp_path) is None
    assert json.loads(latest.read_text()) == kept(tmp_path, "acct-a")

    # Nearly out: I swap to my other account, under the sessions that are running.
    sign_in(tmp_path, "acct-b")
    fresh = usage_line((12, HOUR - 3600), (30, OTHER_WEEK))                # its session window ends sooner
    status_line(tmp_path, fresh)
    assert saved_usage(tmp_path, "acct-b") == (12, 30) and saved_usage(tmp_path, "acct-a") == (92, 89)
    assert json.loads(latest.read_text()) == fresh
    status_line(tmp_path, usage_line((55, HOUR), (88, WEEK), "s2"))        # an idle session, still on the first
    assert saved_usage(tmp_path, "acct-b") == (12, 30) and saved_usage(tmp_path, "acct-a") == (92, 89)
    assert json.loads(latest.read_text()) == fresh

    # A reply that was on its way as I swapped: the first account's, by its week.
    late = usage_line((93, HOUR), (89, WEEK), "s2")
    status_line(tmp_path, late)
    assert kept(tmp_path, "acct-a") == late and saved_usage(tmp_path, "acct-b") == (12, 30)
    assert json.loads(latest.read_text()) == fresh                         # not the one signed in

    # Back again, and a reply there.
    sign_in(tmp_path, "acct-a")
    back = usage_line((94, HOUR), (89, WEEK))
    status_line(tmp_path, back)
    assert kept(tmp_path, "acct-a") == back == json.loads(latest.read_text())
    assert sorted(os.listdir(latest.parent / ".usage")) == ["acct-a", "acct-b"]


@pytest.mark.parametrize("indent", [2, None])
def test_the_account_is_the_one_signed_in_however_the_config_is_written(tmp_path, indent):
    sign_in(tmp_path, "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab", indent)
    started(tmp_path, "s1")
    status_line(tmp_path, usage_line((12, HOUR), (30, WEEK)))
    assert os.listdir(tmp_path / ".claude" / "ctally.d" / ".usage") == ["0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"]


@pytest.mark.parametrize("config", ['{"oauthAccount": null, "other": {"accountUuid": "x"}}', "not json", None,
                                    '{"oauthAccount": {"accountUuid": "../../escape"}}'])
def test_with_nobody_signed_in_the_limits_are_just_kept(tmp_path, config):
    if config is not None:
        (tmp_path / ".claude.json").write_text(config)
    started(tmp_path, "s1")
    status_line(tmp_path, usage_line((12, HOUR), (30, WEEK)))
    assert saved_usage(tmp_path) == (12, 30)
    assert os.listdir(tmp_path / ".claude" / "ctally.d" / ".usage") == ["-"]


def test_two_accounts_with_the_same_week_go_by_who_is_signed_in(tmp_path):
    sign_in(tmp_path, "acct-a")
    started(tmp_path, "s1", "s2")
    status_line(tmp_path, usage_line((92, HOUR), (89, WEEK)))
    sign_in(tmp_path, "acct-b")
    status_line(tmp_path, usage_line((12, HOUR + 3600), (30, WEEK)))       # another session window in that week
    assert (saved_usage(tmp_path, "acct-a"), saved_usage(tmp_path, "acct-b")) == ((92, 89), (12, 30))
    status_line(tmp_path, usage_line((13, HOUR + 3600), (30, WEEK)))
    assert (saved_usage(tmp_path, "acct-a"), saved_usage(tmp_path, "acct-b")) == ((92, 89), (13, 30))


def test_status_line_ends_a_limit_reset_early(tmp_path):
    state = tmp_path / ".claude" / "ctally.d"
    state.mkdir(parents=True)
    limit = state / ".limit"
    limit.write_text(f"{HOUR} {SESSION_HIT}\n")
    started(tmp_path, "s1")
    status_line(tmp_path, LIMITS)                                           # the same window
    assert limit.exists()
    status_line(tmp_path, usage_line((2, HOUR + 30), (0, WEEK)))            # a clock's slack, no more
    assert limit.exists() and saved_usage(tmp_path) == (2, 0)
    later = usage_line((2, HOUR + 7200), (0, WEEK))
    done = status_line(tmp_path, json.dumps(later, separators=(" , ", " : ")))   # a new window, however spaced
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not limit.exists()


def test_status_line_keeps_a_limit_it_cant_place(tmp_path):
    state = tmp_path / ".claude" / "ctally.d"
    state.mkdir(parents=True)
    started(tmp_path, "s1")
    notes = (f"0 {SESSION_HIT}", f"{HOUR} You've hit your Fable limit", "junk",
             f"{HOUR} {SESSION_HIT}\naccount acct-a")                       # hit on the account I've left
    sign_in(tmp_path, "acct-b")
    for used, note in enumerate(notes, start=2):
        (state / ".limit").write_text(note + "\n")
        done = status_line(tmp_path, usage_line((used, HOUR + 7200)))
        assert (done.returncode, done.stderr) == (0, "")
        assert saved_usage(tmp_path, "acct-b") == (used, None)
        assert (state / ".limit").exists(), note
    (state / ".limit").write_text(f"{HOUR} {SESSION_HIT}\naccount acct-b\n")
    status_line(tmp_path, usage_line((9, HOUR + 7200)))
    assert not (state / ".limit").exists()


# MARK: What a session asks

ASK = payload(hook_event_name="PermissionRequest", tool_name="Bash",
              tool_input={"command": 'npm publish --tag "next"', "description": "Publish"})


def asked(hook) -> dict | None:
    path = hook.dir / ".asks" / SID
    return json.loads(path.read_text()) if path.exists() else None


def test_a_permission_request_is_kept_whole(hook):
    hook("asking", ASK)
    assert hook.state(SID) == "waiting"
    assert asked(hook) == ASK
    assert not [n for n in os.listdir(hook.dir / ".asks") if n.startswith(".")]     # no temporary left
    hook("waiting", payload(hook_event_name="Notification", notification_type="permission_prompt"))
    assert asked(hook) == ASK


def test_other_tools_and_agents_running_meanwhile_leave_it(hook):
    """The question is still on screen, so the session still waits on me."""
    hook("asking", ASK)
    for event in (payload(hook_event_name="PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}),    # started alongside
                  payload(hook_event_name="PreToolUse", tool_name="Grep", agent_id="a1", agent_type="Explore"),
                  payload(hook_event_name="PostToolUse", tool_name="Bash", agent_id="a1", agent_type="Explore"),
                  payload(hook_event_name="PostToolUseFailure", tool_name="Bash", agent_id="a1")):
        hook("working", event)
        assert (asked(hook), hook.state(SID)) == (ASK, "waiting"), event


def test_whoever_asked_going_on_to_another_tool_settles_it(hook):
    """Told "no, do this instead", Claude carries on, and the tool it asked about never runs."""
    hook("asking", ASK)
    hook("working", payload(hook_event_name="PreToolUse", tool_name="Read", tool_input={"file_path": "/x"}))
    assert asked(hook) is None and hook.state(SID) == "working"


def test_what_an_agent_asks_is_settled_by_that_agent(hook):
    ask = payload(agent_id="a1", agent_type="general-purpose", hook_event_name="PermissionRequest", tool_name="Bash",
                  tool_input={"command": "rm -rf build"})
    hook("asking", ask)
    hook("working", payload(hook_event_name="PreToolUse", tool_name="Read"))                # Claude itself
    hook("working", payload(hook_event_name="PostToolUse", tool_name="Bash"))
    hook("working", payload(hook_event_name="PostToolUse", tool_name="Bash", agent_id="a2"))    # another agent
    assert (asked(hook), hook.state(SID)) == (ask, "waiting")
    hook("working", payload(hook_event_name="PostToolUse", tool_name="Bash", agent_id="a1"))
    assert asked(hook) is None and hook.state(SID) == "working"


def test_an_agent_at_work_leaves_the_background_marker_while_a_question_is_up(hook):
    hook("agent-start", payload(agent_id="a1", agent_type="Explore"))
    marker = hook.dir / ".agents" / SID / ".background"
    marker.write_text("")
    hook("asking", ASK)
    hook("working", payload(hook_event_name="PostToolUse", tool_name="Read"))
    assert marker.exists() and hook.state(SID) == "waiting"


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
def test_the_tool_running_settles_it(hook, event):
    hook("asking", ASK)
    hook("working", payload(hook_event_name=event, tool_name="Bash", tool_input={"command": "npm publish"}))
    assert asked(hook) is None and hook.state(SID) == "working"


@pytest.mark.parametrize("state, fields", [("working", {"hook_event_name": "UserPromptSubmit", "prompt": "no"}),
                                           ("done", {"hook_event_name": "Stop"}),
                                           ("failed", {"hook_event_name": "StopFailure", "error": "server_error"}),
                                           ("end", {"hook_event_name": "SessionEnd"})])
def test_a_new_prompt_or_the_end_of_the_turn_settles_it(hook, state, fields):
    hook("asking", ASK)
    hook(state, payload(**fields))
    assert asked(hook) is None


# MARK: Each session's context, from the status line

CONTEXT = {"session_id": "s1", "context_window": {
    "total_input_tokens": 195019, "context_window_size": 1000000, "used_percentage": 20,
    "current_usage": {"input_tokens": 2, "output_tokens": 11996, "cache_creation_input_tokens": 7878,
                      "cache_read_input_tokens": 187139}},
    "rate_limits": {"five_hour": {"used_percentage": 17, "resets_at": HOUR}}}


def test_status_line_keeps_each_sessions_own(tmp_path):
    done = status_line(tmp_path, CONTEXT)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    status = tmp_path / ".claude" / "ctally.d" / ".status"
    assert json.loads((status / "s1").read_text()) == CONTEXT
    status_line(tmp_path, {**CONTEXT, "session_id": "s2"})
    assert sorted(os.listdir(status)) == ["s1", "s2"]                  # one each, no temporary left


def test_status_line_keeps_no_context_it_wasnt_given_or_cant_name(tmp_path):
    from ctally.sessions import context_from
    status_line(tmp_path, LIMITS)                   # the limits it said, to tell its next from; no context in it
    status = tmp_path / ".claude" / "ctally.d" / ".status"
    assert context_from(json.loads((status / "s1").read_text())) is None
    status_line(tmp_path, {"session_id": "s3", "model": {"id": "x"}})
    for bad in ("../escape", ".hidden", "", "two words"):
        status_line(tmp_path, {**CONTEXT, "session_id": bad})
    assert os.listdir(status) == ["s1"]
    assert not (tmp_path / ".claude" / "escape").exists()


# MARK: The history, for ctally stats

def history(hook) -> list[list[str]]:
    """Today's lines, without the time: [session, state, ...]."""
    path = hook.dir / ".history" / time.strftime("%Y-%m-%d")
    return [line.split()[1:] for line in path.read_text().splitlines()] if path.exists() else []


def test_history_notes_changes_and_prompts(hook):
    before = time.time()
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="go"))
    hook("working", payload(hook_event_name="PreToolUse", tool_name="Bash"))      # no change: nothing
    hook("asking", ASK)
    hook("working", payload(hook_event_name="PostToolUse", tool_name="Bash"))
    hook("done", payload(hook_event_name="Stop"))
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="again"))
    hook("working", {"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": "queued"})
    hook("end", payload(hook_event_name="SessionEnd"))
    assert history(hook) == [[SID, "working", "prompt"], [SID, "waiting"], [SID, "working"], [SID, "done"],
                             [SID, "working", "prompt"], [SID, "working", "prompt"], [SID, "end"]]
    stamps = [int(line.split()[0]) for line in (hook.dir / ".history" / time.strftime("%Y-%m-%d")).read_text().splitlines()]
    assert all(before - 1 <= t <= time.time() + 1 for t in stamps)
    assert not (hook.dir / ".history" / f".{SID}").exists()          # the session's marker goes with it


def test_a_lasting_state_is_noted_again_every_few_minutes(hook):
    hook("working", payload(hook_event_name="PreToolUse"))
    marker = hook.dir / ".history" / f".{SID}"
    assert marker.read_text().split()[1] == "0"
    for _ in range(5):
        hook("working", payload(hook_event_name="PreToolUse"))
    assert marker.read_text().split()[1] == "5" and len(history(hook)) == 1   # counted, not yet looked into
    # Twenty calls on, five minutes since the last line: noted again.
    marker.write_text(f"{int(time.time()) - 301} 19\n")
    hook("working", payload(hook_event_name="PreToolUse"))
    assert history(hook) == [[SID, "working"], [SID, "working"]]
    # Twenty calls on, but not five minutes: only the count starts again.
    marker.write_text(f"{int(time.time()) - 60} 19\n")
    hook("working", payload(hook_event_name="PreToolUse"))
    assert len(history(hook)) == 2 and marker.read_text().split()[1] == "0"


def test_a_session_already_at_work_when_the_hook_came_is_noted_at_once(hook):
    (hook.dir).mkdir(parents=True)
    (hook.dir / SID).write_text(f"working {hook.pid} my project\n")       # the old hook's doing
    hook("working", payload(hook_event_name="PreToolUse"))
    assert history(hook) == [[SID, "working"]]
    hook("working", payload(hook_event_name="PreToolUse"))
    assert history(hook) == [[SID, "working"]]
    # A mark with no time in it counts as none.
    (hook.dir / ".history" / f".{SID}").write_text("0 3\n")
    hook("working", payload(hook_event_name="PreToolUse"))
    assert history(hook) == [[SID, "working"], [SID, "working"]]
