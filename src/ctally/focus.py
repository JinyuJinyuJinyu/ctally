"""Brings a session's terminal to the front.

Inside tmux, that means switching tmux to the session's pane, then raising the terminal
attached to that tmux session (or opening one if none is). Outside tmux, it means raising the
terminal the session runs in: on macOS the exact Terminal tab, or the app for anything else
(iTerm2, VS Code, the Claude desktop app); on Linux the exact GNOME Terminal tab, or the
window of any other terminal where the desktop allows it (X11, with xdotool installed;
Wayland lets no app raise another's window).

Shared Codex tasks open by session ID through `codex resume`, even without a known terminal.
All of this shells out, so it runs off the main thread. Codex launch errors are reported
through the caller's callback; raising an existing terminal remains best-effort.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

from . import system
from .codex_hooks import home as codex_home

TERMINAL = "com.apple.Terminal"
_opening: set[str] = set()
_opened_at: dict[str, float] = {}
_open_lock = threading.Lock()

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


def focus(pid: int, when: int = 0) -> None:
    """`when` is the click that asked, by the window system's clock: on X11 a window is let in
    front of the one I'm using only for something I did after I last used that."""
    if pid > 0:
        threading.Thread(target=_focus_now, args=(pid, when), daemon=True).start()


def open_session(session, when: int = 0, failed=None) -> None:
    """Open the selected task, coalescing double-clicks while its terminal starts."""
    with _open_lock:
        if session.id in _opening or time.monotonic() - _opened_at.get(session.id, -10) < 2:
            return
        _opening.add(session.id)

    def go():
        try:
            _open_session_now(session, when)
        except (OSError, ValueError) as error:
            if failed:
                failed(str(error))
        else:
            with _open_lock:
                _opened_at[session.id] = time.monotonic()
        finally:
            with _open_lock:
                _opening.discard(session.id)

    threading.Thread(target=go, daemon=True).start()


def _open_session_now(session, when: int) -> None:
    if session.source != "codex":
        if session.pid > 0:
            _focus_now(session.pid, when)
        return
    # A private server belongs to this terminal. A shared daemon can have many
    # tasks in one pane: raising that pane alone would show whichever task was last selected.
    current = session.owner_pid
    for _ in range(32):
        if session.pid > 0 and current == session.pid:
            _focus_now(session.pid, when)
            return
        current = system.parent(current) if current > 1 else None
        if not current:
            break
    sid = session.id.removeprefix("codex:")
    try:
        UUID(sid)
    except ValueError:
        raise ValueError("This Codex task has no valid session ID.") from None
    binary = shutil.which("codex") or next(
        (str(p) for p in (Path.home() / ".npm-global/bin/codex", Path.home() / ".local/bin/codex",
                         Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"))
         if p.is_file() and os.access(p, os.X_OK)), None)
    if not binary:
        raise OSError("Codex CLI was not found. Install it or add codex to CTally's PATH, then try again.")
    # Explicit ID, no prompt: this attaches a UI to the existing task without starting a turn.
    command = shlex.join(["env", f"CODEX_HOME={codex_home()}", binary, "resume", "--", sid])
    command = "exec " + command
    if session.path and Path(session.path).is_dir():
        command = f"cd {shlex.quote(session.path)} && " + command
    if system.MAC:
        script = ('on run argv\n tell application "Terminal"\n'
                  ' do script (item 1 of argv)\n activate\n end tell\nend run')
        if system.run(["osascript", "-e", script, command]) is None:
            raise OSError("Could not open Terminal. Allow CTally to control Terminal in System Settings → Privacy & Security → Automation.")
    elif system.LINUX and (terminal := shutil.which("x-terminal-emulator")):
        subprocess.Popen([terminal, "-e", "sh", "-c", command], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise OSError("No supported terminal was found. Install an x-terminal-emulator to open Codex tasks.")


def _focus_now(pid: int, when: int) -> None:
    target = system.tmux_pane(pid)
    if target:
        _focus_tmux(target, when)
    else:
        _raise(system.tty(pid), pid, when)


def _focus_tmux(target: system.TmuxPane, when: int) -> None:
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
        _raise(client_tty, client_pid, when)
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


def _raise(tty: str | None, pid: int, when: int) -> None:
    if system.MAC:
        _raise_mac(tty, pid)
    elif system.LINUX:
        _raise_linux(pid, when)


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


def _raise_linux(pid: int, when: int) -> None:
    """Activates the window of the nearest ancestor that has one: the terminal emulator. GNOME
    Terminal keeps all its windows in one process, so any of them could be the one; it is
    asked for the session's own tab instead."""
    xdotool = shutil.which("xdotool")
    current = pid
    for _ in range(32):
        up = system.parent(current)
        if up and system.program(up) == "gnome-terminal-server" and _show_gnome_terminal_tab(current, when):
            return
        if xdotool:
            windows = (system.run([xdotool, "search", "--pid", str(current)]) or "").split()
            if windows:
                system.run([xdotool, "windowactivate", windows[-1]])
                return
        if not up or up <= 1 or up == current:
            return
        current = up


def _show_gnome_terminal_tab(shell: int, when: int) -> bool:
    """GNOME Terminal tells what it starts in each tab, the shell, which tab that is. Its search
    provider, which the Activities overview uses to find a terminal, switches to a tab by that
    id and brings its window forward, X11 or Wayland, with no xdotool. Only for the time of a
    click, though: without one GNOME takes it for a window stealing focus, and leaves it."""
    env = system.environment(shell) or {}
    path = env.get("GNOME_TERMINAL_SCREEN", "")
    tab = path.rsplit("/", 1)[-1].replace("_", "-")     # its object path, then the id the provider knows
    if not path.startswith("/org/gnome/Terminal/screen/") or not re.fullmatch(r"[0-9a-f-]+", tab):
        return False
    service = env.get("GNOME_TERMINAL_SERVICE") or "org.gnome.Terminal"
    return system.run(["gdbus", "call", "--session", "--dest", service,
                       "--object-path", "/org/gnome/Terminal/SearchProvider",
                       "--method", "org.gnome.Shell.SearchProvider2.ActivateResult",
                       f"'{tab}'", "@as []", str(when)]) is not None
