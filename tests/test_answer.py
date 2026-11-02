"""Answering from CTally: the choices read off a Claude Code screen, and the key typed into its
tmux pane only while the same choices are still there. The screens are as Claude Code 2.1.316
drew them, captured with tmux."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import ROOT, wait_for

from ctally import answer, system
from ctally.answer import Choice, Prompt, parse

PERMISSION = """\
❯ Create a file named probe.txt containing the word hello, using the Write tool. Do nothing else.

⏺ Write(probe.txt)

──────────────────────────────────────────────────────────────────────────────────────
 Create file
 probe.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hello
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create probe.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend


"""

QUESTION = """\
❯ Use the AskUserQuestion tool once, with two questions in the same call.
──────────────────────────────────────────────────────────────────────────────────────
←  ☒ Colour  ☐ Size  ✔ Submit  →

Which size?

❯ 1. Small
     Small
  2. Large
     Large
  3. Type something.
──────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

REVIEW = """\
←  ☒ Colour  ☒ Size  ✔ Submit  →

Review your answers

 ● Which colour do you prefer?
   → Red
 ● Which size?
   → Large

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel
"""

PLAN = """\
  ──────────────────────────────────────────────────────────────────────────────────
   Ready to code?

   Here is Claude's plan:
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
   Create /private/tmp/ctxprobe/plan.txt containing the text x using the Write tool.
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  ──────────────────────────────────────────────────────────────────────────────────

   Claude has written up a plan and is ready to execute. Would you like to proceed?

   ❯ 1. Yes, and use auto mode
     2. Yes, manually approve edits
     3. Tell Claude what to change
        shift+tab to approve with this feedback

   ctrl+g to edit in Vim · ~/.claude/plans/make-a-plan-to-wiggly-blossom.md
"""

# The folder-trust question has no numbers: it waits for Enter, so it isn't answered from here.
TRUST = """\
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

# Idle: Claude's answer had a numbered list, and the prompt is empty.
IDLE = """\
⏺ Two ways to go:
  1. Retry the flaky tests
  2. Skip them for now
