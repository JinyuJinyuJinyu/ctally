"""What CTally needs to know about other processes, on macOS and Linux: whether they're
alive, their terminal, their parent, the environment they started with (which says where
in tmux they run), and a way to run a tool and read its answer."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from typing import NamedTuple

MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")


class TmuxPane(NamedTuple):
    binary: str     # the tmux to ask
    socket: str     # the server the process runs under
    pane: str       # its pane, "%12"


def is_alive(pid: int) -> bool:
    """A session whose process is gone can no longer be doing anything."""
    if pid <= 0:
        return True                 # no pid recorded, can't disprove it
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True                 # alive, just not ours to signal
    except OSError:
        return False
    return True


def run(args: list[str], timeout: float = 5) -> str | None:
    """A tool's output, or None when it can't run or fails."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def tty(pid: int) -> str | None:
    """The terminal a process runs in, "/dev/ttys003", if it has one."""
    name = (run(["ps", "-o", "tty=", "-p", str(pid)]) or "").strip()
    if not name or name.startswith("?"):
        return None
    return name if name.startswith("/dev/") else "/dev/" + name


def parent(pid: int) -> int | None:
    if LINUX:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                # pid (comm) state ppid ...; comm may hold spaces and parentheses.
                return int(f.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    answer = (run(["ps", "-o", "ppid=", "-p", str(pid)]) or "").strip()
    return int(answer) if answer.isdigit() else None


def environment(pid: int) -> dict[str, str] | None:
    """The environment a process started with. Only our own processes' can be read."""
    if LINUX:
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                data = f.read()
        except OSError:
            return None
        pairs = (item.decode(errors="replace").split("=", 1) for item in data.split(b"\0") if b"=" in item)
        return dict(pairs)
    if MAC:
        return _mac_environment(pid)
    return None


# macOS keeps a process's arguments and environment behind sysctl KERN_PROCARGS2: the
# argument count, the executable's path, then the arguments and the environment, each a
# NUL-terminated string. Apple's own binaries keep their environment hidden.
_CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49
_libc = None


def _sysctl(mib: list[int], size: int) -> bytes | None:
    global _libc
    if _libc is None:
        # Its fixed path: looking it up by name walks the disk and takes a good fraction of a second.
        _libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    names = (ctypes.c_int * len(mib))(*mib)
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if _libc.sysctl(names, len(mib), buffer, ctypes.byref(length), None, 0) != 0:
        return None
    return buffer.raw[:length.value]


def _mac_environment(pid: int) -> dict[str, str] | None:
    argmax = _sysctl([_CTL_KERN, _KERN_ARGMAX], 4)
    if not argmax:
        return None
    raw = _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], int.from_bytes(argmax, sys.byteorder))
    if not raw or len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    strings = raw[4:].split(b"\0")
    # The executable's path, the padding after it, then the arguments.
    index = 1
    while index < len(strings) and strings[index] == b"":
        index += 1
    index += argc
    environment = {}
    for item in strings[index:]:
        if not item:
            break
        if b"=" in item:
            key, value = item.decode(errors="replace").split("=", 1)
            environment[key] = value
    return environment


_tmux: str | None = None


def tmux_binary() -> str | None:
    """tmux, wherever it was installed. Started at login, CTally has only a bare PATH. Once
    found it's remembered: searching a long PATH is slow, and tmux doesn't move."""
    global _tmux
    if _tmux is None:
        _tmux = shutil.which("tmux") or next(
            (candidate for candidate in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux",
                                         "/opt/local/bin/tmux", "/home/linuxbrew/.linuxbrew/bin/tmux")
             if os.access(candidate, os.X_OK)), None)
    return _tmux


def tmux_pane(pid: int) -> TmuxPane | None:
    """Where a process sits in tmux. tmux leaves TMUX ("socket,server pid,index") and
    TMUX_PANE in the environment of everything started inside it."""
    env = environment(pid) or {}
    server, pane = env.get("TMUX", ""), env.get("TMUX_PANE", "")
    socket = server.split(",", 1)[0]
    binary = tmux_binary()
    if not socket or not pane or not binary:
        return None
    return TmuxPane(binary, socket, pane)


def tmux_location(target: TmuxPane) -> str | None:
    """"report:0.2": the pane's session, window and index, the way tmux shows them."""
    answer = run([target.binary, "-S", target.socket, "display-message", "-p", "-t", target.pane,
                  "#{session_name}:#{window_index}.#{pane_index}"])
    location = (answer or "").strip()
    return location or None
