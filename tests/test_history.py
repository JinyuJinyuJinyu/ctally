"""`ctally stats`: the hook's history worked out into time and counts, day by day."""
from __future__ import annotations

import os
import time
from datetime import date, datetime

import pytest

from ctally import cli, history
from ctally.history import Line, stats, stretches


def at(hour: int, minute: int = 0, day: int = 28) -> float:
    """A local time on a Wednesday, Oct 28 2026."""
    return datetime(2026, 10, day, hour, minute).timestamp()


NOON = at(12)


def write(folder, lines: list[Line]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for line in lines:
        name = datetime.fromtimestamp(line.when).date().isoformat()
        with open(folder / name, "a") as f:
            f.write(f"{int(line.when)} {line.session} {line.state}{' prompt' if line.prompt else ''}\n")


A_MORNING = [
    Line(at(10), "s1", "working", True),
    Line(at(10, 15), "s2", "working", True),
    Line(at(10, 30), "s1", "waiting"),
    Line(at(10, 32), "s1", "working"),
    Line(at(10, 45), "s2", "done"),
    Line(at(11), "s1", "done"),
    Line(at(11, 5), "s1", "working", True),
    Line(at(11, 20), "s1", "done"),
]


def test_a_morning(tmp_path):
    write(tmp_path, A_MORNING)
    found = stats(7, NOON, tmp_path, running={})
    today = found.days[-1]
    assert (today.day, len(found.days)) == (date(2026, 10, 28), 7)
    assert today.working == (30 + 28 + 15 + 30) * 60
    assert today.waiting == 2 * 60
    assert (today.prompts, today.turns, len(today.sessions), today.limits) == (3, 3, 2, 0)
    assert found.answered == 120 and found.came_back == 300
    assert (found.peak, found.peak_at) == (2, at(10, 15))
    assert found.busy == (60 + 15) * 60                 # 10:00–11:00 and 11:05–11:20, one at least
    assert found.since == date(2026, 10, 28)
    assert all(d.working == 0 and not d.sessions for d in found.days[:-1])


def test_lines_that_only_repeat_the_state_join_up(tmp_path):
    lines = [Line(at(9), "s", "working", True), Line(at(9, 5), "s", "working"), Line(at(9, 10), "s", "working"),
             Line(at(9, 12), "s", "waiting"), Line(at(9, 17), "s", "waiting"), Line(at(9, 20), "s", "working")]
    spans = stretches(lines, NOON, {})
    assert [(s.state, s.start, s.end, s.settled) for s in spans] == [
        ("working", at(9), at(9, 12), True), ("waiting", at(9, 12), at(9, 20), True),
        ("working", at(9, 20), at(9, 25), False)]
    write(tmp_path, lines)
    assert stats(1, NOON, tmp_path, running={}).answered == 8 * 60      # one wait, not two


def test_after_the_last_line(tmp_path):
    lines = [Line(at(11), "gone", "working", True), Line(at(11), "here", "working", True),
             Line(at(11, 30), "asks", "waiting")]
    write(tmp_path, lines)
    found = stats(1, NOON, tmp_path, running={"here": "working", "asks": "waiting"})
    # Gone without a word: a few minutes at most. Still at it: until now.
    assert found.days[0].working == (5 + 60) * 60
    assert found.days[0].waiting == 30 * 60
    assert found.answered is None                         # still waiting: not answered yet


def test_a_long_silence_is_capped(tmp_path):
    # Working at 9, then nothing until 1 pm: the computer slept; an hour at most is counted.
    write(tmp_path, [Line(at(9), "s", "working", True), Line(at(13), "s", "done")])
    assert stats(1, at(14), tmp_path, running={}).days[0].working == 3600


def test_midnight_splits_a_stretch(tmp_path):
    write(tmp_path, [Line(at(23, 30, day=27), "s", "working", True), Line(at(0, 30), "s", "done")])
    found = stats(2, NOON, tmp_path, running={})
    assert [d.working for d in found.days] == [30 * 60, 30 * 60]
    assert [d.prompts for d in found.days] == [1, 0] and [d.turns for d in found.days] == [0, 1]
    # Asked for today only, it still knows the session was working as the day began.
    assert stats(1, NOON, tmp_path, running={}).days[0].working == 30 * 60


def test_the_usage_limit(tmp_path):
    write(tmp_path, [Line(at(9), "s", "working", True), Line(at(9, 30), "s", "limited"),
                     Line(at(9, 40), "s", "limited"), Line(at(11), "s", "working", True), Line(at(11, 10), "s", "done")])
    day = stats(1, NOON, tmp_path, running={}).days[0]
    assert (day.limits, day.limited, day.prompts) == (1, 90 * 60, 2)


def test_coming_back_counts_only_within_a_while(tmp_path):
    write(tmp_path, [Line(at(8), "s", "working", True), Line(at(8, 10), "s", "done"),
                     Line(at(11), "s", "working", True), Line(at(11, 5), "s", "done")])
    assert stats(1, NOON, tmp_path, running={}).came_back is None       # nearly three hours: gone, not slow


def test_the_report(tmp_path):
    write(tmp_path, A_MORNING)
    found = stats(3, NOON, tmp_path, running={})
    lines = history.report(found, NOON)
    assert lines[0].split() == ["working", "waiting", "on", "you", "prompts", "turns", "sessions", "limit", "hits"]
    assert lines[1].split() == ["Mon", "Oct", "26", "-", "-", "-", "-", "-", "-"]
    assert lines[3].split() == ["Today", "1h", "43m", "2m", "3", "3", "2", "-"]
    assert lines[4].split()[:4] == ["Last", "3", "days", "1h"]
    assert "Claude worked 1h 43m in all, with up to 2 sessions at once (today 10:15 am); " \
           "something was working for 1h 15m." in lines
    assert "You answered what it asked in 2m, and came back to finished turns in 5m (medians)." in lines
    assert lines[-1] == "History since Oct 28."
    data = history.as_json(found)
    assert data["total"]["working"] == 103 * 60 and data["days"][-1]["sessions"] == 2


def test_durations():
    assert [history.duration(s) for s in (0, 0.4, 45, 60, 18 * 60 + 59, 3600, 5 * 3600 + 12 * 60, 30 * 3600)] == [
        "-", "-", "45s", "1m", "18m", "1h 00m", "5h 12m", "30h 00m"]


def test_reading_skips_junk_and_other_files(tmp_path):
    (tmp_path / "2026-10-28").write_text("1793580000 s working prompt\nnonsense\n\n17935 s\nx s done\n")
    (tmp_path / ".s").write_text("1793580000 3\n")
    (tmp_path / "notes.txt").write_text("1793580000 s working\n")
    assert history.read(tmp_path) == [Line(1793580000.0, "s", "working", True)]
    assert history.read(tmp_path / "missing") == []


def test_prune_keeps_ninety_days_and_live_marks(tmp_path):
    now = NOON
    for name in ("2026-07-29", "2026-07-30", "2026-10-28"):
        (tmp_path / name).write_text("")
    (tmp_path / ".old").write_text("1 0\n")
    os.utime(tmp_path / ".old", (now - 2 * 86400, now - 2 * 86400))
    (tmp_path / ".fresh").write_text("1 0\n")
    os.utime(tmp_path / ".fresh", (now - 60, now - 60))
    history.prune(tmp_path, now)
    assert sorted(os.listdir(tmp_path)) == [".fresh", "2026-07-30", "2026-10-28"]


def test_live_sessions_from_the_state_files(tmp_path):
    (tmp_path / "mine").write_text(f"waiting {os.getpid()} api\n")
    (tmp_path / "junk").write_text("x\n")
    (tmp_path / ".history").mkdir()
    assert history.live(tmp_path) == {"mine": "waiting"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    folder = tmp_path / "ctally.d"
    (folder / ".history").mkdir(parents=True)
    monkeypatch.setattr(history, "HISTORY_DIR", folder / ".history")
    monkeypatch.setattr(history, "STATE_DIR", folder)
    return folder


def test_the_command(state, capsys):
    assert cli.main(["stats"]) == 1
    assert "no history yet" in capsys.readouterr().err
    now = time.time()
    write(state / ".history", [Line(now - 600, "s", "working", True), Line(now - 300, "s", "done")])
    assert cli.main(["stats", "--days", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2].startswith("Today") and out[2].split()[1:3] == ["5m", "-"], out
    assert cli.main(["stats", "--json"]) == 0
    import json
    assert json.loads(capsys.readouterr().out)["total"]["prompts"] == 1
    assert cli.main(["stats", "--days", "91"]) == 2
