#!/bin/bash
# Remove CTally: the app, its hooks in ~/.claude/settings.json and its block in your tmux
# config (both backed up first), the hook script, the ctally command, its state files and
# its preferences. Other Claude Code and tmux settings stay as they are.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/CTally.app"

echo "==> stopping CTally"
pkill -f "$APP/Contents/MacOS/ctally" 2>/dev/null || true

echo "==> unhooking from Claude Code"
/usr/bin/python3 "$ROOT/scripts/configure-hooks.py" uninstall
# The claude-pet.* names are what migrate-from-claude-pet.sh left behind, if it ran.
rm -f "$HOME/.claude/hooks/ctally.sh" "$HOME/.claude/hooks/claude-pet.sh"
rmdir "$HOME/.claude/hooks" 2>/dev/null || true
rm -rf "$HOME/.claude/ctally.d"
if [ -L "$HOME/.claude/claude-pet.d" ]; then rm -f "$HOME/.claude/claude-pet.d"; fi

echo "==> taking CTally out of tmux"
"$ROOT/scripts/configure-tmux.sh" uninstall
rm -f "$HOME/.local/bin/ctally"

echo "==> removing ${APP:?}"
rm -rf "${APP:?}"
defaults delete io.github.jinyujinyujinyu.ctally >/dev/null 2>&1 || true

echo "CTally is uninstalled."
echo "If you added it as a login item, remove it in System Settings > General > Login Items."
