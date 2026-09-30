import sys
from pathlib import Path

import pytest

from altium_helper import setup_cmd


def _fake_extension(home: Path, folder: str, version: str) -> Path:
    name = "claude.exe" if sys.platform == "win32" else "claude"
    binary = (
        home
        / folder
        / "extensions"
        / f"anthropic.claude-code-{version}-linux-x64"
        / "resources"
        / "native-binary"
        / name
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    return binary


def test_finds_the_newest_claude_bundled_with_an_editor(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _fake_extension(tmp_path, ".vscode-server", "2.1.9")
    newest = _fake_extension(tmp_path, ".vscode-oss", "2.1.285")
    assert setup_cmd.find_claude() == newest


def test_no_claude_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert setup_cmd.find_claude() is None


def test_skill_goes_into_the_claude_config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    skill = setup_cmd.install_skill()
    assert skill == tmp_path / "skills" / "altium-boards" / "SKILL.md"
    assert skill.read_text(encoding="utf-8").startswith("---\nname: altium-boards\n")


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a shell script")
def test_registers_both_servers_at_user_scope(tmp_path):
    log = tmp_path / "calls.txt"
    fake = tmp_path / "claude"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    setup_cmd.register_claude(fake, ["/opt/altium-helper"])
    assert log.read_text().splitlines() == [
        "mcp remove --scope user altium-helper",
        "mcp add --scope user altium-helper -- /opt/altium-helper mcp",
        "mcp remove --scope user universal-netlist",
        "mcp add --scope user universal-netlist -- /opt/altium-helper serve-netlist",
    ]
