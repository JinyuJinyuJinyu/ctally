"""The Claude Code hook script: what it writes for each event."""
from __future__ import annotations

import json
import os
import threading
import time

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
    assert sorted(os.listdir(hook.dir)) == ["real"]


def test_environment_session_id_wins_over_stdin(hook):
    hook("working", payload(), CLAUDE_CODE_SESSION_ID="from-env")
    assert sorted(os.listdir(hook.dir)) == ["from-env"]


def test_missing_or_unsafe_session_id_falls_back_to_default(hook):
    hook("working", {"prompt": "no id"})
    hook("working", {"session_id": "../escape"})
    hook("working", {"session_id": ".hidden"})
    assert sorted(os.listdir(hook.dir)) == ["default"]
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


def test_idle_prompt_after_an_interrupted_turn_says_waiting(hook):
    hook("working", payload(prompt="again"))     # Esc: no Stop follows
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "waiting"


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
    leftovers = [name for name in os.listdir(hook.dir) if name not in (SID, ".agents")]
    assert leftovers == []


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
    hook("working", payload(hook_event_name="PostToolUseFailure"))
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
    assert sorted(os.listdir(hook.dir)) == [".limit", ".statusline", SID]


def test_a_prompt_while_the_limit_holds_stays_limited(hook):
    seen_limits(hook, int(time.time()) + 3600, int(time.time()) + 86400)
    failed(hook)
    hook("working", payload(hook_event_name="UserPromptSubmit", prompt="go on"))
    assert hook.state(SID) == "limited"
    hook("waiting", payload(notification_type="idle_prompt"))
    assert hook.state(SID) == "limited"
    hook("working", payload(hook_event_name="PreToolUse"))        # it got through after all
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


def status_line(home, payload, *args):
    import subprocess
    text = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**{k: v for k, v in os.environ.items() if k != "CTALLY_STATE_DIR"}, "HOME": str(home)}
    return subprocess.run(["sh", str(STATUS_LINE_SCRIPT), *args], input=text, text=True,
                          capture_output=True, env=env, timeout=10)


LIMITS = {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 79, "resets_at": 1791361800}}}


def test_status_line_saves_the_limits_and_prints_nothing(tmp_path):
    done = status_line(tmp_path, LIMITS)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    saved = tmp_path / ".claude" / "ctally.d" / ".statusline"
    assert json.loads(saved.read_text()) == LIMITS
    assert sorted(os.listdir(saved.parent)) == [".statusline"]          # no temporary file left


def test_status_line_without_limits_keeps_the_last_ones(tmp_path):
    status_line(tmp_path, LIMITS)
    status_line(tmp_path, {"session_id": "s2", "model": {"id": "x"}})   # before the first reply
    saved = tmp_path / ".claude" / "ctally.d" / ".statusline"
    assert json.loads(saved.read_text()) == LIMITS


def test_status_line_runs_the_one_you_had(tmp_path):
    theirs = "read line; printf 'mine: %s' \"$(printf '%s' \"$line\" | cut -c1-14)\"; exit 3"
    done = status_line(tmp_path, LIMITS, theirs)
    assert (done.returncode, done.stdout) == (3, 'mine: {"session_id":')
    assert (tmp_path / ".claude" / "ctally.d" / ".statusline").exists()


def test_status_line_writes_nothing_where_it_cant(tmp_path):
    (tmp_path / ".claude").write_text("a file where the folder should be")
    done = status_line(tmp_path, LIMITS, "echo still here")
    assert (done.returncode, done.stdout, done.stderr) == (0, "still here\n", "")
