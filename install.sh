#!/bin/bash
# Build CTally and install it: ~/Applications/CTally.app, plus the Claude Code hooks that
# feed it. Safe to re-run: it replaces the app and keeps your preferences.
#
#   ./install.sh              build, install, hook into Claude Code, start CTally
#   ./install.sh --tmux       also set up tmux: prefix + J jumps to the session that needs
#                             you, and the status line shows the counts
#   ./install.sh --no-hooks   leave ~/.claude/settings.json alone
#   ./install.sh --no-launch  install without starting CTally
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
APP="$HOME/Applications/CTally.app"
CLI="$HOME/.local/bin/ctally"
BUNDLE_ID="io.github.jinyujinyujinyu.ctally"
hooks=1
launch=1
tmux=0
for arg in "$@"; do
  case "$arg" in
    --tmux) tmux=1 ;;
    --no-hooks) hooks=0 ;;
    --no-launch) launch=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v swiftc >/dev/null; then
  echo "swiftc not found. Install the Command Line Tools first: xcode-select --install" >&2
  exit 1
fi

echo "==> building"
mkdir -p "$BUILD"
swiftc -O "$ROOT/ctally.swift" -o "$BUILD/ctally"
if [ ! -f "$BUILD/CTally.icns" ]; then
  swiftc -O "$ROOT/make-icon.swift" -o "$BUILD/make-icon"
  "$BUILD/make-icon" "$BUILD/CTally.icns"
fi

echo "==> installing $APP"
if pkill -f "$APP/Contents/MacOS/ctally" 2>/dev/null; then sleep 1; fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/ctally" "$APP/Contents/MacOS/ctally"
cp "$BUILD/CTally.icns" "$APP/Contents/Resources/CTally.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key><string>CTally</string>
	<key>CFBundleDisplayName</key><string>CTally</string>
	<key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
	<key>CFBundleExecutable</key><string>ctally</string>
	<key>CFBundleIconFile</key><string>CTally</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSMinimumSystemVersion</key><string>12.0</string>
	<key>LSUIElement</key><true/>
	<key>NSHighResolutionCapable</key><true/>
	<key>NSAppleEventsUsageDescription</key><string>Clicking a session in CTally brings its Terminal tab to the front.</string>
</dict>
</plist>
PLIST
# Nudge Launch Services so Finder picks up the new icon and bundle info.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

# Coming from Claude Pet, this project's earlier name? Carry everything over.
"$ROOT/scripts/migrate-from-claude-pet.sh" "$BUNDLE_ID" "$APP"

if [ "$hooks" = 1 ]; then
  echo "==> hooking into Claude Code"
  mkdir -p "$HOME/.claude/hooks"
  cp "$ROOT/hooks/ctally.sh" "$HOME/.claude/hooks/ctally.sh"
  chmod +x "$HOME/.claude/hooks/ctally.sh"
  /usr/bin/python3 "$ROOT/scripts/configure-hooks.py" install
  echo "    Restart any Claude Code sessions already running so they pick up the hooks."
fi

echo "==> installing the ctally command at $CLI"
mkdir -p "$(dirname "$CLI")"
cp "$ROOT/bin/ctally" "$CLI"
chmod +x "$CLI"

if [ "$tmux" = 1 ]; then
  echo "==> setting up tmux"
  "$ROOT/scripts/configure-tmux.sh" install "$CLI"
elif command -v tmux >/dev/null; then
  echo "    Using tmux? ./install.sh --tmux adds a jump key and status-line counts."
fi

if [ "$launch" = 1 ]; then
  echo "==> starting"
  open "$APP"
  sleep 1
  if pgrep -f "$APP/Contents/MacOS/ctally" >/dev/null; then
    echo "CTally is running. Look for the hexagon in the menu bar."
  else
    echo "CTally did not start." >&2
    exit 1
  fi
fi
