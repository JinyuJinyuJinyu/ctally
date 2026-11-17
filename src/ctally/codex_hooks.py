"""Install CTally's observer hooks in Codex's hooks.json, preserving other hooks."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from .claude_hooks import SettingsError, backup

EVENTS = ("SessionStart", "UserPromptSubmit", "PreModelRequest", "PreToolUse",
          "PermissionRequest", "PostToolUse", "Stop", "Interrupt", "SessionEnd")


def home() -> Path:
    return Path(os.environ.get("CTALLY_CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def is_ours(handler: dict) -> bool:
    try:
        args = shlex.split(handler.get("command", ""))
    except ValueError:
        return False
    return any(args[i:i + 2] == ["-m", "ctally.codex"] for i in range(len(args) - 1))


def configure(settings: dict, install: bool, command: str) -> dict:
    updated = json.loads(json.dumps(settings))
    hooks = updated.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError("Codex hooks must be an object; left them alone.")
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                or any(not isinstance(h, dict) or not isinstance(h.get("command", ""), str)
                       for h in group["hooks"]) for group in groups):
            raise SettingsError(f"Codex {event} hooks have an unexpected shape; left them alone.")
        kept = []
        for group in groups:
            handlers = [h for h in group["hooks"] if not is_ours(h)]
            if handlers == group["hooks"]:
                kept.append(group)
            elif handlers:
                kept.append({**group, "hooks": handlers})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if install:
        for event in EVENTS:
            hooks.setdefault(event, []).append({"hooks": [
                {"type": "command", "command": command,
                 "statusMessage": f"CTally · {event}", "timeout": 3}]})
    if hooks:
        updated["hooks"] = hooks
    else:
        updated.pop("hooks", None)
    return updated


def update(install: bool, path: Path | None = None) -> str:
    path = path if path is not None else home() / "hooks.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not install:
            return ""
        text = None
    except OSError as error:
        raise SettingsError(f"Can't read {path} ({error}); left it alone.") from error
    try:
        settings = json.loads(text) if text is not None else {}
    except ValueError as error:
        raise SettingsError(f"{path} is not valid JSON; left it alone.") from error
    if not isinstance(settings, dict):
        raise SettingsError(f"{path} does not hold a JSON object; left it alone.")
    command = shlex.join([sys.executable, "-m", "ctally.codex", str(path.parent.absolute())])
    updated = configure(settings, install, command)
    if updated == settings:
        return f"Codex hooks already {'installed' if install else 'removed'} in {path}"
    notes = [f"Backed up {path} to {backup(path)}"] if text is not None else []
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".ctally-hooks.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        if target.exists():
            shutil.copymode(target, temp)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    notes.append(f"Codex hooks {'installed in' if install else 'removed from'} {path}")
    return "\n".join(notes)
