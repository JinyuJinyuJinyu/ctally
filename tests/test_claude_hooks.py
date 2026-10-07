"""Merging CTally's hooks into Claude Code's settings.json and taking them out again."""
from __future__ import annotations

import json
import os
import stat

import pytest

from ctally import claude_hooks
from ctally.claude_hooks import EVENTS, HOOK, SettingsError, configure, update


def ours(settings: dict) -> dict[str, list[str]]:
    """Our hook commands, by event."""
    found: dict[str, list[str]] = {}
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if "ctally" in hook.get("command", ""):
                    found.setdefault(event, []).append(hook["command"])
    return found


def test_install_adds_each_hook_once():
    settings = configure({"model": "opus"}, install=True)
    assert settings["model"] == "opus"
    assert ours(settings) == {event: [f"{HOOK} {state}"] for event, state in EVENTS.items()}


def test_install_twice_changes_nothing():
    once = configure({}, install=True)
    assert configure(once, install=True) == once


def test_uninstall_keeps_other_hooks_and_settings():
    mine = {"type": "command", "command": "my-notifier"}
    settings = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [mine, {"type": "command", "command": f"{HOOK} done"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit.sh"}]}],
        },
    }
    removed = configure(configure(settings, install=True), install=False)
    assert removed == {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [mine]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit.sh"}]}],
        },
    }


def test_uninstall_of_only_ours_drops_the_hooks_key():
    assert configure(configure({"a": 1}, install=True), install=False) == {"a": 1}


def test_claude_pet_hooks_are_replaced():
    old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/claude-pet.sh" done'}]}]}}
    settings = configure(old, install=True)
    commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert commands == [f"{HOOK} done"]


def test_configure_does_not_change_its_input():
    original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    snapshot = json.dumps(original)
    configure(original, install=True)
    assert json.dumps(original) == snapshot


def test_update_creates_a_missing_file(tmp_path):
    path = tmp_path / "sub" / "settings.json"
    message = update(install=True, path=path)
    assert "installed in" in message
    assert ours(json.loads(path.read_text())).keys() == EVENTS.keys()


def test_update_backs_up_first_and_is_then_a_no_op(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"model": "opus"}\n')
    message = update(install=True, path=path)
    backups = list(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == '{"model": "opus"}\n'
    assert "Backed up" in message
    assert "already installed" in update(install=True, path=path)
    assert len(list(tmp_path.glob("settings.json.bak.*"))) == 1


def test_backups_never_overwrite_each_other(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    copies = {claude_hooks.backup(path) for _ in range(3)}
    assert len(copies) == 3


def test_update_writes_through_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"model": "opus"}')
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    update(install=True, path=link)
    assert link.is_symlink()
    assert ours(json.loads(real.read_text())).keys() == EVENTS.keys()
    assert list(tmp_path.glob("settings.json.bak.*"))           # the backup sits by the link


def test_update_keeps_the_file_mode(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    path.chmod(0o600)
    update(install=True, path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"a": 1}')
    path.chmod(0)
    try:
        with pytest.raises(SettingsError, match="Can't read"):
            update(install=True, path=path)
    finally:
        path.chmod(0o600)
    assert path.read_text() == '{"a": 1}'
    assert not list(tmp_path.glob("settings.json.bak.*"))


@pytest.mark.parametrize("text, message", [("{not json", "not valid JSON"), ("[1, 2]", "JSON object")])
def test_bad_contents_are_left_alone(tmp_path, text, message):
    path = tmp_path / "settings.json"
    path.write_text(text)
    with pytest.raises(SettingsError, match=message):
        update(install=True, path=path)
    assert path.read_text() == text


def test_empty_file_counts_as_empty_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("  \n")
    update(install=True, path=path)
    assert ours(json.loads(path.read_text())).keys() == EVENTS.keys()


def test_install_script_writes_an_executable_copy(tmp_path, monkeypatch):
    script = tmp_path / "hooks" / "ctally.sh"
    monkeypatch.setattr(claude_hooks, "SCRIPT", script)
    claude_hooks.install_script()
    source = (claude_hooks.resources.files("ctally") / "hooks" / "ctally.sh").read_bytes()
    assert script.read_bytes() == source
    assert os.access(script, os.X_OK)
    assert not list(script.parent.glob(".*"))                  # no temporary file left


def test_remove_scripts_keeps_a_shared_folder(tmp_path, monkeypatch):
    folder = tmp_path / "hooks"
    folder.mkdir()
    monkeypatch.setattr(claude_hooks, "SCRIPT", folder / "ctally.sh")
    for name in ("ctally.sh", "claude-pet.sh", "someone-elses.sh"):
        (folder / name).write_text("#!/bin/sh\n")
    claude_hooks.remove_scripts()
    assert sorted(os.listdir(folder)) == ["someone-elses.sh"]
    (folder / "someone-elses.sh").unlink()
    claude_hooks.remove_scripts()
    assert not folder.exists()
