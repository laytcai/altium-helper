"""Install and run the pinned universal-netlist.

universal-netlist is a Node.js program. We run it with the Node.js that ships as the
``nodejs-wheel-binaries`` Python package, so users don't install Node themselves, and we
install it from npm with a committed lockfile because its standalone binary replaces
itself with the newest release on every start.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from nodejs_wheel import executable as nodejs

from . import config

PACKAGE = "@intelligentelectron/universal-netlist"


class NetlistError(RuntimeError):
    """universal-netlist is missing or failed on a design."""


def _pin_file(name: str) -> bytes:
    return (
        resources.files("altium_helper")
        .joinpath(f"data/universal-netlist/{name}")
        .read_bytes()
    )


def pinned_version() -> str:
    return json.loads(_pin_file("package.json"))["dependencies"][PACKAGE]


def install_dir() -> Path:
    return config.data_dir() / "universal-netlist"


def _package_dir() -> Path:
    return install_dir() / "node_modules" / "@intelligentelectron" / "universal-netlist"


def script() -> Path:
    return _package_dir() / "dist" / "index.js"


def node_executable() -> Path:
    root = Path(nodejs.ROOT_DIR)
    return root / "node.exe" if os.name == "nt" else root / "bin" / "node"


def _npm_cli() -> Path:
    root = Path(nodejs.ROOT_DIR)
    for candidate in (
        root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        root / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ):
        if candidate.exists():
            return candidate
    raise NetlistError(f"npm not found in the Node.js wheel at {root}")


def installed_version() -> str | None:
    manifest = _package_dir() / "package.json"
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["UNIVERSAL_NETLIST_TELEMETRY_PATH"] = os.devnull  # keep no usage log
    env["OTEL_SDK_DISABLED"] = "1"
    return env


def install(force: bool = False) -> str:
    """Install the pinned version with ``npm ci`` unless it's already there. Returns the version."""
    target = install_dir()
    lock = _pin_file("package-lock.json")
    if (
        not force
        and installed_version() == pinned_version()
        and (target / "package-lock.json").exists()
    ):
        if (target / "package-lock.json").read_bytes() == lock:
            return pinned_version()
    target.mkdir(parents=True, exist_ok=True)
    (target / "package.json").write_bytes(_pin_file("package.json"))
    (target / "package-lock.json").write_bytes(lock)
    result = subprocess.run(
        [
            str(node_executable()),
            str(_npm_cli()),
            "ci",
            "--omit=dev",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise NetlistError(
            f"npm ci failed in {target}:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return pinned_version()


def _require_installed() -> None:
    if not script().exists():
        raise NetlistError(
            "universal-netlist isn't installed yet. Run: altium-helper setup"
        )


def serve() -> int:
    """Run universal-netlist's MCP server on this process's stdin and stdout."""
    _require_installed()
    return subprocess.call([str(node_executable()), str(script())], env=_environment())


def export_json(design: str | Path, out: str | Path) -> Path:
    """Write the netlist of ``design`` (a .PrjPcb or .netlist.json) to ``out`` (must end in .netlist.json)."""
    _require_installed()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(node_executable()), str(script()), "export-json", str(design), str(out)],
        capture_output=True,
        text=True,
        env=_environment(),
    )
    if result.returncode != 0 or not out.exists():
        message = (result.stderr or result.stdout).strip()
        raise NetlistError(f"universal-netlist couldn't read {design}: {message}")
    return out


def read_netlist(design: str | Path) -> dict:
    """Return the Universal Netlist JSON of a design, exporting it through a temporary file."""
    with tempfile.TemporaryDirectory(prefix="altium-helper-") as tmp:
        out = export_json(design, Path(tmp) / "design.netlist.json")
        return json.loads(out.read_text(encoding="utf-8"))
