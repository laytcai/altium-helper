"""The ``altium-helper`` command."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import (
    __version__,
    api,
    boards,
    checks,
    cloud,
    config,
    git,
    history,
    netlist,
    nexar,
    pcbdoc,
    setup_cmd,
)
from .timeparse import parse_when


def _repo_root() -> Path | None:
    """This repository's root when altium-helper runs from a git checkout (an editable install)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "hooks").is_dir() else None


def _print(data: dict | list, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2) if as_json else text)


def _subject(message: str) -> str:
    """A commit's first line. Altium doesn't ask for a message, so many are empty."""
    return message.strip().splitlines()[0] if message.strip() else "(no message)"


def cmd_setup(args: argparse.Namespace) -> int:
    version = netlist.install(force=args.force)
    print(f"universal-netlist {version} is installed in {netlist.install_dir()}")
    command = setup_cmd.launcher()
    if args.no_register:
        print(setup_cmd.manual_instructions(command))
    else:
        claude = setup_cmd.find_claude()
        if claude:
            setup_cmd.register_claude(claude, command)
            print(
                f"Registered altium-helper and universal-netlist with Claude Code ({claude})"
            )
        else:
            print("Claude Code not found.\n" + setup_cmd.manual_instructions(command))
        if codex := shutil.which("codex"):
            setup_cmd.register_codex(codex, command)
            print("Registered them with Codex too")
        print(
            f"Installed the altium-boards skill in {setup_cmd.install_skill().parent}"
        )
    root = _repo_root()
    if root:
        subprocess.run(
            ["git", "-C", str(root), "config", "core.hooksPath", "hooks"], check=False
        )
        print(f"Enabled this repository's git hooks ({root / 'hooks'})")
    print(
        "Next: run `altium-helper login`, then restart Claude so it loads the new tools."
    )
    return 0


