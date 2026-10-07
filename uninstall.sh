#!/bin/bash
# Remove Claude Pet: the app, its hooks in ~/.claude/settings.json (backed up first), the
# hook script, its state files and its preferences. Other Claude Code settings stay as
# they are.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/ClaudePet.app"

echo "==> stopping the pet"
pkill -f "$APP/Contents/MacOS/claude-pet" 2>/dev/null || true

echo "==> unhooking from Claude Code"
/usr/bin/python3 "$ROOT/scripts/configure-hooks.py" uninstall
rm -f "$HOME/.claude/hooks/claude-pet.sh"
rmdir "$HOME/.claude/hooks" 2>/dev/null || true
rm -rf "$HOME/.claude/claude-pet.d"

echo "==> removing ${APP:?}"
rm -rf "${APP:?}"
defaults delete local.claudepet >/dev/null 2>&1 || true

echo "Claude Pet is uninstalled."
echo "If you added it as a login item, remove it in System Settings > General > Login Items."
