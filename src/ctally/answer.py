"""Answering a session from CTally, without going to its terminal.

Whatever Claude Code puts to me, a tool to allow, a question, the review of my answers or a
plan to approve, it shows as numbered choices with a ❯ on the one picked, and a digit answers
with that choice at once. So the choices are read off the session's tmux pane, just as I'd see
them, rather than worked out from what was asked: that keeps CTally in step with whatever
Claude Code offers, version to version. Answering types the digit into the pane, but only once
the same choices are seen to be still on screen, so a keystroke never lands on anything else.

Only for sessions in tmux, whose screen another program can read and type into.
"""
from __future__ import annotations

import re
import threading
from typing import NamedTuple

from . import focus, system

# "❯ 1. Yes", "  2. No", inside a box's sides or not.
OPTION = re.compile(r"^[\s│┃|]*(?P<cursor>[❯›>]\s*)?(?P<number>[1-9])\.\s+(?P<label>\S.*?)[\s│┃|]*$")
# Lines that only draw: rules and box edges.
RULE = re.compile(r"^[\s─━═╌╍┄┅┈┉▔▁│┃|╭╮╰╯┌┐└┘-]*$")
SIDES = re.compile(r"^[\s│┃|]+|[\s│┃|]+$")
# Choices that leave me something to type in Claude Code, which is brought forward for it.
TYPES = re.compile(r"type something|chat about|tell claude|feedback", re.IGNORECASE)

LOOK = 40       # how far up from the bottom of the screen the choices can start
GAP = 4         # lines between two choices: descriptions, a rule
FOOT = 4        # lines under the last choice: hints about keys, where the plan is saved


class Choice(NamedTuple):
    number: int
    label: str          # "Yes, and don't ask again for npm commands"
    types: bool         # leaves me something to type there


class Prompt(NamedTuple):
    question: str                   # "Do you want to proceed?", or "" if none shows
    choices: tuple[Choice, ...]
    cursor: int                     # the choice the ❯ is on

    def same(self, other: Prompt | None) -> bool:
        """Still the same thing asked, wherever the ❯ has moved to since."""
        return other is not None and (other.question, other.choices) == (self.question, self.choices)


def parse(screen: str) -> Prompt | None:
    """The choices at the bottom of a screen of Claude Code, if it's asking me something:
    numbered from 1 with none missing, the ❯ on one of them, and little under the last. What
    Claude writes can hold numbered lists too, but not with the ❯, and not at the bottom."""
    lines = [line.rstrip() for line in screen.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    lines = lines[-LOOK:]
    found = [(index, match) for index, line in enumerate(lines) if (match := OPTION.match(line))]
    if not found:
        return None
    last_index, last = found[-1]
    under = [line for line in lines[last_index + 1:] if line.strip() and not RULE.match(line)]
    if len(under) > FOOT:
        return None

    # The last choice, then each one before it, back to 1, with nothing out of turn between.
    chain = [(last_index, last)]
    want = int(last["number"]) - 1
    for index, match in reversed(found[:-1]):
        if want == 0 or chain[-1][0] - index - 1 > GAP:
            break
        if int(match["number"]) != want:
            return None
        chain.append((index, match))
        want -= 1
    if want != 0:
        return None
    chain.reverse()
    cursors = [int(match["number"]) for _, match in chain if match["cursor"]]
    if len(cursors) != 1:
        return None

    question = ""
    for line in reversed(lines[:chain[0][0]]):
        if line.strip() and not RULE.match(line):
            question = SIDES.sub("", line)
            break
    choices = tuple(Choice(int(m["number"]), m["label"], bool(TYPES.search(m["label"]))) for _, m in chain)
    return Prompt(question, choices, cursors[0])


def read(target: system.TmuxPane) -> Prompt | None:
    """What the session's pane shows me to choose from, if anything."""
    screen = system.run([target.binary, "-S", target.socket, "capture-pane", "-p", "-t", target.pane], timeout=2)
    return parse(screen) if screen else None


def choose(target: system.TmuxPane, asked: Prompt, choice: Choice) -> bool:
    """Types the choice's digit into the pane, if what was asked is still on screen, and says
    whether it did. Answered in the terminal meanwhile, or moved on to something else, it
    types nothing."""
    if not asked.same(read(target)):
        return False
    return system.run([target.binary, "-S", target.socket, "send-keys", "-t", target.pane, "-l",
                       str(choice.number)], timeout=2) is not None


def answer(pid: int, target: system.TmuxPane, asked: Prompt, choice: Choice) -> None:
    """Off the main thread, like everything else that runs tmux on a click. A choice that
    leaves me something to type brings the terminal forward for it."""
    def go() -> None:
        if choose(target, asked, choice) and choice.types:
            focus.focus(pid)
    threading.Thread(target=go, daemon=True).start()
