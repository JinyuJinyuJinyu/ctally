"""Shared helpers. Nothing here touches the real ~/.claude, tmux config or tmux server: homes
are temporary, and tmux runs as private servers that are killed afterwards."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "ctally" / "hooks" / "ctally.sh"
DATA = Path(__file__).resolve().parent / "data"
TMUX = shutil.which("tmux")


def sleeper(env: dict | None = None) -> subprocess.Popen:
    """A process that just waits. Python rather than /bin/sleep: macOS hides the environment
    of Apple's own binaries, and some tests read it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dead_pid() -> int:
    """The pid of a process that has come and gone."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def wait_for(check, timeout: float = 5.0, every: float = 0.05):
    """Polls until check() gives something truthy, and returns it (or the last falsy try)."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(every)


class Hook:
    """Runs the hook script the way Claude Code does: a state word, JSON on stdin."""

    def __init__(self, home: Path, pid: int):
        self.home = home
        self.pid = pid
        self.dir = home / ".claude" / "ctally.d"

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_SESSION_ID", "TMUX", "TMUX_PANE", "CLAUDE_PROJECT_DIR")}
        env.update(HOME=str(self.home), CLAUDE_PID=str(self.pid), CLAUDE_PROJECT_DIR="/work/my project")
        env.update(extra)
        return env

    def __call__(self, state: str, payload, **env) -> subprocess.CompletedProcess:
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        return subprocess.run(["sh", str(HOOK), state], input=text + "\n", text=True,
                              capture_output=True, env=self.env(**env), cwd=self.home, timeout=10)

    def state(self, sid: str) -> str | None:
        try:
            return (self.dir / sid).read_text().split()[0]
        except (OSError, IndexError):
            return None

    def agents(self, sid: str) -> list[str] | None:
        """Everything in the session's agents folder, dot files included; None if no folder."""
        folder = self.dir / ".agents" / sid
        return sorted(os.listdir(folder)) if folder.is_dir() else None


def _real_files() -> dict:
    home = Path.home()
    watched = [home / ".claude" / "settings.json", home / ".tmux.conf", home / ".config" / "tmux" / "tmux.conf"]
    hooks = home / ".claude" / "hooks"
    if hooks.is_dir():
        watched += sorted(hooks.iterdir())
    found = {}
    for path in watched:
        try:
            info = path.stat()
            found[str(path)] = (info.st_mtime_ns, info.st_size)
        except OSError:
            pass
    return found


@pytest.fixture(scope="session", autouse=True)
def real_setup_untouched():
    """A test that writes to the real Claude Code or tmux setup fails the run, loudly."""
    before = _real_files()
    yield
    after = _real_files()
    changed = sorted(set(before) ^ set(after) | {p for p in before.keys() & after.keys() if before[p] != after[p]})
    assert not changed, f"the tests changed real files: {changed}"


@pytest.fixture(autouse=True)
def own_usage_and_prefs(tmp_path_factory, monkeypatch):
    """Usage limits and preferences come from each test's own files, never the real
    ~/.claude.json, status line or CTally settings."""
    from ctally import usage
    home = tmp_path_factory.mktemp("usage")
    monkeypatch.setattr(usage, "CLAUDE_JSON", home / ".claude.json")
    monkeypatch.setattr(usage, "STATUS_LINE", home / ".statusline")
    monkeypatch.setenv("CTALLY_CLAUDE_JSON", str(home / ".claude.json"))
    monkeypatch.setenv("CTALLY_CONFIG_DIR", str(home / "config"))
    return home


@pytest.fixture
def hook(tmp_path):
    return Hook(tmp_path, os.getpid())


class PrivateTmux:
    """A tmux server of our own, on its own socket, gone after the test."""

    def __init__(self):
        self.name = "ctally-pytest-" + uuid.uuid4().hex[:8]
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.processes: list[subprocess.Popen] = []

    def __call__(self, *args: str, check: bool = True) -> str:
        done = subprocess.run([TMUX, "-L", self.name, *args], capture_output=True, text=True,
                              env=self.env, stdin=subprocess.DEVNULL, timeout=10)
        if check and done.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout.strip()

    def sleeper_command(self) -> list[str]:
        # Several arguments: tmux runs them directly, not through sh, so the pane's process is
        # the python itself and its environment holds TMUX and TMUX_PANE.
        return [sys.executable, "-c", "import time; time.sleep(600)"]

    def new_session(self, name: str) -> str:
        self("-f", "/dev/null", "new-session", "-d", "-s", name, "-x", "120", "-y", "40", *self.sleeper_command())
        return f"{name}:0.0"

    def split(self, target: str) -> None:
        self("split-window", "-t", target, *self.sleeper_command())

    def pane(self, target: str, what: str) -> str:
        return self("display-message", "-p", "-t", target, what)

    def pid_in(self, target: str) -> int:
        """The pane's process, once it has become the python (not the forked tmux child)."""
        pid = int(self.pane(target, "#{pane_pid}"))
        from ctally import system
        wait_for(lambda: (system.environment(pid) or {}).get("TMUX_PANE"), timeout=5)
        return pid

    @property
    def socket(self) -> str:
        return self("display-message", "-p", "#{socket_path}")

    def kill(self) -> None:
        for process in self.processes:
            process.kill()
        socket = self("display-message", "-p", "#{socket_path}", check=False)
        subprocess.run([TMUX, "-L", self.name, "kill-server"], capture_output=True, env=self.env)
        if socket and Path(socket).name == self.name:
            Path(socket).unlink(missing_ok=True)        # tmux leaves its socket file behind


@pytest.fixture
def private_tmux():
    if not TMUX:
        pytest.skip("tmux is not installed")
    server = PrivateTmux()
    try:
        yield server
    finally:
        server.kill()


@pytest.fixture
def short_tmp():
    """A short temporary directory: tmux sockets must fit in about 104 characters."""
    path = Path(tempfile.mkdtemp(prefix="ctx", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
