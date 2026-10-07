"""`ctally setup` and `ctally uninstall`: the Claude Code hooks, tmux, starting at login,
and moving over from the Swift app CTally used to be on macOS."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import claude_hooks, prefs, system, tmux_conf
from .sessions import STATE_DIR

LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{prefs.APP_ID}.plist"
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "ctally.desktop"
OLD_APP = Path.home() / "Applications" / "CTally.app"


def say(text: str) -> None:
    if text:
        print(text)


def command() -> str:
    """This ctally, as a command a login item or tmux can run."""
    invoked = Path(sys.argv[0]).absolute()
    if invoked.name == "ctally" and os.access(invoked, os.X_OK):
        return str(invoked)
    found = shutil.which("ctally")
    if found:
        return found
    sys.exit("Run this as the `ctally` command (pipx install puts it on your PATH).")


def setup(tmux: bool, hooks: bool, autostart: bool, launch: bool) -> int:
    ctally = command()
    if system.MAC and OLD_APP.exists():
        print("==> replacing the old CTally.app")
        retire_old_app()

    if hooks:
        print("==> hooking into Claude Code")
        claude_hooks.install_script()
        try:
            say(claude_hooks.update(install=True))
        except claude_hooks.SettingsError as error:
            print(error, file=sys.stderr)
            return 1
        print("    Restart any Claude Code sessions already running so they pick up the hooks.")

    if tmux:
        print("==> setting up tmux")
        say(tmux_conf.install(ctally))
    elif system.tmux_binary():
        print("    tmux user? `ctally setup --tmux` adds a jump key (prefix + J) and status-line counts.")

    # A running indicator is the old code: let it go, so the new one can take over.
    if prefs.tell_running("quit"):
        time.sleep(0.5)
    if autostart:
        print("==> starting at login")
        say(install_autostart(ctally, start_now=launch))
    elif launch:
        start(ctally)
    if launch:
        print("CTally is running." + (" Look for the hexagon in the menu bar." if system.MAC else ""))
    if system.LINUX and launch and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("    No desktop session here: CTally starts at your next login, or run `ctally run` there.")
    return 0


def uninstall() -> int:
    if prefs.tell_running("quit"):
        print("Stopped CTally")
    if system.MAC:
        if LAUNCH_AGENT.exists():
            launchctl("bootout", f"gui/{os.getuid()}/{prefs.APP_ID}")
            LAUNCH_AGENT.unlink()
            print(f"Removed {LAUNCH_AGENT}")
        if OLD_APP.exists():
            retire_old_app()
    elif AUTOSTART.exists():
        AUTOSTART.unlink()
        print(f"Removed {AUTOSTART}")

    try:
        say(claude_hooks.update(install=False))
    except claude_hooks.SettingsError as error:
        print(error, file=sys.stderr)
    claude_hooks.remove_scripts()
    say(tmux_conf.uninstall())

    old = Path.home() / ".claude" / "claude-pet.d"
    if old.is_symlink():
        old.unlink()
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    shutil.rmtree(prefs.config_dir(), ignore_errors=True)
    print("Removed CTally's hooks, state and settings. To remove the command too: pipx uninstall ctally")
    return 0


# MARK: Starting at login

def launchctl(*args: str) -> bool:
    return subprocess.run(["launchctl", *args], capture_output=True).returncode == 0


def install_autostart(ctally: str, start_now: bool) -> str:
    if system.MAC:
        # A LaunchAgent: run at login, and right away when loaded.
        agent = {
            "Label": prefs.APP_ID,
            "ProgramArguments": [ctally, "run"],
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "StandardOutPath": str(prefs.log_file()),
            "StandardErrorPath": str(prefs.log_file()),
        }
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        prefs.log_file().parent.mkdir(parents=True, exist_ok=True)
        with open(LAUNCH_AGENT, "wb") as f:
            plistlib.dump(agent, f)
        domain = f"gui/{os.getuid()}"
        launchctl("bootout", f"{domain}/{prefs.APP_ID}")
        if start_now:
            launchctl("bootstrap", domain, str(LAUNCH_AGENT))
        return f"Starts at login: {LAUNCH_AGENT}"

    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text("\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=CTally",
        "Comment=A status light for your Claude Code sessions",
        f'Exec="{ctally}" run',
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
        "",
    ]))
    if start_now:
        start(ctally)
    return f"Starts at login: {AUTOSTART}"


def start(ctally: str) -> None:
    """Starts the indicator detached from this terminal."""
    if system.LINUX and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    log = prefs.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        subprocess.Popen([ctally, "run"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)


# MARK: The Swift app this replaces

def retire_old_app() -> None:
    """Quits and removes ~/Applications/CTally.app and its login item, keeping its settings."""
    subprocess.run(["pkill", "-f", f"{OLD_APP}/Contents/MacOS/"], capture_output=True)
    subprocess.run(["osascript", "-e", 'tell application "System Events" to delete '
                    '(every login item whose name is "CTally")'], capture_output=True)
    migrate_old_prefs()
    shutil.rmtree(OLD_APP, ignore_errors=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                    "/Support/lsregister", "-u", str(OLD_APP)], capture_output=True)
    print(f"Removed {OLD_APP}; its settings carry over")


def migrate_old_prefs() -> None:
    exported = subprocess.run(["defaults", "export", prefs.APP_ID, "-"], capture_output=True)
    if exported.returncode != 0:
        return
    try:
        old = plistlib.loads(exported.stdout)
    except Exception:
        return
    new = prefs.Prefs()
    if new.path.exists():
        return                          # set up before; don't overwrite
    if "Enabled" in old:
        new["enabled"] = bool(old["Enabled"])
    if "Opacity" in old:
        new["opacity"] = float(old["Opacity"])
    if "ListFolded" in old:
        new["folded"] = bool(old["ListFolded"])
    anchor = old.get("WindowAnchor")
    if isinstance(anchor, list) and len(anchor) == 2:
        # AppKit counts up from the bottom of the main screen; Qt down from its top.
        try:
            from AppKit import NSScreen
            height = NSScreen.screens()[0].frame().size.height
            new["anchor"] = [float(anchor[0]), float(height - anchor[1])]
        except Exception:
            pass
    subprocess.run(["defaults", "delete", prefs.APP_ID], capture_output=True)
