"""Where CTally keeps its few settings, and how a second `ctally` reaches the running one."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from . import system

APP_ID = "io.github.jinyujinyujinyu.ctally"


def config_dir() -> Path:
    if os.environ.get("CTALLY_CONFIG_DIR"):
        return Path(os.environ["CTALLY_CONFIG_DIR"])
    if system.MAC:
        return Path.home() / "Library" / "Application Support" / "CTally"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ctally"


def log_file() -> Path:
    if system.MAC:
        return Path.home() / "Library" / "Logs" / "ctally.log"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ctally" / "ctally.log"


class Prefs:
    """A small JSON file: on or off, opacity, folded, whether to show usage limits, and where
    the indicator sits (its bottom-right corner, in screen coordinates)."""

    DEFAULTS = {"enabled": True, "opacity": 0.8, "folded": False, "usage": True, "anchor": None}

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "prefs.json"
        try:
            stored = json.loads(self.path.read_text())
        except (OSError, ValueError):
            stored = {}
        self.values = {**Prefs.DEFAULTS, **(stored if isinstance(stored, dict) else {})}

    def __getitem__(self, key: str):
        return self.values[key]

    def __setitem__(self, key: str, value) -> None:
        if self.values.get(key) == value:
            return
        self.values[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(".prefs.json.new")
            temp.write_text(json.dumps(self.values, indent=1))
            os.replace(temp, self.path)
        except OSError:
            pass


def socket_path() -> str:
    """The running indicator listens here: "show" opens its settings, "quit" ends it."""
    if os.environ.get("CTALLY_CONFIG_DIR"):
        base = Path(os.environ["CTALLY_CONFIG_DIR"])
    elif system.MAC:
        base = Path.home() / "Library" / "Caches" / "CTally"
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime) if runtime else Path.home() / ".cache" / "ctally"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "ctally.sock")


def tell_running(message: str) -> bool:
    """Sends a word to the running indicator; False if none is running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(socket_path())
            s.sendall(message.encode() + b"\n")
            s.recv(16)                  # it answers once it has acted
        return True
    except OSError:
        return False
