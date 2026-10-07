#!/bin/bash
# Build Claude Pet and install it: ~/Applications/ClaudePet.app, plus the Claude Code hooks
# that feed it. Safe to re-run: it replaces the app and keeps your preferences.
#
#   ./install.sh              build, install, hook into Claude Code, start the pet
#   ./install.sh --no-hooks   leave ~/.claude/settings.json alone
#   ./install.sh --no-launch  install without starting the pet
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
APP="$HOME/Applications/ClaudePet.app"
hooks=1
launch=1
for arg in "$@"; do
  case "$arg" in
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
swiftc -O "$ROOT/claude-pet.swift" -o "$BUILD/claude-pet"
if [ ! -f "$BUILD/ClaudePet.icns" ]; then
  swiftc -O "$ROOT/make-icon.swift" -o "$BUILD/make-icon"
  "$BUILD/make-icon" "$BUILD/ClaudePet.icns"
fi

echo "==> installing $APP"
if pkill -f "$APP/Contents/MacOS/claude-pet" 2>/dev/null; then sleep 1; fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/claude-pet" "$APP/Contents/MacOS/claude-pet"
cp "$BUILD/ClaudePet.icns" "$APP/Contents/Resources/ClaudePet.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key><string>ClaudePet</string>
	<key>CFBundleDisplayName</key><string>Claude Pet</string>
	<key>CFBundleIdentifier</key><string>local.claudepet</string>
	<key>CFBundleExecutable</key><string>claude-pet</string>
	<key>CFBundleIconFile</key><string>ClaudePet</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSMinimumSystemVersion</key><string>12.0</string>
	<key>LSUIElement</key><true/>
	<key>NSHighResolutionCapable</key><true/>
	<key>NSAppleEventsUsageDescription</key><string>Clicking a session in Claude Pet brings its Terminal tab to the front.</string>
</dict>
</plist>
PLIST
# Nudge Launch Services so Finder picks up the new icon and bundle info.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

if [ "$hooks" = 1 ]; then
  echo "==> hooking into Claude Code"
  mkdir -p "$HOME/.claude/hooks"
  cp "$ROOT/hooks/claude-pet.sh" "$HOME/.claude/hooks/claude-pet.sh"
  chmod +x "$HOME/.claude/hooks/claude-pet.sh"
  /usr/bin/python3 "$ROOT/scripts/configure-hooks.py" install
  echo "    Restart any Claude Code sessions already running so they pick up the hooks."
fi

if [ "$launch" = 1 ]; then
  echo "==> starting"
  open "$APP"
  sleep 1
  if pgrep -f "$APP/Contents/MacOS/claude-pet" >/dev/null; then
    echo "Claude Pet is running. Look for the hexagon in the menu bar."
  else
    echo "Claude Pet did not start." >&2
    exit 1
  fi
fi
