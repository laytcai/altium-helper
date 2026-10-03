"""Running git for board repositories, read-only.

Credentials never go on the command line or into a config file. They're passed as a
URL-scoped ``http.<url>.extraHeader`` through git's ``GIT_CONFIG_*`` environment
variables, the "Authorization header" method in Altium's Project Git Access docs.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PUSH_DISABLED = "DISABLED-altium-helper-is-read-only"

# Give up on a connection that has sent nothing for 10 minutes, so a stalled fetch can't
# hold a board forever. Altium's server sends nothing while it builds a download: up to
# a minute is normal, and about 5 minutes has been seen.
NETWORK_SETTINGS = {"http.lowSpeedLimit": "1", "http.lowSpeedTime": "600"}
# No background upkeep: since git 2.29, fetch and merge start it, detached, and newer
# gits repack and rewrite .git/shallow there while our next command runs, failing it
# ("shallow file has changed since we read it"). Boards repack in the foreground
# instead (Board._tidy).
SETTINGS = {"maintenance.auto": "false", "gc.auto": "0"}

PRE_PUSH_HOOK = """#!/bin/sh
echo "altium-helper: this is a read-only copy of an Altium 365 project; pushing is disabled." >&2
exit 1
"""


class GitError(RuntimeError):
    """A git command failed."""


@dataclass(frozen=True)
class Credentials:
    """HTTP Basic credentials. For an Altium 365 token the username is ignored."""

    username: str
    password: str

    def header(self) -> str:
        pair = f"{self.username}:{self.password}".encode("utf-8")
        return "Authorization: Basic " + base64.b64encode(pair).decode("ascii")


def _environment(url: str | None, credentials: Credentials | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never wait for a password prompt
    env["GCM_INTERACTIVE"] = "Never"  # nor for Git Credential Manager's window
    env.setdefault("LC_ALL", "C")  # messages we can match on
    settings = {**SETTINGS, **(NETWORK_SETTINGS if url else {})}
    if credentials and url:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https"):
            scope = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}/"
            settings[f"http.{scope}.extraHeader"] = credentials.header()
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for key, value in settings.items():
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    if settings:
        env["GIT_CONFIG_COUNT"] = str(count)
    return env


def run(
    args: list[str],
    cwd: Path | None = None,
    *,
    url: str | None = None,
    credentials: Credentials | None = None,
    keep_fds: tuple[int, ...] = (),
) -> str:
    """Run ``git`` and return its standard output. Raises GitError on failure.

    ``keep_fds`` stay open in git (POSIX only): a held lock passed this way stays held
    until git exits, even if this process dies first.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # never the MCP server's stdin, which is its JSON-RPC pipe
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_environment(url, credentials),
            pass_fds=keep_fds if sys.platform != "win32" else (),
        )
    except FileNotFoundError as e:
        raise GitError("git isn't installed or isn't on PATH") from e
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "Authentication failed" in message or "could not read Username" in message:
            message += (
                "\nGit couldn't sign in to this repository. Run: altium-helper login"
            )
        raise GitError(f"git {' '.join(args[:2])} failed: {message}")
    return result.stdout


def make_read_only(repo: Path) -> None:
    """Point pushes at a URL that can't work, and add a pre-push hook that always fails."""
    run(["remote", "set-url", "--push", "origin", PUSH_DISABLED], repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    if sys.platform != "win32":
        hook.chmod(0o755)
    # A global core.hooksPath would bypass .git/hooks, so pin it for this repository.
    run(["config", "core.hooksPath", str(hooks)], repo)
