"""Reading sessions: state files, transcripts, subagents and tmux locations."""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import dead_pid, sleeper, wait_for

from ctally.sessions import AgentWatcher, Agents, Names, State, StateReader, TmuxLocator

LIVE = os.getpid()


@pytest.fixture
def dirs(tmp_path):
    state, projects = tmp_path / "ctally.d", tmp_path / "projects"
    state.mkdir()
    projects.mkdir()
    return state, projects


def write(state_dir, name: str, text: str, mtime: float | None = None) -> None:
    path = state_dir / name
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def transcript(projects, sid: str, lines: list, folder: str = "-Users-someone-code-api") -> object:
    path = projects / folder / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
    return path


# MARK: State

def test_parses_state_pid_and_a_name_with_spaces(dirs):
    state, projects = dirs
    write(state, "a", f"working {LIVE} my big project\n")
    [session] = StateReader(state, projects).poll()
    assert (session.id, session.state, session.pid, session.label) == ("a", State.WORKING, LIVE, "my big project")


def test_unknown_words_mean_idle_and_junk_is_skipped(dirs):
    state, projects = dirs
    write(state, "a", f"Pondering {LIVE} p\n")
    write(state, "b", "")
    write(state, ".hidden", f"working {LIVE} p\n")
    sessions = StateReader(state, projects).poll()
    assert [(s.id, s.state) for s in sessions] == [("a", State.IDLE)]


def test_limited_shows_as_waiting_until_the_limit_resets(dirs):
    state, projects = dirs
    write(state, "a", f"limited {LIVE} p\n")
    reader = StateReader(state, projects)
    write(state, ".limit", f"{int(time.time()) + 3600} You've hit your session limit · resets 7:30pm\n")
    [session] = reader.poll()
    assert (session.state, session.limited) == (State.WAITING, True)
    write(state, ".limit", "0 You've hit your Fable limit\n")              # when: not known
    assert reader.poll()[0].limited
    write(state, ".limit", f"{int(time.time()) - 5} You've hit your session limit\n")
    [session] = reader.poll()
    assert (session.state, session.limited) == (State.DONE, False)   # over: it just sits there
    (state / ".limit").unlink()                                      # a turn has got through since
    assert reader.poll()[0].state is State.DONE


def test_the_limit_note(dirs):
    from ctally.sessions import read_limit
    state, _ = dirs
    assert read_limit(state) is None
    write(state, ".limit", "1791379800 You've hit your session limit · resets 7:30pm (Australia/Sydney)\n", mtime=1000)
    hit = read_limit(state)
    assert (hit.resets_at, hit.key, hit.noted) == (1791379800, ("session",), 1000)
    assert hit.holds(1791379799) and not hit.holds(1791379800)
    write(state, ".limit", "0 You've hit your weekly limit\n")
    assert (read_limit(state).resets_at, read_limit(state).key, read_limit(state).holds(9e12)) == (None, ("week",), True)
    write(state, ".limit", "junk\n")
    assert (read_limit(state).resets_at, read_limit(state).key) == (None, None)


def test_state_words_any_case():
    assert State.parse("DONE") is State.DONE
    assert State.parse("") is State.IDLE
    assert [s.urgency for s in (State.WAITING, State.DONE, State.WORKING, State.IDLE)] == [3, 2, 1, 0]


def test_dead_sessions_hide_while_live_ones_exist(dirs):
    state, projects = dirs
    gone = dead_pid()
    write(state, "live", f"working {LIVE} p\n")
    write(state, "dead-done", f"done {gone} p\n")
    write(state, "dead-working", f"working {gone} p\n")
    assert [s.id for s in StateReader(state, projects).poll()] == ["live"]


def test_finished_dead_sessions_show_when_nothing_is_live(dirs):
    state, projects = dirs
    gone = dead_pid()
    write(state, "dead-done", f"done {gone} p\n")
    write(state, "dead-working", f"working {gone} p\n")
    assert [s.id for s in StateReader(state, projects).poll()] == ["dead-done"]


def test_no_pid_counts_as_alive(dirs):
    state, projects = dirs
    write(state, "a", "waiting\n")
    assert [s.id for s in StateReader(state, projects).poll()] == ["a"]


