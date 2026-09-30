"""``altium-helper setup``: install universal-netlist, and register both MCP servers and the
skill with Claude Code (and Codex, if it's installed)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

# MCP server name -> arguments to the altium-helper command that starts it.
SERVERS = {"altium-helper": ["mcp"], "universal-netlist": ["serve-netlist"]}
EDITOR_FOLDERS = (
    ".vscode",
    ".vscode-server",
    ".vscode-insiders",
    ".vscode-oss",
    ".cursor",
    ".cursor-server",
    ".windsurf",
)


class SetupError(RuntimeError):
    """Registering with an MCP client failed."""


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _version(path: Path) -> tuple[int, ...]:
    match = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(path))
    return tuple(int(part) for part in match.groups()) if match else (0,)


def find_claude() -> Path | None:
    """The Claude Code CLI: on PATH, or the copy bundled with an editor extension."""
    if found := shutil.which("claude"):
        return Path(found)
    executable = "claude.exe" if os.name == "nt" else "claude"
    home = Path.home()
    candidates = [
        path
        for folder in EDITOR_FOLDERS
        for path in (home / folder / "extensions").glob(
            f"anthropic.claude-code-*/resources/native-binary/{executable}"
        )
    ]
    candidates += [
        p
        for p in (
            home / ".local" / "bin" / executable,
            home / ".claude" / "local" / executable,
        )
        if p.exists()
    ]
    return max(candidates, key=_version) if candidates else None


def launcher() -> list[str]:
    """The command that starts altium-helper, stable enough to store in an MCP config."""
    if found := shutil.which("altium-helper"):
        return [str(Path(found).absolute())]
    return [sys.executable, "-m", "altium_helper"]


def _register(
    client: list[str], name: str, command: list[str], scope: list[str]
) -> None:
    subprocess.run([*client, "mcp", "remove", *scope, name], capture_output=True)
    result = subprocess.run(
        [*client, "mcp", "add", *scope, name, "--", *command],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise SetupError(
            f"Couldn't register {name} with {Path(client[0]).name}: {message}"
        )


def register_claude(claude: Path, command: list[str]) -> None:
    for name, args in SERVERS.items():
        _register([str(claude)], name, [*command, *args], ["--scope", "user"])


def register_codex(codex: str, command: list[str]) -> None:
    for name, args in SERVERS.items():
        _register([codex], name, [*command, *args], [])


def install_skill() -> Path:
    target = claude_config_dir() / "skills" / "altium-boards" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        resources.files("altium_helper").joinpath("data/skill/SKILL.md").read_bytes()
    )
    return target


def manual_instructions(command: list[str]) -> str:
    lines = ["Register the MCP servers with your client by hand, for example:"]
    for name, args in SERVERS.items():
        lines.append(
            f"  claude mcp add --scope user {name} -- {' '.join([*command, *args])}"
        )
    return "\n".join(lines)
