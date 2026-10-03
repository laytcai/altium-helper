"""The MCP server Claude uses to fetch boards and see how they changed.

Connectivity queries (parts, nets, pins, traces) are universal-netlist's job: ``get_board``
returns the design path to pass to its tools.
"""

from __future__ import annotations

import contextlib
import functools
import re

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


# How much of Altium 365's commit list board_history reads before asking git instead.
API_HISTORY_LIMIT = 10_000
COMMIT_ID = re.compile(r"[0-9a-fA-F]{4,40}")


def _when(text: str | None):
    return parse_when(text) if text else None


def _history_from_api(
    board: boards.Board,
    since: str | None,
    until: str | None,
    limit: int,
    note: str | None = None,
) -> dict:
    """The board's commits from Altium 365's own list, which matches its git history.

    Raises api.ApiError if the list doesn't reach back to the window asked for, so git
    answers instead; once git has failed (``note`` given), the note says so.
    """
    start, end = _when(since), _when(until)
    asked = max(limit, 100)
    while True:
        listed = cloud.revisions(board, limit=asked)
        commits = [
            c
            for c in listed
            if (not start or parse_when(c["date"]) >= start)
            and (not end or parse_when(c["date"]) <= end)
        ][:limit]
        complete = len(listed) < asked
        reached = bool(start and listed and parse_when(listed[-1]["date"]) < start)
        if complete or reached or len(commits) == limit:
            break
        if asked >= API_HISTORY_LIMIT:
            if note is None:
                raise api.ApiError(
                    f"Altium 365's commit list for {board.name} doesn't reach that far back"
                )
            note += f" Only its newest {asked} commits were read."
            break
        asked *= 10
    for commit in commits:
        commit["design_files_changed"] = [
            f["path"] for f in commit["files"] if boards.is_design_file(f["path"])
        ]
    result = {
        "board": board.name,
        "source": "Altium 365 API",
        "since": start.isoformat() if start else None,
        "until": end.isoformat() if end else None,
        "commits": commits,
    }
    return {**result, "note": note} if note else result


def _fetch_history_for(
    board: boards.Board,
    since=None,
    until=None,
    revisions: tuple[str | None, ...] = (),
    with_parent: bool = False,
) -> None:
    """Fetch the history a call needs in one request, sized from Altium 365's commit list.

    A copy that starts at its latest revision can only guess, by count or date, how far
    back to fetch, and each request takes seconds on Altium's server. Each history
    reader still checks for itself afterwards, and fetches more if this fell short.
    ``with_parent``: the call also needs the commit before the one it names.
    """
    if not (board.project_id and board.cloned() and board.is_shallow()):
        return
    extra = 2 if with_parent else 1
    targets = [t for t in revisions if t and not board.resolve(t)]
    if not (since or until or targets):
        return
    # A commit from before the moment, to the second, as Board.ensure_since needs.
    since_s = since.replace(microsecond=0) if since else None
    until_s = until.replace(microsecond=0) if until else None
    for limit in (100, 1000, API_HISTORY_LIMIT):
        try:
            listed = cloud.revision_times(board, limit=limit)
        except api.ApiError:
            return
        complete = len(listed) < limit
        depth, found = 0, set()
        found_since, found_until, reached_until = not since, not until, not until
        for index, (rev, when) in enumerate(listed):
            if not found_since and when < since_s:
                depth, found_since = max(depth, index + 1), True
            if not found_until and when <= until:  # the range's end
                depth, found_until = max(depth, index + extra), True
            if not reached_until and when < until_s:
                depth, reached_until = max(depth, index + 1), True
            for target in targets:
                if rev.startswith(target.lower()):
                    depth = max(depth, index + extra)
                    found.add(target)
        covered = found_since and found_until and reached_until
        if (covered and len(found) == len(targets)) or complete:
            break
    head = board.resolve("HEAD")
    if complete and any(rev == head for rev, _ in listed):
        # Altium's whole list, which holds this copy's latest commit: what isn't in it
        # doesn't exist, and fetching all the history first, as git alone would have
        # to, can't change the answer.
        for target in targets:
            if target not in found and COMMIT_ID.fullmatch(target):
                raise boards.BoardError(f"{board.name}: no revision {target!r}")
        if not found_until:
            raise boards.BoardError(f"{board.name} has no revisions in that range")
    if depth:
        board.ensure_depth(depth)


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
    _fetch_history_for(found, revisions=(revision,))
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
    found.ensure_allowed()  # the commit list counts as fetching the board
    if found.project_id and not (found.cloned() and not found.is_shallow()):
        # The copy doesn't hold the history (yet): Altium's list answers in about a
        # second, where git would first download it.
        with contextlib.suppress(api.ApiError):
            return _history_from_api(found, since, until, limit)
    synced, reason = _sync_or_none(found)
    if synced is None:
        return _history_from_api(
            found,
            since,
            until,
            limit,
            note=f"From Altium 365's commit list; git couldn't fetch the files ({reason}).",
        )
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
    if since or until or from_revision or to_revision:
        _fetch_history_for(  # only the ends board_changes uses: a revision wins
            found,
            since=None if from_revision else _when(since),
            until=None if to_revision else _when(until),
            revisions=(from_revision, to_revision),
            with_parent=not (since or from_revision),
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
    if revision != "latest":
        _fetch_history_for(found, revisions=(revision,))
    analysis = found.analyze("HEAD" if revision == "latest" else revision)
    if analysis["check"] is None:
        raise boards.BoardError(f"{found.name} has no project at revision {revision}")
    return {"board": found.name, "revision": analysis["rev"], **analysis["check"]}


def main() -> None:
    server.run("stdio")
