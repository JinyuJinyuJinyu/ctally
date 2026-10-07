#!/usr/bin/env python3
"""Add CTally's hooks to Claude Code's settings, or take them out again.

    configure-hooks.py install   [path/to/settings.json]
    configure-hooks.py uninstall [path/to/settings.json]

The settings file defaults to ~/.claude/settings.json. It is merged, never overwritten:
other hooks and settings are left as they are, and a timestamped backup is written before
any change. Earlier CTally hooks, and those from when it was called Claude Pet, are
replaced rather than duplicated, so running it twice is harmless.
"""
import json
import os
import shutil
import sys
import tempfile
import time

HOOK = '"$HOME/.claude/hooks/ctally.sh"'

# Each event, and the state it reports. No "matcher": these events take none.
EVENTS = {
    "UserPromptSubmit": "working",  # a turn starts
    "PreToolUse": "working",        # a tool is about to run
    "PostToolUse": "working",       # back to work after a permission prompt
    "Stop": "done",                 # the turn is over
    "Notification": "waiting",      # a permission prompt, or idle waiting for input
    "SessionEnd": "end",            # the session is gone
}


# "claude-pet" catches the hooks from before the rename, inline or scripted.
MARKERS = ("ctally", "claude-pet")


def is_ours(hook):
    return any(marker in hook.get("command", "") for marker in MARKERS)


def configure(settings, install):
    """Settings with every CTally hook removed, then (if installing) added back."""
    settings = json.loads(json.dumps(settings))  # work on a copy
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


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        sys.exit(__doc__)
    install = sys.argv[1] == "install"
    path = os.path.expanduser(sys.argv[2] if len(sys.argv) > 2 else "~/.claude/settings.json")

    settings = {}
    # Only a file that is really not there counts as empty. One that merely can't be seen (no
    # permission, or a sandbox hiding it) would otherwise be replaced by just our hooks.
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        exists = True
    except FileNotFoundError:
        exists = False
    except OSError as error:
        sys.exit(f"Can't read {path} ({error.strerror}); left it alone.")
    if exists:
        try:
            settings = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as error:
            sys.exit(f"{path} is not valid JSON ({error}); left it alone.")
        if not isinstance(settings, dict):
            sys.exit(f"{path} does not hold a JSON object; left it alone.")

    updated = configure(settings, install)
    if updated == settings and exists:
        print(f"Claude Code hooks already {'installed' if install else 'removed'} in {path}")
        return

    if exists:
        # Never overwrite an earlier backup, even one made the same second.
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup, n = f"{path}.bak.{stamp}", 1
        while os.path.exists(backup):
            n += 1
            backup = f"{path}.bak.{stamp}-{n}"
        shutil.copy2(path, backup)
        print(f"Backed up {path} to {backup}")
    # A symlink (into a dotfiles checkout, say) is written through, not replaced by a file.
    target = os.path.realpath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Write beside the file, then swap it in, so a crash can't leave half a settings file.
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".settings.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.path.exists(target):
        shutil.copymode(target, temp)
    os.replace(temp, target)
    print(f"Claude Code hooks {'installed in' if install else 'removed from'} {path}")


if __name__ == "__main__":
    main()
