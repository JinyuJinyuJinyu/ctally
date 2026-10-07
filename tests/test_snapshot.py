"""The indicator draws every layout without a window server, through tools/snapshot.py (run as
a separate process, so Qt never starts inside the test run)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")


def test_every_layout_renders(tmp_path):
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "snapshot.py"), "all", str(tmp_path),
                           "--scale", "1", "--background", "#585858"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    from_header = {}
    for name in ("one", "three", "list", "folded"):
        data = (tmp_path / f"{name}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        from_header[name] = (width - 48, height - 48)            # less the margin around it
    assert from_header["one"] == (120, 120)
    assert from_header["three"] == (3 * 124, 140)
    assert 200 <= from_header["list"][0] <= 320
    assert from_header["folded"][1] == 2 * 8 + 26
    assert from_header["list"][1] > from_header["folded"][1]
