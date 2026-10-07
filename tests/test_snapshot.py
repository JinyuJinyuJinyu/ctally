"""The indicator draws every layout without a window server, through tools/snapshot.py (run as
a separate process, so Qt never starts inside the test run)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")


def sizes(folder, *flags) -> dict[str, tuple[int, int]]:
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "snapshot.py"), "all", str(folder),
                           "--scale", "1", "--background", "#585858", *flags],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    found = {}
    for name in ("one", "three", "list", "folded"):
        data = (folder / f"{name}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        found[name] = (width - 48, height - 48)            # less the margin around it
    return found


def test_every_layout_renders(tmp_path):
    plain = sizes(tmp_path / "plain", "--no-usage")
    assert plain["one"] == (120, 120)
    assert plain["three"] == (3 * 124, 140)
    assert 200 <= plain["list"][0] <= 320
    assert plain["folded"][1] == 2 * 8 + 26
    assert plain["list"][1] > plain["folded"][1]

    # With usage limits: a card under the badges, a line per limit in the list, two small
    # meters in the folded bar.
    usage = sizes(tmp_path / "usage")
    assert usage["one"] == (120, 120 + 30 + 8)
    assert usage["three"] == (3 * 124, 140 + 30 + 8)
    assert usage["list"][1] == plain["list"][1] + 3 * 18 + 6
    assert 200 <= usage["list"][0] <= 320
    assert usage["folded"][1] == plain["folded"][1]
    assert usage["folded"][0] > plain["folded"][0] + 80
