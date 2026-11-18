"""Codex setup preserves existing hooks, trusts nothing itself, and uninstalls only its own."""
import json
import shlex
import sys

import pytest

from ctally import cli, codex_hooks, install
from ctally.claude_hooks import SettingsError

COMMAND = shlex.join([sys.executable, "-m", "ctally.codex", "/tmp/a codex home"])


def test_merge_is_idempotent_and_removal_preserves_other_handlers():
    before = {"description": "my hooks", "hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": "echo keep"}]}]}, "other": {"keep": True}}
    added = codex_hooks.configure(before, True, COMMAND)
    assert codex_hooks.configure(added, True, COMMAND) == added
    assert codex_hooks.configure(added, False, COMMAND) == before
    mixed = {"hooks": {"Stop": [{"matcher": "abc", "hooks": [
        {"type": "command", "command": COMMAND}, {"type": "command", "command": "echo ctally"}]}]}}
    assert codex_hooks.configure(mixed, False, COMMAND) == {"hooks": {"Stop": [{"matcher": "abc", "hooks": [
        {"type": "command", "command": "echo ctally"}]}]}}


@pytest.mark.parametrize("text", ['{bad', '[]', '', '{"hooks": []}', '{"hooks":{"Stop":null}}',
                                   '{"hooks":{"Stop":[{"hooks":[null]}]}}'])
def test_malformed_settings_are_left_untouched(tmp_path, text):
    path = tmp_path / "hooks.json"
    path.write_text(text)
    with pytest.raises(SettingsError):
        codex_hooks.update(True, path)
    assert path.read_text() == text
    assert not list(tmp_path.glob("*.bak.*"))


def test_update_backup_symlink_and_uninstall(tmp_path):
    target = tmp_path / "dotfiles.json"
    before = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo original"}]}]}}
    target.write_text(json.dumps(before))
    path = tmp_path / "hooks.json"
    path.symlink_to(target)
    codex_hooks.update(True, path)
    assert path.is_symlink()
    added = json.loads(target.read_text())
    assert set(codex_hooks.EVENTS) <= set(added["hooks"])
    assert len(list(tmp_path.glob("hooks.json.bak.*"))) == 1
    codex_hooks.update(True, path)
    assert len(list(tmp_path.glob("hooks.json.bak.*"))) == 1
    codex_hooks.update(False, path)
    assert json.loads(target.read_text()) == before
    missing = tmp_path / "not-installed" / "hooks.json"
    assert codex_hooks.update(False, missing) == "" and not missing.exists()


def test_codex_setup_does_not_install_claude_hooks(monkeypatch, own_usage_and_prefs, capsys):
    monkeypatch.setattr(install, "command", lambda: "/somewhere/ctally")
    monkeypatch.setattr(install.prefs, "tell_running", lambda _: False)
    monkeypatch.setattr(install.claude_hooks, "install_script", lambda: pytest.fail("installed Claude hooks"))
    monkeypatch.setattr(install.claude_hooks, "update", lambda **_: pytest.fail("edited Claude settings"))
    assert cli.main(["setup", "--codex", "--no-launch", "--no-autostart"]) == 0
    assert (codex_hooks.home() / "hooks.json").exists()
    assert "/hooks" in capsys.readouterr().out
    assert not (codex_hooks.home() / "config.toml").exists()
