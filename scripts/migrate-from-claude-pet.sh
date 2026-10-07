#!/bin/bash
# One-time move from Claude Pet, this project's earlier name, to CTally. install.sh runs it
# after installing CTally.app; on a machine that never had Claude Pet it does nothing.
#
#   migrate-from-claude-pet.sh <CTally bundle id> <path to CTally.app>
#
# Claude Code sessions that are already running keep the hooks they started with, so the
# old names are kept working rather than removed: the old state folder becomes a link to the
# new one, and the old hook script becomes a forwarder to the new one. Restarted sessions
# use CTally's hooks directly.
set -euo pipefail

NEW_ID="$1"
NEW_APP="$2"
OLD_APP="$HOME/Applications/ClaudePet.app"
OLD_ID="local.claudepet"
OLD_STATE="$HOME/.claude/claude-pet.d"
NEW_STATE="$HOME/.claude/ctally.d"
OLD_HOOK="$HOME/.claude/hooks/claude-pet.sh"

if [ -d "$OLD_APP" ]; then
  echo "==> moving on from Claude Pet"
  pkill -f "$OLD_APP/Contents/MacOS/claude-pet" 2>/dev/null || true
  rm -rf "${OLD_APP:?}"
  # A login item for the old app would now point at nothing: point it at CTally instead.
  osascript -e 'tell application "System Events"' \
            -e '  if exists login item "ClaudePet" then' \
            -e '    delete login item "ClaudePet"' \
            -e "    make login item at end with properties {path:\"$NEW_APP\", hidden:false}" \
            -e '  end if' \
            -e 'end tell' >/dev/null 2>&1 || true
fi

# Preferences carry over, losing the old "Pet" prefix on their names.
/usr/bin/python3 - "$OLD_ID" "$NEW_ID" <<'PY'
import plistlib, subprocess, sys
old_id, new_id = sys.argv[1:3]

def export(domain):
    out = subprocess.run(["defaults", "export", domain, "-"], capture_output=True).stdout
    try:
        return plistlib.loads(out) if out else {}
    except Exception:
        return {}

old, new = export(old_id), export(new_id)
if old and not new:
    moved = {(k[3:] if k.startswith("Pet") else k): v for k, v in old.items()}
    subprocess.run(["defaults", "import", new_id, "-"], input=plistlib.dumps(moved), check=True)
    print("    carried over preferences: " + ", ".join(sorted(moved)))
if old:
    subprocess.run(["defaults", "delete", old_id], capture_output=True)
PY

# Session files move to the new folder; the old one becomes a link to it.
if [ -d "$OLD_STATE" ] && [ ! -L "$OLD_STATE" ]; then
  mkdir -p "$NEW_STATE"
  find "$OLD_STATE" -mindepth 1 -maxdepth 1 -type f -exec mv -f {} "$NEW_STATE/" \;
  if rmdir "$OLD_STATE" 2>/dev/null; then
    ln -s "$NEW_STATE" "$OLD_STATE"
    echo "    session files moved to $NEW_STATE"
  fi
fi

# The old hook script forwards to the new one.
if [ -f "$OLD_HOOK" ] && ! grep -q "ctally.sh" "$OLD_HOOK"; then
  cat > "$OLD_HOOK" <<'SH'
#!/bin/sh
# Claude Pet is now CTally: sessions started before the rename still call this name.
[ -x "$HOME/.claude/hooks/ctally.sh" ] && exec "$HOME/.claude/hooks/ctally.sh" "$@"
exit 0
SH
  chmod +x "$OLD_HOOK"
fi
exit 0
