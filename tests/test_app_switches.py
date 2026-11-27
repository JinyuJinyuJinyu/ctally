"""The app's switches for Exact usage and Snapshots, from the settings window, how often
snapshots are taken and how long they're kept, and the Restore Sessions item. Qt runs in a separate process, as in the view tests; its tmux server, snapshots,
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

    # Snapshots: off from the start. On takes one at once and then every half hour,
    # and switches off ccsnap's timer.
    assert not c.keep_snapshots and not c.snapshot_timer.isActive()
    window.snapshots_switch.click()
    assert c.keep_snapshots and c.snapshot_timer.isActive() and prefs.Prefs()["snapshots"] is True
    assert until(lambda: stopped and c.keeper.store.paths()), (stopped, c.keeper.store.home)
    assert str(c.keeper.store.home) == os.environ["CCSNAP_HOME"]

    # How often, and for how long: every half hour and for 3 days to begin with, as set from
    # the window, at once. Half an hour to eight hours, shown in hours and minutes.
    assert c.snapshot_timer.interval() == 30 * 60_000 and c.keeper.store.keep_for == 3 * 86400
    assert window.every_box.value() == 30 and window.keep_box.value() == 3
    assert (window.every_box.minimum(), window.every_box.maximum()) == (30, 480)
    assert window.every_box.text() == "30 min" and window.keep_box.suffix() == " days"
    window.every_box.setValue(90)
    assert c.snapshot_timer.interval() == 90 * 60_000 and c.snapshot_timer.isActive()
    assert prefs.Prefs()["snapshot_minutes"] == 90 and window.every_box.text() == "1 h 30 min"
    window.every_box.stepUp()                       # by the half hour
    assert prefs.Prefs()["snapshot_minutes"] == 120 and window.every_box.text() == "2 h"
    # Typed any way that reads as a time; a bare number up to 8 is hours, and too little or
    # too much is the nearest allowed. What doesn't read as one leaves it as it was.
    for typed, minutes in (("45", 45), ("3", 180), ("1.5 h", 90), ("2 h 15 min", 135), ("240 min", 240),
                           ("10 min", 30), ("12 h", 480), ("soon", 480)):
        window.every_box.lineEdit().setText(typed)
        window.every_box.interpretText()
        assert window.every_box.value() == minutes and prefs.Prefs()["snapshot_minutes"] == minutes, typed
    assert window.every_box.text() == "8 h" and window.every_box.sizeHint().width() <= window.every_box.width()
    window.keep_box.setValue(1)
    assert c.keeper.store.keep_for == 86400 and prefs.Prefs()["snapshot_days"] == 1
    assert window.keep_box.suffix() == " day"
    # From elsewhere, and too much or too little: the nearest allowed, and the window follows.
    c.set_snapshot_minutes(2)
    assert c.snapshot_timer.interval() == 30 * 60_000 and window.every_box.value() == 30
    c.set_snapshot_minutes(5000)
    c.set_snapshot_days(0)
    assert c.snapshot_timer.interval() == 480 * 60_000 and window.every_box.value() == 480
    assert c.keeper.store.keep_for == 86400 and window.keep_box.value() == 1
    c.set_snapshot_days(14)
    assert window.keep_box.value() == 14 and window.keep_box.suffix() == " days"

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
    assert c.snapshot_timer.interval() == 480 * 60_000          # kept for next time

    # A number typed counts once it's done with, or as the window closes, and is still there
    # when something else is switched meanwhile (from the menus, say).
    window.every_box.lineEdit().setText("2 h")
    window.keep_box.lineEdit().setText("7 days")
    assert prefs.Prefs()["snapshot_minutes"] == 480 and prefs.Prefs()["snapshot_days"] == 14
    c.refresh_controls()
    assert window.every_box.text() == "2 h" and window.keep_box.text() == "7 days"
    # Close Window puts the window away, and CTally runs on: Quit CTally is what stops it.
    assert (window.close_button.text(), window.quit_button.text()) == ("Close Window", "Quit CTally")
    quits = []
    window.quit_requested.connect(lambda: quits.append(True))
    assert window.isVisible()
    window.close_button.click()
    assert not window.isVisible() and quits == []
    assert prefs.Prefs()["snapshot_minutes"] == 120 and c.snapshot_timer.interval() == 120 * 60_000
    assert prefs.Prefs()["snapshot_days"] == 7 and c.keeper.store.keep_for == 7 * 86400

    # Started again, it's as it was set.
    app.processEvents()
    again = Controller(app, QLocalServer())
    assert again.snapshot_timer.interval() == 120 * 60_000 and again.keeper.store.keep_for == 7 * 86400

    # Quit CTally asks for just that, and says how to start it again: by its place among the
    # applications on Linux, once `ctally setup` has put it there, with the icon drawn for it.
    from PySide6.QtGui import QImage
    from ctally import install, system
    from ctally.gui.app import _draw_launcher_icon
    from ctally.gui.settings import SettingsWindow
    lone, asked = SettingsWindow(), []
    lone.quit_requested.connect(lambda: asked.append(True))
    lone.show()
    lone.close_button.click()
    assert asked == [] and not lone.isVisible()
    lone.quit_button.click()
    assert asked == [True]
    assert "ctally run" in lone.quit_hint.text() and "applications" not in lone.quit_hint.text()
    _draw_launcher_icon()
    assert not install.LAUNCHER.exists() and not install.ICON.exists()      # nothing asked for it yet
    install.install_launcher("/somewhere/bin/ctally")
    entry = install.LAUNCHER.read_text().splitlines()
    assert entry[0] == "[Desktop Entry]" and 'Exec="/somewhere/bin/ctally" run' in entry
    assert f"Icon={install.ICON}" in entry and "Name=CTally" in entry and "StartupWMClass=CTally" in entry
    _draw_launcher_icon()
    assert QImage(str(install.ICON)).width() == 256
    after = SettingsWindow()
    assert ("applications" in after.quit_hint.text()) == (not system.MAC)

    # macOS: the search box takes the keyboard as a panel, CTally staying behind; only if macOS
    # doesn't give it the keyboard (whatever Qt says) does CTally come forward, going back after.
    from ctally import system
    from ctally.gui.view import SearchBox
    calls, was = [], system.MAC
    mac.activate, mac.hide_app = lambda: calls.append("activate"), lambda: calls.append("hide")
    system.MAC = True
    for given in (True, False):
        calls.clear()
        mac.has_keyboard = lambda widget, given=given: given
        box = SearchBox(c.view.fonts.search)
        box.show()
        c._search_started(box)
        until(lambda: calls, timeout=0.6)
        c._search_ended()
        box.hide()
        assert calls == ([] if given else ["activate", "hide"]), (given, calls)
    system.MAC = was
    print("ok")
''')


def test_the_switches_for_exact_usage_and_snapshots(private_tmux, tmp_path):
    private_tmux.new_session("keep")
    private_tmux.new_session("work")
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "CCSNAP_TMUX_SOCKET": private_tmux.name,
                               "CTALLY_STATE_DIR": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr
