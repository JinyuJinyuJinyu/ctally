"""Add CTally to tmux, or take it out again.

Installing writes a marked block to your tmux config (~/.tmux.conf, or
~/.config/tmux/tmux.conf if that's the one you use), backed up first:
  prefix + J  jumps to the Claude Code session that needs you
  the status line shows CTally's counts, e.g. "!1 ▶2 ✓5"
A running tmux server picks the change up straight away. Uninstalling removes the block and
undoes both in the running server.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import system
from .claude_hooks import backup

BEGIN = "# >>> ctally >>>"
END = "# <<< ctally <<<"


def config() -> Path:
    home = Path.home()
    legacy, xdg = home / ".tmux.conf", home / ".config" / "tmux" / "tmux.conf"
    return xdg if xdg.is_file() and not legacy.is_file() else legacy


def without_block(text: str) -> str:
    kept, skipping = [], False
    for line in text.splitlines():
        if line.startswith(BEGIN):
            skipping = True
        if not skipping:
            kept.append(line)
        if line.startswith(END):
            skipping = False
    return "\n".join(kept)


def block(ctally: str) -> str:
    return "\n".join([
        f"{BEGIN}  (added by ctally setup --tmux; ctally uninstall removes it)",
        "# prefix + J: jump to the Claude Code session that needs you",
        f"bind-key J run-shell -b \"'{ctally}' jump '#{{pane_id}}' '#{{client_name}}' '#{{socket_path}}'\"",
        # The guard also matches the segment as earlier versions wrote it, so it's never added twice.
        "# CTally's counts at the end of the status line, added only once",
        f"if-shell -F \"#{{m:*ctally*status --tmux*,#{{status-right}}}}\" \"\" "
        f"\"set -ag status-right \\\" #('{ctally}' status --tmux)\\\" ; set -g status-right-length 80\"",
        END,
    ])


def _tmux(*args: str) -> str | None:
    binary = system.tmux_binary()
    return system.run([binary, *args]) if binary else None


def _server_running() -> bool:
    return _tmux("list-sessions") is not None


def install(ctally: str) -> str:
    path = config()
    current = path.read_text() if path.is_file() else ""
    kept = without_block(current).rstrip("\n")
    updated = (kept + "\n" if kept else "") + block(ctally) + "\n"
    notes = []
    if updated == current:
        notes.append(f"tmux already set up in {path}")
    else:
        if path.is_file():
            notes.append(f"Backed up {path} to {backup(path)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
        notes.append(f"tmux set up in {path}: prefix + J jumps, the status line shows the counts")
    if _server_running():
        _tmux("source-file", str(path))
    return "\n".join(notes)


def uninstall() -> str:
    path = config()
    notes = []
    if path.is_file() and BEGIN in path.read_text():
        notes.append(f"Backed up {path} to {backup(path)}")
        updated = without_block(path.read_text())
        if not updated.strip():
            path.unlink()               # it only ever held CTally's block
        else:
            path.write_text(updated.rstrip("\n") + "\n")
        notes.append(f"Removed CTally from {path}")
    if _server_running():
        keys = _tmux("list-keys", "-T", "prefix") or ""
        if any(line.split()[3:4] == ["J"] and "ctally" in line for line in keys.splitlines()):
            _tmux("unbind-key", "J")
        right = (_tmux("show", "-gv", "status-right") or "").rstrip("\n")
        cleaned = re.sub(r" #\([^)]*ctally[^)]* status --tmux\)", "", right)
        if cleaned != right:
            _tmux("set", "-g", "status-right", cleaned)
    return "\n".join(notes)