✻ Brewed for 6s · done 11:51 AM
──────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on
"""


def test_a_permission():
    prompt = parse(PERMISSION)
    assert prompt.question == "Do you want to create probe.txt?"
    assert [c.number for c in prompt.choices] == [1, 2, 3] and prompt.cursor == 1
    assert prompt.choices[0] == Choice(1, "Yes", False)
    assert prompt.choices[1].label.startswith("Yes, and switch to accept edits")
    assert prompt.choices[2] == Choice(3, "No", False)


def test_a_question_mid_form():
    prompt = parse(QUESTION)
    assert prompt.question == "Which size?"
    assert [c.label for c in prompt.choices] == ["Small", "Large", "Type something.", "Chat about this"]
    assert [c.types for c in prompt.choices] == [False, False, True, True]


def test_the_review_of_my_answers():
    prompt = parse(REVIEW)
    assert prompt.question == "Ready to submit your answers?"
    assert [c.label for c in prompt.choices] == ["Submit answers", "Cancel"]


def test_a_plan():
    prompt = parse(PLAN)
    assert prompt.question.endswith("Would you like to proceed?")
    assert [c.label for c in prompt.choices] == ["Yes, and use auto mode", "Yes, manually approve edits",
                                                "Tell Claude what to change"]
    assert prompt.choices[2].types and not prompt.choices[0].types


def test_nothing_to_answer():
    assert parse(TRUST) is None                  # no numbers
    assert parse(IDLE) is None                   # a numbered list, but no ❯ on it
    assert parse("") is None


def test_the_cursor_can_be_anywhere_but_must_be_somewhere():
    moved = PERMISSION.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No")
    assert parse(moved).cursor == 3
    assert parse(PERMISSION.replace(" ❯ 1. Yes", "   1. Yes")) is None


def test_choices_must_run_from_one_without_a_gap():
    assert parse(PERMISSION.replace("   2. Yes, and switch", "   5. Yes, and switch")) is None
    assert parse(PERMISSION.replace(" ❯ 1. Yes", " ❯ 7. Yes")) is None


def test_choices_high_up_with_a_lot_under_them_are_not_a_prompt():
    assert parse(PERMISSION.replace("Esc to cancel", "\n".join(f"line {i}" for i in range(8)))) is None


def test_a_box_around_the_choices():
    boxed = "╭──────────────╮\n│ Do you want to proceed?  │\n│ ❯ 1. Yes     │\n│   2. No      │\n╰──────────────╯\n"
    prompt = parse(boxed)
    assert prompt.question == "Do you want to proceed?"
    assert [c.label for c in prompt.choices] == ["Yes", "No"]


def test_same_means_same_question_and_choices_wherever_the_cursor():
    a = parse(PERMISSION)
    b = parse(PERMISSION.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No"))
    assert a.same(b) and not a.same(parse(REVIEW)) and not a.same(None)


# MARK: Typing the answer into tmux

DIALOG = textwrap.dedent('''
    import sys, termios, time, tty
    screen, out = sys.argv[1], sys.argv[2]
    sys.stdout.write(open(screen).read())
    sys.stdout.flush()
    tty.setcbreak(sys.stdin.fileno())
    key = sys.stdin.read(1)
    open(out, "w").write(key)
    sys.stdout.write("\\nanswered " + key + "\\n")
    sys.stdout.flush()
    time.sleep(600)
''')


@pytest.fixture
def dialog(private_tmux, short_tmp):
    """A pane that shows a Claude Code permission dialog and keeps the first key it gets."""
    screen, out = short_tmp / "screen.txt", short_tmp / "key"
    screen.write_text(PERMISSION.rstrip() + "\n")
    private_tmux("-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "140", "-y", "40",
                 sys.executable, "-c", DIALOG, str(screen), str(out))
    pid = private_tmux.pid_in("work:0.0")
    target = system.tmux_pane(pid)
    assert target is not None
    wait_for(lambda: answer.read(target))
    return target, out, pid


def test_reading_and_answering_a_pane(dialog):
    target, out, _ = dialog
    asked = answer.read(target)
    assert asked == parse(PERMISSION)
    assert answer.choose(target, asked, asked.choices[1])
    assert wait_for(lambda: out.exists() and out.read_text()) == "2"


def test_nothing_is_typed_once_something_else_is_asked(dialog):
    target, out, _ = dialog
    other = Prompt("Do you want to proceed?", (Choice(1, "Yes", False), Choice(2, "No", False)), 1)
    assert not answer.choose(target, other, other.choices[0])
    time.sleep(0.3)
    assert not out.exists()


def test_the_controller_menu_answers_the_pane(dialog):
    """Right-click on a waiting session: what it asks, then the choices from its screen;
    picking one types its digit. Qt in a process of its own, as in the other view tests."""
    target, out, pid = dialog
    script = textwrap.dedent('''
        import os, sys, time
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        sys.path[:0] = [sys.argv[1] + "/src"]
        from PySide6.QtNetwork import QLocalServer
        from PySide6.QtWidgets import QApplication, QMenu
        app = QApplication([])
        from ctally.gui.app import Controller, _menu_text
        from ctally.sessions import Ask, Session, State
        c = Controller(app, QLocalServer())
        ask = Ask("tool", "Allow Write(probe.txt)?")
        session = Session("s", State.WAITING, "work", int(sys.argv[2]), ask=ask)
        menu = QMenu()
        assert c._add_answers(menu, session)
        texts = [a.text() for a in menu.actions()]
        assert texts[0] == "Allow Write(probe.txt)?" and not menu.actions()[0].isEnabled(), texts
        assert texts[1] == "1. Yes" and texts[3] == "3. No", texts
        menu.actions()[3].trigger()
        # Outside tmux: what's known, and the terminal.
        menu = QMenu()
        quiet = Session("q", State.WAITING, "work", 0, ask=Ask("question", "Red or blue?", options=("Red", "Blue")))
        assert not c._add_answers(menu, quiet)
        assert [a.text() for a in menu.actions()] == ["Red or blue?", "· Red", "· Blue", "Answer in Its Terminal"]
        assert _menu_text("Fish & chips " + "x" * 90).startswith("Fish && chips") and _menu_text("a" * 90).endswith("…")
        time.sleep(1)
        print("ok")
    ''')
    done = subprocess.run([sys.executable, "-c", script, str(ROOT), str(pid)], capture_output=True, text=True,
                          timeout=60, env={**os.environ, "CTALLY_STATE_DIR": str(out.parent)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
    assert wait_for(lambda: out.exists() and out.read_text()) == "3"