def _ask(prompt: str, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise ValueError(f"{prompt.strip(': ')} is needed; run this in a terminal")
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def _choose_workspace(spaces: list[dict], wanted: str | None) -> dict:
    if not spaces:
        raise api.ApiError("Your Altium account isn't in any Altium 365 workspace")
    if wanted:
        matches = [
            s
            for s in spaces
            if wanted.lower() in s["url"].lower() or wanted.lower() in s["name"].lower()
        ]
        if len(matches) != 1:
            names = ", ".join(s["name"] for s in spaces)
            raise ValueError(
                f"--workspace {wanted!r} matches {len(matches)} of: {names}"
            )
        return matches[0]
    if len(spaces) == 1:
        return spaces[0]
    for number, space in enumerate(spaces, 1):
        default = " (default)" if space.get("isDefault") else ""
        print(f"  {number}. {space['name']}  {space['url']}{default}")
    fallback = next((i for i, s in enumerate(spaces, 1) if s.get("isDefault")), 1)
    answer = _ask(f"Which workspace has the team's boards? [{fallback}] ") or str(
        fallback
    )
    return spaces[int(answer) - 1]


def _setup_git(mode: str | None, with_git: list[dict]) -> None:
    """Sign git in to Altium 365 and test it on one board; keep nothing that doesn't work."""
    settings = config.Settings.load()
    if not with_git:
        print(
            "None of the boards has a git repository, so the tools will use PCB data only."
        )
        return
    url = with_git[0]["git_url"]
    if settings.git_auth != "token":
        if mode is None:
            if not sys.stdin.isatty():
                mode = "skip"
            else:
                answer = _ask("Try git with your Altium email and password? [Y/n] ")
                mode = "skip" if answer.lower().startswith("n") else "password"
        if mode == "skip":
            print(
                "Skipped git sign-in: until git works, the tools use PCB data from "
                "Altium 365 (no schematic history)."
            )
            return
        credentials = config.load_credentials()
        credentials["git"] = {
            "username": _ask("Altium account email: "),
            "password": _ask("Altium password: ", secret=True),
        }
        config.save_credentials(credentials)
        settings.git_auth = "password"
        settings.save()
    print(f"Testing git on {with_git[0]['name']} ...")
    ok, message = boards.test_git_access(url)
    if ok:
        print(
            "Git can read the boards. Every question gets the full design and its history."
        )
        return
    if settings.git_auth == "password":
        credentials = config.load_credentials()
        credentials.pop("git", None)
        config.save_credentials(credentials)
        settings.git_auth = "none"
        settings.save()
    print(
        f"Altium's git server refused the sign-in:\n  {message.splitlines()[0]}\n"
        "Until git works, the tools use PCB data from Altium 365 (no schematic history).\n"
        "Other ways in are in the README, under 'Getting into Altium 365'."
    )


def cmd_login(args: argparse.Namespace) -> int:
    settings = config.Settings.load()
    credentials = config.load_credentials()
    if args.token:
        workspace = args.workspace or _ask(
            "Altium 365 workspace URL (e.g. https://yourteam.365.altium.com): "
        )
        token = os.environ.get("ALTIUM365_TOKEN") or _ask(
            "Altium 365 API token (from a workspace admin): ", secret=True
        )
        credentials["altium365"] = {"token": token}
        credentials["git"] = {"username": "token", "password": token}
        config.save_credentials(credentials)
        settings.api, settings.git_auth = "altium365", "token"
        settings.workspace_url = workspace.rstrip("/")
        settings.save()
    else:
        stored = credentials.get("nexar", {})
        client_id = (
            args.client_id
            or os.environ.get("NEXAR_CLIENT_ID")
            or stored.get("client_id")
            or _ask("Nexar app client ID: ")
        )
        client_secret = (
            args.client_secret
            or os.environ.get("NEXAR_CLIENT_SECRET")
            or stored.get("client_secret")
            or _ask("Nexar app client secret: ", secret=True)
        )
        nexar.login(client_id, client_secret)
        settings = config.Settings.load()
        settings.api = "nexar"
        settings.save()
        chosen = _choose_workspace(cloud.workspaces(), args.workspace)
        settings.workspace_url = chosen["url"]
        settings.save()
        print(f"Signed in. Workspace: {chosen['name']} ({chosen['url']})")
    entries = cloud.discover_boards()
    with_git = [e for e in entries if e["git_url"]]
    names = ", ".join(e["name"] for e in entries[:25])
    more = f" and {len(entries) - 25} more" if len(entries) > 25 else ""
    print(
        f"Found {len(entries)} boards ({len(with_git)} with a git repository): {names}{more}"
    )
    _setup_git(args.git, with_git)
    print("Done. Ask Claude about a board, e.g. 'what changed on <board> yesterday?'")
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    board = boards.find(args.board)
    board.ensure_allowed()
    threads = cloud.comments(board)
    lines = []
    for thread in threads:
        state = "open" if thread["open"] else "resolved"
        lines.append(
            f"#{thread['thread']} ({state}, {thread['document'] or 'project'})"
        )
        for comment in thread["comments"]:
            lines.append(f"  {comment['at'][:16]} {comment['by']}: {comment['text']}")
    _print(threads, args.json, "\n".join(lines) or f"No comments on {board.name}.")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import (  # imported here: the MCP SDK is slow to load for other commands
        mcp_server,
    )

    mcp_server.main()
    return 0


def cmd_add_board(args: argparse.Namespace) -> int:
    settings = config.Settings.load()
    key = boards.slug(args.key)
    settings.boards[key] = config.BoardConfig(
        git_url=args.git_url,
        project_file=args.project or "",
        name=args.name or args.key,
    )
    settings.save()
    print(
        f"Added {args.name or args.key} as '{key}'. Fetch it with: altium-helper sync {key}"
    )
    return 0


def cmd_boards(args: argparse.Namespace) -> int:
    if args.refresh:
        cloud.discover_boards()
    rows = []
    for board in sorted(boards.all_boards().values(), key=lambda b: b.name.lower()):
        meta = board.meta()
        rows.append(
            {
                "key": board.key,
                "name": board.name,
                "project_id": board.project_id,
                "last_sync": meta.get("last_sync"),
                "head": meta.get("head"),
            }
        )
    text = "\n".join(
        f"{r['key']:<24} {r['name']:<32} "
        + (f"synced {r['last_sync'][:16]}" if r["last_sync"] else "not fetched yet")
        + (f"  {r['project_id']}" if r["project_id"] else "")
        for r in rows
    )
    _print(
        rows,
        args.json,
        text
        or "No boards yet. Run `altium-helper login`, or add one with `altium-helper add-board`.",
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.all:  # every board already fetched; not every project in the workspace
        targets = [b for b in boards.all_boards().values() if b.cloned()]
        if not targets:
            print("No boards fetched yet. Fetch one with: altium-helper sync <board>")
    else:
        targets = [boards.find(args.board)]
    for board in targets:
        result = board.sync(force=True)
        head = result["latest_revision"]
        print(
            f"{board.name}: {'updated' if result['updated'] else 'up to date'}, latest {head['rev']} {head['date'][:16]} {head['author']}: {_subject(head['message'])}"
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    board = boards.find(args.board)
    result = history.board_history(
        board,
        since=parse_when(args.since) if args.since else None,
        until=parse_when(args.until) if args.until else None,
        limit=args.limit,
    )
    lines = []
    for commit in result["commits"]:
        design = (
            f" [{len(commit['design_files_changed'])} design files]"
            if commit["design_files_changed"]
            else ""
        )
        lines.append(
            f"{commit['rev']} {commit['date'][:16]} {commit['author']}: {_subject(commit['message'])}{design}"
        )
    _print(result, args.json, "\n".join(lines) or "No commits in that range.")
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    board = boards.find(args.board)
    result = history.board_changes(
        board,
        since=parse_when(args.since) if args.since else None,
        until=parse_when(args.until) if args.until else None,
        from_rev=args.from_rev,
        to_rev=args.to_rev,
    )
    lines = [f"{result['board']}: {result['commit_count']} commits"]
    for commit in result["commits"]:
        lines.append(
            f"\n{commit['rev']} {commit['date'][:16]} {commit['author']}: {_subject(commit['message'])}"
        )
        lines += [f"  {line}" for line in commit["changes"]]
    lines.append("\nOverall:")
    lines += [f"  {line}" for line in result["overall"]]
    pcb = result.get("pcb")
    if pcb:
        lines.append(
            "PCB files changed: " + (", ".join(pcb["pcb_files_changed"]) or "none")
        )
        for report in pcb["schematic_vs_pcb_at_end"] or []:
            if "matching" in report:
                lines.append(
                    f"Schematic vs {report['document']} at the end: {report['matching']}/{report['pcb_nets']} PCB nets match"
                )
    _print(result, args.json, "\n".join(lines))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if target.suffix.lower() == ".prjpcb" and target.exists():
        report = checks.check_project(target)
    else:
        board = boards.find(args.target)
        board.sync()
        report = board.analyze(args.rev)["check"]
        if report is None:
            print(
                f"{board.name} has no project at revision {args.rev}", file=sys.stderr
            )
            return 1
    _print(report, args.json, checks.summarize(report))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    design = Path(args.design)
    out = (
        Path(args.output) if args.output else Path.cwd() / f"{design.stem}.netlist.json"
    )
    print(netlist.export_json(design, out))
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

    def command(
        name: str, func, help: str, json_flag: bool = False
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.set_defaults(func=func)
        if json_flag:
            sub.add_argument("--json", action="store_true", help="print JSON")
        return sub

    setup = command(
        "setup", cmd_setup, "install universal-netlist and prepare this machine"
    )
    setup.add_argument(
        "--force", action="store_true", help="reinstall universal-netlist"
    )
    setup.add_argument(
        "--no-register",
        action="store_true",
        help="don't touch Claude's settings; print the commands instead",
    )

    add = command(
        "add-board",
        cmd_add_board,
        "add a board by its git URL (for boards no API lists)",
    )
    add.add_argument("key", help="short name, e.g. daq")
    add.add_argument("git_url", help="the repository URL")
    add.add_argument(
        "--project", help="path of the .PrjPcb in the repository, if it holds several"
    )
    add.add_argument("--name", help="display name")

    login = command(
        "login", cmd_login, "sign in to Altium 365, find the boards, set up git"
    )
    login.add_argument(
        "--token",
        action="store_true",
        help="use an Altium 365 API token from a workspace admin instead of Nexar",
    )
    login.add_argument("--workspace", help="workspace URL or name, if you have several")
    login.add_argument("--client-id", help="Nexar app client ID (or NEXAR_CLIENT_ID)")
    login.add_argument(
        "--client-secret", help="Nexar app client secret (or NEXAR_CLIENT_SECRET)"
    )
    login.add_argument(
        "--git",
        choices=["password", "skip"],
        help="git sign-in: your Altium email and password, or skip",
    )

    listing = command("boards", cmd_boards, "list known boards", json_flag=True)
    listing.add_argument(
        "--refresh", action="store_true", help="fetch the board list from Altium 365"
    )

    comments = command(
        "comments", cmd_comments, "show a board's Altium 365 comments", json_flag=True
    )
    comments.add_argument("board")

    sync = command("sync", cmd_sync, "fetch a board's latest revisions now")
    sync.add_argument("board", nargs="?", help="board name")
    sync.add_argument("--all", action="store_true", help="every board fetched before")

    log = command("history", cmd_history, "list a board's commits", json_flag=True)
    log.add_argument("board")
    log.add_argument("--since", help="e.g. yesterday, '3 days ago', 2026-09-29")
    log.add_argument("--until", help="same forms as --since")
    log.add_argument("-n", "--limit", type=int, default=50)

    changes = command(
        "changes",
        cmd_changes,
        "show what changed in a board's connectivity",
        json_flag=True,
    )
    changes.add_argument("board")
    changes.add_argument(
        "--since", help="compare against the board as it was at this time"
    )
    changes.add_argument("--until", help="end at the last commit before this time")
    changes.add_argument("--from", dest="from_rev", help="start revision")
    changes.add_argument("--to", dest="to_rev", help="end revision")

    check = command(
        "check",
        cmd_check,
        "check a board or .PrjPcb: documents present, schematic matches PCB",
        json_flag=True,
    )
    check.add_argument("target", help="board name or path to a .PrjPcb")
    check.add_argument("--rev", default="HEAD", help="revision of a board to check")

    export = command("export", cmd_export, "write a design's netlist as .netlist.json")
    export.add_argument("design", help="a .PrjPcb (or .netlist.json)")
    export.add_argument("-o", "--output", help="output file ending in .netlist.json")

    command("mcp", cmd_mcp, "run altium-helper's MCP server (Claude starts this)")
    command(
        "serve-netlist",
        cmd_serve_netlist,
        "run universal-netlist's MCP server (Claude starts this)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync" and not (args.board or args.all):
        parser.error("sync needs a board name or --all")
    try:
        return args.func(args)
    except (
        api.ApiError,
        boards.BoardError,
        git.GitError,
        netlist.NetlistError,
        pcbdoc.PcbDocError,
        setup_cmd.SetupError,
        FileNotFoundError,
        ValueError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
