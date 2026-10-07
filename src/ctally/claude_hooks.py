"""Add CTally's hooks and status line to Claude Code's settings, or take them out again.

The settings file (~/.claude/settings.json) is merged, never overwritten: other hooks and
settings are left as they are, and a timestamped backup is written before any change.
Earlier CTally hooks, and those from when it was called Claude Pet, are replaced rather
than duplicated, so running it twice is harmless.

The status line is how CTally hears your plan's usage limits as they change. It prints
nothing; a status line you already had is kept, run by CTally's with the same input so it
still shows, and put back as it was when CTally's is taken out.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
import time
from importlib import resources
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
SCRIPT = Path.home() / ".claude" / "hooks" / "ctally.sh"
HOOK = '"$HOME/.claude/hooks/ctally.sh"'
STATUS = '"$HOME/.claude/hooks/ctally-statusline.sh"'    # beside it

# Each event, and the state it reports. No "matcher": these events take none.
EVENTS = {
    "UserPromptSubmit": "working",  # a turn starts
    "PreToolUse": "working",        # a tool is about to run
    "PostToolUse": "working",       # back to work after a permission prompt
    "Stop": "done",                 # the turn is over, unless agents still run behind it
    "Notification": "waiting",      # a permission prompt, or idle waiting for input
    "SessionEnd": "end",            # the session is gone
    "SubagentStart": "agent-start", # a subagent, maybe one of a workflow's, sets off
    "SubagentStop": "agent-stop",   # and comes back
}

# "claude-pet" catches the hooks from before the rename, inline or scripted.
MARKERS = ("ctally", "claude-pet")


class SettingsError(Exception):
    """The settings file can't be read or isn't what we expect; it was left alone."""


def is_ours(hook: dict) -> bool:
    return any(marker in hook.get("command", "") for marker in MARKERS)


def configure(settings: dict, install: bool, statusline: bool = True) -> dict:
    """Settings with every CTally hook removed, then (if installing) added back; the same for
    the status line, unless it's left out."""
    settings = json.loads(json.dumps(settings))  # work on a copy
    _unwrap_status_line(settings)
    if install and statusline:
        _wrap_status_line(settings)
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            # Only our hooks go: others sharing a group with them (merged in by hand) stay.
            theirs = [hook for hook in group.get("hooks", []) if not is_ours(hook)]
            if len(theirs) == len(group.get("hooks", [])):
                kept.append(group)
            elif theirs:
                kept.append({**group, "hooks": theirs})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if install:
        for event, state in EVENTS.items():
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": f"{HOOK} {state}"}]})
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def _unwrap_status_line(settings: dict) -> None:
    """Takes CTally's status line out, putting back the one it ran, if any."""
    line = settings.get("statusLine")
    command = line.get("command") if isinstance(line, dict) else None
    if not isinstance(command, str) or "ctally-statusline" not in command:
        return
    rest = command.split("ctally-statusline.sh", 1)[1].lstrip('"').strip()
    try:
        before = shlex.split(rest)
    except ValueError:
        before = []
    if before:
        settings["statusLine"] = {**line, "command": before[0]}
    else:
        del settings["statusLine"]


def _wrap_status_line(settings: dict) -> None:
    line = settings.get("statusLine")
    command = line.get("command") if isinstance(line, dict) else None
    if isinstance(command, str) and command.strip():
        settings["statusLine"] = {**line, "command": f"{STATUS} {shlex.quote(command)}"}
    else:
        settings["statusLine"] = {"type": "command", "command": STATUS}


def update(install: bool, path: Path = SETTINGS, statusline: bool = True) -> str:
    """Installs or removes the hooks and status line in a settings file; returns what
    happened."""
    settings = {}
    # Only a file that is really not there counts as empty. One that merely can't be seen (no
    # permission, or a sandbox hiding it) would otherwise be replaced by just our hooks.
    try:
        text = path.read_text(encoding="utf-8")
        exists = True
    except FileNotFoundError:
        exists = False
    except OSError as error:
        raise SettingsError(f"Can't read {path} ({error.strerror}); left it alone.") from error
    if exists:
        try:
            settings = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as error:
            raise SettingsError(f"{path} is not valid JSON ({error}); left it alone.") from error
        if not isinstance(settings, dict):
            raise SettingsError(f"{path} does not hold a JSON object; left it alone.")

    updated = configure(settings, install, statusline)
    what = "hooks and status line" if statusline else "hooks"
    if updated == settings and exists:
        return f"Claude Code {what} already {'installed' if install else 'removed'} in {path}"

    notes = []
    if exists:
        notes.append(f"Backed up {path} to {backup(path)}")
    # A symlink (into a dotfiles checkout, say) is written through, not replaced by a file.
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the file, then swap it in, so a crash can't leave half a settings file.
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".settings.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if target.exists():
        shutil.copymode(target, temp)
    os.replace(temp, target)
    notes.append(f"Claude Code {what} {'installed in' if install else 'removed from'} {path}")
    return "\n".join(notes)


def backup(path: Path) -> Path:
    """Copies a file aside, never over an earlier backup, even one made the same second."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy, n = Path(f"{path}.bak.{stamp}"), 1
    while copy.exists():
        n += 1
        copy = Path(f"{path}.bak.{stamp}-{n}")
    shutil.copy2(path, copy)
    return copy


def install_script() -> None:
    """Puts the hook and status line scripts where the settings call them."""
    SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    for target in (SCRIPT, SCRIPT.with_name("ctally-statusline.sh")):
        source = resources.files("ctally").joinpath(f"hooks/{target.name}").read_bytes()
        temp = target.with_name(f".{target.name}.new")
        temp.write_bytes(source)
        temp.chmod(0o755)
        os.replace(temp, target)


def remove_scripts() -> None:
    for name in ("ctally.sh", "ctally-statusline.sh", "claude-pet.sh"):
        (SCRIPT.parent / name).unlink(missing_ok=True)
    try:
        SCRIPT.parent.rmdir()               # only if nothing else lives there
    except OSError:
        pass
