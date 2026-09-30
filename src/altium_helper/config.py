"""Where altium-helper keeps its files, and its settings and credentials.

Board data and installed tools go in the data directory; settings and credentials in the
config directory. Both are outside the repository, per platform convention, and can be
moved with the ``ALTIUM_HELPER_DATA`` and ``ALTIUM_HELPER_CONFIG`` environment variables.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP = "altium-helper"


def data_dir() -> Path:
    if override := os.environ.get("ALTIUM_HELPER_DATA"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP


def config_dir() -> Path:
    if override := os.environ.get("ALTIUM_HELPER_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP


def designs_dir() -> Path:
    """Where board copies live; ``ALTIUM_HELPER_DESIGNS`` moves them (e.g. to another disk)."""
    if override := os.environ.get("ALTIUM_HELPER_DESIGNS"):
        return Path(override)
    return data_dir() / "designs"


@dataclass
class BoardConfig:
    """A board added by hand, for when no API lists it (for example a public repository)."""

    git_url: str
    # Path of the .PrjPcb inside the repository; found automatically if empty.
    project_file: str = ""
    name: str = ""


@dataclass
class Settings:
    """Everything in ``config.json``. Secrets live in ``credentials.json`` instead."""

    # Which GraphQL API lists boards: "nexar" or "altium365".
    api: str = "nexar"
    # The Altium 365 workspace, e.g. https://penn-electric-racing.365.altium.com
    workspace_url: str = ""
    # How git signs in: "none", "password" (route B) or "token" (routes A and C).
    git_auth: str = "none"
    # Boards that must never be fetched, e.g. ones under sponsor NDA.
    exclude: list[str] = field(default_factory=list)
    boards: dict[str, BoardConfig] = field(default_factory=dict)
    # How old a local copy may be, in minutes, before a tool fetches again.
    sync_interval_minutes: int = 5

    @classmethod
    def load(cls) -> Settings:
        path = config_dir() / "config.json"
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        boards = {
            key: BoardConfig(**value) for key, value in raw.pop("boards", {}).items()
        }
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(boards=boards, **known)

    def save(self) -> None:
        path = config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


def load_credentials() -> dict:
    path = config_dir() / "credentials.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_credentials(credentials: dict) -> None:
    """Write credentials readable only by the current user."""
    path = config_dir() / "credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
    # On Windows the per-user profile folder is already private.
    if sys.platform != "win32":
        tmp.chmod(0o600)
    tmp.replace(path)
