"""Adding CTally's block to the tmux config, and taking it out again.

tmux_conf runs plain `tmux`, which talks to the default server of $TMUX_TMPDIR. Every test
here points TMUX_TMPDIR at a private folder (and clears TMUX), so the real server is never
reached; the ones that want a server start their own there."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import TMUX

from ctally import tmux_conf
from ctally.tmux_conf import BEGIN, END

CTALLY = "/home/someone/.local/bin/ctally"
SPACED = "/opt/ctally bin/ctally"       # a space, to check the quoting


@pytest.fixture(autouse=True)
def isolated(tmp_path, short_tmp, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX_TMPDIR", str(short_tmp))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return tmp_path


@pytest.fixture
def server(short_tmp):
    """A tmux server on the private default socket: the one tmux_conf will find."""
    if not TMUX:
        pytest.skip("tmux is not installed")
    env = {**os.environ, "TMUX_TMPDIR": str(short_tmp)}
    env.pop("TMUX", None)

    def tmux(*args: str) -> str:
        return subprocess.run([TMUX, *args], capture_output=True, text=True, env=env, check=True).stdout

    tmux("-f", "/dev/null", "new-session", "-d", "-s", "t", sys.executable, "-c", "import time; time.sleep(600)")
    try:
        yield tmux
    finally:
        subprocess.run([TMUX, "kill-server"], capture_output=True, env=env)


def test_install_writes_the_block(isolated):
    tmux_conf.install(SPACED)
    text = (isolated / ".tmux.conf").read_text()
    assert text.startswith(BEGIN) and text.rstrip().endswith(END)
    assert f"bind-key J run-shell -b \"'{SPACED}' jump '#{{pane_id}}' '#{{client_name}}' '#{{socket_path}}'\"" in text
    assert "status --tmux" in text and SPACED in text


def test_install_is_idempotent_and_keeps_other_lines(isolated):
    conf = isolated / ".tmux.conf"
    conf.write_text("set -g mouse on\nbind r source-file ~/.tmux.conf\n")
    tmux_conf.install(CTALLY)
    once = conf.read_text()
    assert "already set up" in tmux_conf.install(CTALLY)
    assert conf.read_text() == once
    assert once.startswith("set -g mouse on\nbind r source-file ~/.tmux.conf\n")
    assert once.count(BEGIN) == 1
    assert len(list(isolated.glob(".tmux.conf.bak.*"))) == 1     # only the first change backs up


def test_install_replaces_an_older_block(isolated):
    conf = isolated / ".tmux.conf"
    conf.write_text(f"set -g mouse on\n{BEGIN}  (old)\nbind-key J run-shell 'old'\n{END}\nset -g base-index 1\n")
    tmux_conf.install(CTALLY)
    text = conf.read_text()
    assert "'old'" not in text and text.count(BEGIN) == 1
    assert "set -g mouse on" in text and "set -g base-index 1" in text


def test_xdg_config_only_when_there_is_no_tmux_conf(isolated):
    xdg = isolated / ".config" / "tmux" / "tmux.conf"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("set -g mouse on\n")
    assert tmux_conf.config() == xdg
    (isolated / ".tmux.conf").write_text("")
    assert tmux_conf.config() == isolated / ".tmux.conf"


def test_neither_config_exists_means_tmux_conf(isolated):
    assert tmux_conf.config() == isolated / ".tmux.conf"


def test_uninstall_removes_the_block_and_keeps_the_rest(isolated):
    conf = isolated / ".tmux.conf"
    conf.write_text("set -g mouse on\n")
    tmux_conf.install(CTALLY)
    tmux_conf.uninstall()
    assert conf.read_text() == "set -g mouse on\n"


def test_uninstall_deletes_a_file_that_only_held_the_block(isolated):
    tmux_conf.install(CTALLY)
    tmux_conf.uninstall()
    assert not (isolated / ".tmux.conf").exists()
    assert list(isolated.glob(".tmux.conf.bak.*"))              # backed up before


def test_uninstall_without_a_block_does_nothing(isolated):
    conf = isolated / ".tmux.conf"
    conf.write_text("set -g mouse on\n")
    assert tmux_conf.uninstall() == ""
    assert conf.read_text() == "set -g mouse on\n"


def test_without_block():
    text = f"a\n{BEGIN} x\nb\n{END}\nc"
    assert tmux_conf.without_block(text) == "a\nc"


def binding(tmux) -> list[str]:
    return [line for line in tmux("list-keys", "-T", "prefix").splitlines() if line.split()[3:4] == ["J"]]


def picked_up_once(server, ctally: str) -> None:
    tmux_conf.install(ctally)
    tmux_conf.install(ctally)
    tmux_conf._tmux("source-file", str(tmux_conf.config()))    # and a reload by hand
    assert any("ctally" in line and "jump" in line for line in binding(server))
    right = server("show", "-gv", "status-right")
    assert right.count("status --tmux") == 1, right
    assert server("show", "-gv", "status-right-length").strip() == "80"


def test_a_running_server_picks_it_up_once(server):
    picked_up_once(server, CTALLY)


def test_a_path_with_a_space_still_gets_the_status_segment(server):
    picked_up_once(server, SPACED)


def test_uninstall_undoes_it_in_the_running_server(server):
    before = server("show", "-gv", "status-right").rstrip("\n")
    tmux_conf.install(CTALLY)
    tmux_conf.uninstall()
    assert binding(server) == []
    assert server("show", "-gv", "status-right").rstrip("\n") == before


def test_uninstall_leaves_someone_elses_j_alone(server):
    server("bind-key", "J", "display-message", "mine")
    tmux_conf.uninstall()
    assert binding(server)
