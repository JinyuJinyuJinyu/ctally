"""Brings a session's terminal to the front.

Inside tmux, that means switching tmux to the session's pane, then raising the terminal
attached to that tmux session (or opening one if none is). Outside tmux, it means raising the
terminal the session runs in: on macOS the exact Terminal tab, or the app for anything else
(iTerm2, VS Code, the Claude desktop app); on Linux the terminal's window, where the desktop
allows it (X11, with xdotool installed; Wayland lets no app raise another's window).

All of this shells out, so it runs off the main thread, and it stays silent when anything
along the way is missing.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import threading

from . import system

TERMINAL = "com.apple.Terminal"

# Picks the Terminal tab whose tty matches, and puts its window in front.
RAISE_TAB = """
on run argv
  set target to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is target then
          set miniaturized of w to false
          set selected tab of w to t
          set index of w to 1
          activate
          return
        end if
      end repeat
    end repeat
  end tell
end run
"""


def focus(pid: int) -> None:
    if pid > 0:
        threading.Thread(target=_focus_now, args=(pid,), daemon=True).start()


def _focus_now(pid: int) -> None:
    target = system.tmux_pane(pid)
    if target:
        _focus_tmux(target)
    else:
        _raise(system.tty(pid), pid)


def _focus_tmux(target: system.TmuxPane) -> None:
    def tmux(*args: str) -> str | None:
        return system.run([target.binary, "-S", target.socket, *args])

    tmux("select-window", "-t", target.pane)
    tmux("select-pane", "-t", target.pane)
    session = (tmux("display-message", "-p", "-t", target.pane, "#{session_name}") or "").strip()
    if not session:
        return

    # The terminal attached to that tmux session that I used most recently.
    clients = []
    for line in (tmux("list-clients", "-t", "=" + session,
                      "-F", "#{client_activity} #{client_pid} #{client_tty}") or "").splitlines():
        fields = line.split(" ", 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            clients.append((int(fields[0]), int(fields[1]), fields[2]))
    if clients:
        _, client_pid, client_tty = max(clients)
        _raise(client_tty, client_pid)
        return

    # Nobody is looking at that session: open a terminal onto it.
    attach = shlex.join([target.binary, "-S", target.socket, "attach-session", "-t", "=" + session])
    if system.MAC:
        quoted = attach.replace("\\", "\\\\").replace('"', '\\"')
        system.run(["osascript", "-e", 'tell application "Terminal"', "-e", f'do script "{quoted}"',
                    "-e", "activate", "-e", "end tell"])
    elif shutil.which("x-terminal-emulator"):
        subprocess.Popen(["x-terminal-emulator", "-e", "sh", "-c", attach], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _raise(tty: str | None, pid: int) -> None:
    if system.MAC:
        _raise_mac(tty, pid)
    elif system.LINUX:
        _raise_x11(pid)


def _raise_mac(tty: str | None, pid: int) -> None:
    app = _host_app(pid)
    if app is None:
        return
    if tty and app.bundleIdentifier() == TERMINAL:
        system.run(["osascript", "-e", RAISE_TAB, tty])
        return
    # The way the Dock does it: since macOS 14 an app can't push itself past the active one,
    # and CTally is never active, so asking the app directly may be ignored.
    from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration
    url = app.bundleURL()
    if url is None:
        app.activateWithOptions_(0)
        return
    configuration = NSWorkspaceOpenConfiguration.configuration()
    configuration.setActivates_(True)
    NSWorkspace.sharedWorkspace().openApplicationAtURL_configuration_completionHandler_(url, configuration, None)


def _host_app(pid: int):
    """The nearest ancestor that is an ordinary app: Terminal, for a shell in one of its tabs."""
    from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication
    current = pid
    for _ in range(32):
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(current)
        if app is not None and app.activationPolicy() == NSApplicationActivationPolicyRegular:
            return app
        up = system.parent(current)
        if not up or up <= 1 or up == current:
            return None
        current = up
    return None


def _raise_x11(pid: int) -> None:
    """Activates the window of the nearest ancestor that has one: the terminal emulator."""
    if not shutil.which("xdotool"):
        return
    current = pid
    for _ in range(32):
        windows = (system.run(["xdotool", "search", "--pid", str(current)]) or "").split()
        if windows:
            system.run(["xdotool", "windowactivate", windows[-1]])
            return
        up = system.parent(current)
        if not up or up <= 1 or up == current:
            return
        current = up
