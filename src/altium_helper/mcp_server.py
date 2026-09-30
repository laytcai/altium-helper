"""The MCP server Claude uses to fetch boards and see how they changed.

Connectivity queries (parts, nets, pins, traces) are universal-netlist's job: ``get_board``
returns the design path to pass to its tools.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, api, boards, cloud, git, history, netlist, pcbdoc
from .timeparse import parse_when

INSTRUCTIONS = """\
altium-helper reads Penn Electric Racing's board designs from Altium 365, read-only. It
downloads and updates boards by itself: never ask the user to download design files.

- Connectivity questions: call get_board, then pass the returned `design` path to the
  universal-netlist tools (query_component, query_xnet_by_net_name, search_nets, ...).
- "What changed" questions: board_changes (e.g. since="yesterday") explains each commit's
  connectivity change; board_history lists commits; board_comments shows the team's
  Altium 365 comments.
- If a result says it's PCB-only data from the API, say so: it has no schematic wiring and
  no per-commit history.
- universal-netlist quirks: query_xnet_by_pin_name takes pin numbers (U3.12), not pin
  names, so look the number up with query_component first. Traces stop at solder bridges
  (SB) and jumpers (JP); continue through them by hand. Designs with variants need
  design_variant on every call (list_designs shows them).
- Cite the board revision (commit, author, date) your answer is based on.
"""

server = MCPServer("altium-helper", version=__version__, instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=True
)
EXPECTED_ERRORS = (
    api.ApiError,
    boards.BoardError,
    git.GitError,
    netlist.NetlistError,
    pcbdoc.PcbDocError,
    ValueError,
)
NO_GIT = (
    "Couldn't fetch the design files with git ({reason}). This is the current PCB from "
    "Altium 365: pads, nets and part pin names, but no schematic wiring and no history."
)


def _tool(func):
    """Register a read-only tool whose expected failures reach Claude as plain messages."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except EXPECTED_ERRORS as e:
            raise ToolError(str(e)) from e

    return server.tool(annotations=READ_ONLY)(wrapper)


def _when(text: str | None):
    return parse_when(text) if text else None


def _find(board: str) -> boards.Board:
    try:
        return boards.find(board)
    except boards.BoardError:
        # A board added in Altium 365 since the list was fetched: look again, once.
        try:
            cloud.discover_boards()
        except api.ApiError:
            raise ToolError(f"No board called {board!r}") from None
        return boards.find(board)


def _sync_or_none(board: boards.Board) -> tuple[dict | None, str]:
    """Sync with git; on failure return (None, reason) so the caller can fall back."""
    board.ensure_allowed()  # never fall back to the API for an excluded board
    try:
        return board.sync(), ""
    except (git.GitError, boards.BoardError) as e:
        if not board.project_id:
            raise
        return None, str(e).splitlines()[0]


@_tool
def list_boards() -> dict:
    """List the boards (Altium 365 projects) this tool can read, with when each was last fetched."""
    try:
        cloud.refresh_boards_if_stale()
    except api.ApiError as e:
        note = f"Couldn't refresh the board list from Altium 365: {e}"
    else:
        note = None
    rows = []
    for board in sorted(boards.all_boards().values(), key=lambda b: b.name.lower()):
        meta = board.meta()
        rows.append(
            {
                "board": board.name,
                "key": board.key,
                "has_git": bool(board.git_url),
                "fetched": bool(meta.get("head")),
                "last_sync": meta.get("last_sync"),
            }
        )
    if not rows:
        raise boards.BoardError(
            "No boards are set up. The user needs to run `altium-helper login` once."
        )
    return {"boards": rows, **({"note": note} if note else {})}


@_tool
def get_board(board: str, revision: str = "latest") -> dict:
    """Fetch a board (or its new revisions) from Altium 365 and return its design path.

    Pass `design` to universal-netlist's tools. revision is "latest" or a commit from
    board_history; for a past revision `design` is a netlist file of that revision.
    """
    found = _find(board)
    synced, reason = _sync_or_none(found)
    if synced is None:
        return {
            "board": found.name,
            "design": str(cloud.pcb_snapshot(found)),
            "source": "Altium 365 API, PCB only",
            "note": NO_GIT.format(reason=reason),
        }
    if revision in ("latest", "HEAD", ""):
        return {
            "board": found.name,
            "design": synced["project"],
            "revision": synced["latest_revision"],
        }
    analysis = found.analyze(revision)
    if analysis["netlist_path"] is None:
        raise boards.BoardError(f"{found.name} has no project at revision {revision}")
    return {
        "board": found.name,
        "design": analysis["netlist_path"],
        "revision": found.commit_info(revision),
    }


@_tool
def board_history(
    board: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 30,
) -> dict:
    """List a board's commits, newest first: author, time, message and files changed.

    since/until take "yesterday", "today", "last week", "3 days ago" or an ISO date.
    """
    found = _find(board)
    synced, reason = _sync_or_none(found)
    if synced is None:
        start, end = _when(since), _when(until)
        commits = [
            c
            for c in cloud.revisions(found, limit=max(limit, 100))
            if (not start or parse_when(c["date"]) >= start)
            and (not end or parse_when(c["date"]) < end)
        ][:limit]
        return {
            "board": found.name,
            "source": "Altium 365 API",
            "note": f"From Altium 365's commit list; git couldn't fetch the files ({reason}).",
            "commits": commits,
        }
    return history.board_history(
        found, since=_when(since), until=_when(until), limit=limit
    )


@_tool
def board_changes(
    board: str,
    since: str | None = None,
    until: str | None = None,
    from_revision: str | None = None,
    to_revision: str | None = None,
) -> dict:
    """Explain how a board's connectivity changed, overall and commit by commit.

    Reports pins that moved or swapped nets, renamed nets, and parts added, removed or
    changed, plus whether the PCB file changed and still matches the schematic.
    With no range, shows the latest commit. since="yesterday" compares with the board as
    it was at the start of yesterday.
    """
    found = _find(board)
    synced, reason = _sync_or_none(found)
    if synced is None:
        raise boards.BoardError(
            f"Pin-level history needs the design files, and git couldn't fetch them ({reason}). "
            "board_history still lists who changed which files and when."
        )
    result = history.board_changes(
        found,
        since=_when(since),
        until=_when(until),
        from_rev=from_revision,
        to_rev=to_revision,
    )
    result.pop("overall_details", None)  # the summary lines say the same, shorter
    return result


@_tool
def board_comments(board: str) -> dict:
    """The team's comment threads on a board in Altium 365: who said what, when, and on which document."""
    found = _find(board)
    found.ensure_allowed()
    return {"board": found.name, "threads": cloud.comments(found)}


@_tool
def check_board(board: str, revision: str = "latest") -> dict:
    """Check a board: are its design documents present, and does the schematic match the PCB?"""
    found = _find(board)
    found.sync()
    analysis = found.analyze("HEAD" if revision == "latest" else revision)
    if analysis["check"] is None:
        raise boards.BoardError(f"{found.name} has no project at revision {revision}")
    return {"board": found.name, "revision": analysis["rev"], **analysis["check"]}


def main() -> None:
    server.run("stdio")