def test_oldest_first_by_first_sight(dirs):
    state, projects = dirs
    reader = StateReader(state, projects)
    write(state, "zz-first", f"working {LIVE} p\n")
    reader.poll()
    write(state, "aa-second", f"working {LIVE} p\n")
    assert [s.id for s in reader.poll()] == ["zz-first", "aa-second"]
    # Found in the same sweep, they tie, and the name decides.
    write(state, "c", f"working {LIVE} p\n")
    write(state, "b", f"working {LIVE} p\n")
    assert [s.id for s in reader.poll()] == ["zz-first", "aa-second", "b", "c"]


def test_a_rewritten_file_is_read_again(dirs):
    state, projects = dirs
    reader = StateReader(state, projects)
    write(state, "a", f"working {LIVE} p\n")
    assert reader.poll()[0].state is State.WORKING
    write(state, "a", f"done {LIVE} p\n")
    assert reader.poll()[0].state is State.DONE
    (state / "a").unlink()
    assert reader.poll() == []


def test_missing_directory_means_no_sessions(tmp_path):
    assert StateReader(tmp_path / "nowhere", tmp_path).poll() == []


def test_stale_dead_files_are_pruned(dirs):
    state, projects = dirs
    old = time.time() - 2 * StateReader.STALE_AFTER
    write(state, "ancient", f"working {dead_pid()} p\n", mtime=old)
    folder = state / ".agents" / "orphan"
    folder.mkdir(parents=True)
    (folder / "a1").write_text("Explore")
    os.utime(folder, (old, old))
    StateReader(state, projects).poll()
    assert not (state / "ancient").exists()
    assert not folder.exists()


# MARK: Names

def test_custom_title_beats_the_generated_one(dirs):
    state, projects = dirs
    transcript(projects, "s", [
        {"type": "user", "cwd": "/Users/someone/code/api", "sessionId": "s"},
        {"type": "ai-title", "aiTitle": "Generated"},
        {"type": "custom-title", "customTitle": "Mine"},
        {"type": "ai-title", "aiTitle": "Generated later"},
    ])
    assert Names(projects).info("s") == ("Mine", "/Users/someone/code/api")


def test_generated_title_and_spaced_json(dirs):
    state, projects = dirs
    transcript(projects, "s", ['{"type": "user", "cwd" : "/srv/spaced dir"}', {"type": "ai-title", "aiTitle": "Gen"}])
    assert Names(projects).info("s") == ("Gen", "/srv/spaced dir")


def test_no_transcript_means_no_name(dirs):
    _, projects = dirs
    names = Names(projects)
    assert names.info("missing") == (None, None)
    assert names.transcript("missing") is None


def test_session_snapshot_carries_name_and_path(dirs):
    state, projects = dirs
    transcript(projects, "s", [{"type": "user", "cwd": "/w/api"}, {"type": "custom-title", "customTitle": "T"}])
    write(state, "s", f"working {LIVE} api\n")
    [session] = StateReader(state, projects).poll()
    assert (session.title, session.path) == ("T", "/w/api")


# MARK: Subagents

def agents_folder(state, sid: str, agents: dict, started: float | None = None):
    folder = state / ".agents" / sid
    folder.mkdir(parents=True, exist_ok=True)
    for name, kind in agents.items():
        (folder / name).write_text(kind + "\n")
        if started is not None:
            os.utime(folder / name, (started, started))
    return folder


def journal(transcript_path, run: str, phases: list[str], finished: int, final: bool = False, mtime=None):
    base = transcript_path.with_suffix("")
    path = base / "subagents" / "workflows" / run / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['{"type":"launched"}']
    lines += [json.dumps({"type": "started", "key": f"k{i}", "agentId": f"x{i}", "label": f"l{i}", "phase": p},
                         separators=(",", ":")) for i, p in enumerate(phases)]
    lines += [json.dumps({"type": "result" if i % 2 else "failed", "key": f"k{i}", "agentId": f"x{i}",
                          "result": {"big": "x" * 100}}, separators=(",", ":")) for i in range(finished)]
    path.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    if final:
        record = base / "workflows" / f"{run}.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}")
    return path


def test_running_agents_counted_and_dot_files_ignored(dirs):
    state, _ = dirs
    folder = agents_folder(state, "s", {"a1": "general-purpose", "a2": "general-purpose"})
    (folder / ".background").write_text("")
    (folder / ".a3").write_text("half-written")
    assert AgentWatcher(state / ".agents").activity("s", None) == Agents(2)


def test_only_the_marker_means_no_agents(dirs):
    state, _ = dirs
    folder = agents_folder(state, "s", {})
    (folder / ".background").write_text("")
    assert AgentWatcher(state / ".agents").activity("s", None) is None


