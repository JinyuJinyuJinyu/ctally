"""The tests' own guard rails. A process a test starts, the app say, gets none of conftest's
patches, only its environment: still, everything it would read or write is the test's own."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT

SCRIPT = '''
import json, shutil, sys
sys.path[:0] = [sys.argv[1] + "/src"]
from ctally import fetch, prefs, sessions, snapshots, usage
print(json.dumps({
    "sign-in": str(fetch.CLAUDE_DIR),
    "registry, jobs and transcripts for snapshots": [str(p) for p in snapshots.Places()],
    "snapshots": str(snapshots.Snapshots().home),
    "hooks' notes": str(sessions.STATE_DIR),
    "status line": str(usage.STATUS_LINE),
    "~/.claude.json": str(usage.CLAUDE_JSON),
    "transcripts": str(sessions.PROJECTS_DIR),
    "running sessions": str(sessions.REGISTRY_DIR),
    "settings": str(prefs.config_dir()),
    "systemctl": shutil.which("systemctl"),
    "launchctl": shutil.which("launchctl"),
    "tmux server for snapshots": snapshots.Tmux().socket,
}))
'''


def test_a_process_a_test_starts_finds_only_the_tests_own_setup(own_usage_and_prefs, stand_ins):
    home = own_usage_and_prefs
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout)
    assert found.pop("tmux server for snapshots").startswith("ctally-pytest-none-")     # nothing runs there
    assert Path(found.pop("systemctl")).parent == Path(found.pop("launchctl")).parent == stand_ins
    for what, where in found.items():
        for path in where if isinstance(where, list) else [where]:
            assert Path(path).is_relative_to(home), f"{what}: {path}"
