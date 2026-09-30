"""The ``altium-helper`` command."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, checks, netlist, pcbdoc


def _repo_root() -> Path | None:
    """This repository's root when altium-helper runs from a git checkout (an editable install)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "hooks").is_dir() else None


def cmd_setup(args: argparse.Namespace) -> int:
    version = netlist.install(force=args.force)
    print(f"universal-netlist {version} is installed in {netlist.install_dir()}")
    root = _repo_root()
    if root:
        subprocess.run(
            ["git", "-C", str(root), "config", "core.hooksPath", "hooks"], check=False
        )
        print(f"Enabled this repository's git hooks ({root / 'hooks'})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    design = Path(args.design)
    out = (
        Path(args.output) if args.output else Path.cwd() / f"{design.stem}.netlist.json"
    )
    print(netlist.export_json(design, out))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    report = checks.check_project(args.project)
    print(json.dumps(report, indent=2) if args.json else checks.summarize(report))
    return 0


def cmd_serve_netlist(args: argparse.Namespace) -> int:
    return netlist.serve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="altium-helper",
        description="Read-only access to Altium 365 board designs and their history, for Claude.",
    )
    parser.add_argument(
        "--version", action="version", version=f"altium-helper {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    setup = commands.add_parser(
        "setup", help="install universal-netlist and prepare this machine"
    )
    setup.add_argument(
        "--force", action="store_true", help="reinstall universal-netlist"
    )
    setup.set_defaults(func=cmd_setup)

    export = commands.add_parser(
        "export", help="write a design's netlist as .netlist.json"
    )
    export.add_argument("design", help="a .PrjPcb (or .netlist.json)")
    export.add_argument("-o", "--output", help="output file ending in .netlist.json")
    export.set_defaults(func=cmd_export)

    check = commands.add_parser(
        "check", help="check a project: documents present, schematic matches PCB"
    )
    check.add_argument("project", help="path to a .PrjPcb")
    check.add_argument(
        "--json", action="store_true", help="print the full report as JSON"
    )
    check.set_defaults(func=cmd_check)

    serve = commands.add_parser(
        "serve-netlist", help="run universal-netlist's MCP server (used by Claude)"
    )
    serve.set_defaults(func=cmd_serve_netlist)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (netlist.NetlistError, pcbdoc.PcbDocError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