def test_no_folder_means_none(dirs):
    state, _ = dirs
    assert AgentWatcher(state / ".agents").activity("s", None) is None


def test_workflow_phase_and_progress(dirs):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    agents_folder(state, "s", {f"v{i}": "workflow-subagent" for i in range(4)})
    journal(path, "wf_one", ["Review"] * 12 + ["Verify"] * 4, finished=12)
    activity = AgentWatcher(state / ".agents").activity("s", path)
    assert activity == Agents(4, started=16, done=12, detail="Verify")
    assert activity.summary == "4 agents · Verify"
    assert activity.progress == "12/16"


def test_two_live_workflows(dirs):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    agents_folder(state, "s", {"v1": "workflow-subagent", "v2": "workflow-subagent"})
    journal(path, "wf_a", ["A"] * 3, finished=2)
    journal(path, "wf_b", ["B"] * 2, finished=1)
    activity = AgentWatcher(state / ".agents").activity("s", path)
    assert (activity.detail, activity.started, activity.done) == ("2 workflows", 5, 3)


def test_finished_and_stale_runs_are_left_out(dirs):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    now = time.time()
    agents_folder(state, "s", {"v1": "workflow-subagent"}, started=now)
    journal(path, "wf_done", ["Old"], finished=0, final=True)
    journal(path, "wf_crashed", ["Crashed"], finished=0, mtime=now - 3600)
    activity = AgentWatcher(state / ".agents").activity("s", path)
    assert activity == Agents(1)


def test_lone_agent_description(dirs):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    agents_folder(state, "s", {"b1": "Explore"})
    meta = path.with_suffix("") / "subagents" / "agent-b1.meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"agentType": "Explore", "description": "Find every caller of verifyToken"}))
    activity = AgentWatcher(state / ".agents").activity("s", path)
    assert activity.summary == "1 agent · Find every caller of verifyToken"
    assert activity.progress is None


@pytest.mark.parametrize("kind, detail", [("Explore", "Explore"), ("general-purpose", None),
                                          ("workflow-subagent", None), ("", None)])
def test_lone_agent_without_a_note_shows_its_type_unless_generic(dirs, kind, detail):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    agents_folder(state, "s", {"b1": kind})
    assert AgentWatcher(state / ".agents").activity("s", path).detail == detail


def test_several_plain_agents_have_no_detail(dirs):
    state, projects = dirs
    path = transcript(projects, "s", [{"cwd": "/w"}])
    agents_folder(state, "s", {"b1": "Explore", "b2": "Plan"})
    assert AgentWatcher(state / ".agents").activity("s", path) == Agents(2)


def test_agents_follow_the_folder(dirs):
    state, projects = dirs
    watcher = AgentWatcher(state / ".agents")
    folder = agents_folder(state, "s", {"a1": "Explore", "a2": "Explore"})
    assert watcher.activity("s", None).running == 2
    (folder / "a1").unlink()
    os.utime(folder, (time.time() + 5, time.time() + 5))      # a coarse clock can't hide the change
    assert watcher.activity("s", None).running == 1


def test_agents_reach_the_snapshot(dirs):
    state, projects = dirs
    transcript(projects, "s", [{"cwd": "/w"}])
    write(state, "s", f"working {LIVE} p\n")
    agents_folder(state, "s", {"a1": "Explore"})
    [session] = StateReader(state, projects).poll()
    assert session.agents == Agents(1, detail="Explore")


def test_summary_and_progress_strings():
    assert Agents(1).summary == "1 agent"
    assert Agents(3, detail="Verify").summary == "3 agents · Verify"
    assert Agents(2).progress is None
    assert Agents(2, started=16, done=12).progress == "12/16"
    assert Agents(2, started=3, done=5).progress == "3/3"            # never more than started


# MARK: tmux

def test_tmux_locator_finds_the_pane(private_tmux):
    private_tmux.new_session("demo")
    pid = private_tmux.pid_in("demo:0.0")
    locator = TmuxLocator()
    assert wait_for(lambda: locator.location("s", pid), timeout=5) == "demo:0.0"


def test_tmux_locator_outside_tmux(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    child = sleeper(env)
    try:
        locator = TmuxLocator()
        locator.location("s", child.pid)
        time.sleep(0.5)
        assert locator.location("s", child.pid) is None
    finally:
        child.kill()
