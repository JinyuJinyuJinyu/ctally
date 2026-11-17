"""The app's switches for Exact usage and Snapshots, from the settings window, and the Restore
Sessions item. Qt runs in a separate process, as in the view tests; its tmux server, snapshots,
settings and Claude Code folder are this test's own (see conftest), and ccsnap's timer is left
alone."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent('''
    import os, sys, threading, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path[:0] = [sys.argv[1] + "/src"]
    from PySide6.QtGui import QAction
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from ctally import prefs, snapshots
    from ctally.gui import mac
    from ctally.gui.app import Controller
    mac.activate = lambda: None                     # the settings window, without taking focus on a Mac
    stopped = []
    snapshots.stop_ccsnap = lambda: stopped.append(True) or "ccsnap's systemd timer (ccsnap.timer)"

    def until(check, timeout=20):
        end = time.monotonic() + timeout
        while not check() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        return check()

    c = Controller(app, QLocalServer())

    # Exact usage: on from the start, and asking only while the limits show.
    assert c.exact_usage and c.show_usage and c.usage.fetcher is c.fetcher
    c.set_exact_usage(False)
    assert c.usage.fetcher is None and prefs.Prefs()["exact_usage"] is False
    c.set_exact_usage(True)
    c.set_show_usage(False)
    assert c.usage.fetcher is None
    c.set_show_usage(True)
    assert c.usage.fetcher is c.fetcher

    # The settings window has a switch for each, in step with the app.
    c.show_settings()
    window = c.settings
    assert window.exact_switch.isChecked() and not window.snapshots_switch.isChecked()
    window.exact_switch.click()
    assert not c.exact_usage and c.usage.fetcher is None
    c.set_exact_usage(True)                         # from elsewhere: the switch follows
    assert window.exact_switch.isChecked() and c.usage.fetcher is c.fetcher

    # Snapshots: off from the start. On takes one at once and then every couple of minutes,
    # and switches off ccsnap's timer.
    assert not c.keep_snapshots and not c.snapshot_timer.isActive()
    window.snapshots_switch.click()
    assert c.keep_snapshots and c.snapshot_timer.isActive() and prefs.Prefs()["snapshots"] is True
    assert until(lambda: stopped and c.keeper.store.paths()), (stopped, c.keeper.store.home)
    assert str(c.keeper.store.home) == os.environ["CCSNAP_HOME"]

    # Restore Sessions: hidden while everything in the snapshot is running...
    restore = QAction("Restore Sessions")
    c._offer_restore(restore)
    assert not restore.isVisible()
    # ...offered, saying when it was saved, once a session in it isn't...
    c.keeper.tmux("kill-session", "-t", "=work")
    c._offer_restore(restore)
    assert restore.isVisible() and restore.isEnabled() and restore.text().startswith("Restore Sessions ("), \\
        restore.text()
    # ...greyed while it's being brought back...
    gate = threading.Event()
    c.keeper.pool.submit(gate.wait)
    c.restore_sessions()
    c._offer_restore(restore)
    assert restore.isVisible() and not restore.isEnabled() and restore.text() == "Restoring Sessions…"
    gate.set()
    assert until(lambda: not c.keeper.restoring)
    # ...and gone again once it's back.
    assert "work" in c.keeper.tmux.sessions()
    c._offer_restore(restore)
    assert not restore.isVisible()

    window.snapshots_switch.click()
    assert not c.keep_snapshots and not c.snapshot_timer.isActive() and prefs.Prefs()["snapshots"] is False
    print("ok")
''')


def test_the_switches_for_exact_usage_and_snapshots(private_tmux, tmp_path):
    private_tmux.new_session("keep")
    private_tmux.new_session("work")
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CCSNAP_TMUX_SOCKET": private_tmux.name,
                               "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
