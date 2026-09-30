"""The MCP server Claude uses to fetch boards and see how they changed.

Connectivity queries (parts, nets, pins, traces) are universal-netlist's job: ``get_board``
returns the design path to pass to its tools.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, boards, git, history, netlist, pcbdoc
from .timeparse import parse_when

INSTRUCTIONS = """\
altium-helper reads Penn Electric Racing's board designs from Altium 365, read-only. It
downloads and updates boards by itself: never ask the user to download design files.

- Connectivity questions: call get_board, then pass the returned `design` path to the
  universal-netlist tools (query_component, query_xnet_by_net_name, search_nets, ...).
- "What changed" questions: board_changes (e.g. since="yesterday") explains each commit's
  connectivity change; board_history lists commits.
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
    boards.BoardError,
    git.GitError,
    netlist.NetlistError,
    pcbdoc.PcbDocError,
    ValueError,
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


@_tool
def list_boards() -> dict:
    """List the boards (Altium 365 projects) this tool can read, with when each was last fetched."""
    rows = []
    for board in sorted(boards.all_boards().values(), key=lambda b: b.name.lower()):
        meta = board.meta()
        rows.append(
            {
                "board": board.name,
                "key": board.key,
                "fetched": bool(meta.get("head")),
                "last_sync": meta.get("last_sync"),
            }
        )
    if not rows:
        raise boards.BoardError(
            "No boards are set up. The user needs to run `altium-helper login` once."
        )
    return {"boards": rows}


@_tool
def get_board(board: str, revision: str = "latest") -> dict:
    """Fetch a board (or its new revisions) from Altium 365 and return its design path.

    Pass `design` to universal-netlist's tools. revision is "latest" or a commit from
    board_history; for a past revision `design` is a netlist file of that revision.
    """
    found = boards.find(board)
    synced = found.sync()
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
    return history.board_history(
        boards.find(board), since=_when(since), until=_when(until), limit=limit
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
    result = history.board_changes(
        boards.find(board),
        since=_when(since),
        until=_when(until),
        from_rev=from_revision,
        to_rev=to_revision,
    )
    result.pop("overall_details", None)  # the summary lines say the same, shorter
    return result


@_tool
def check_board(board: str, revision: str = "latest") -> dict:
    """Check a board: are its design documents present, and does the schematic match the PCB?"""
    found = boards.find(board)
    found.sync()
    analysis = found.analyze("HEAD" if revision == "latest" else revision)
    if analysis["check"] is None:
        raise boards.BoardError(f"{found.name} has no project at revision {revision}")
    return {"board": found.name, "revision": analysis["rev"], **analysis["check"]}


def main() -> None:
    server.run("stdio")
