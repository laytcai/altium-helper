"""Answer "what changed on this board?" from its git history."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import diff
from .boards import Board, BoardError, is_design_file

# Commits beyond this many are covered only by the overall change, not one by one.
MAX_COMMITS = 25
# The files universal-netlist reads. A commit that changes none of them (only the
# .PcbDoc, say) leaves the netlist as it was.
NETLIST_SUFFIXES = (".prjpcb", ".schdoc", ".harness", ".netlist.json")
# Revisions analyzed at once; each runs universal-netlist in its own Node process.
ANALYSES_AT_ONCE = max(1, min(8, (os.cpu_count() or 2) // 2))


def board_history(
    board: Board,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
) -> dict:
    """Commits newest first, with the design files each one changed."""
    board.sync()
    if since:
        board.ensure_since(since)
    else:
        board.ensure_commits(limit, before=until)
    commits = board.log(since=since, until=until, limit=limit)
    for commit in commits:
        commit["design_files_changed"] = [
            f["path"] for f in commit["files"] if is_design_file(f["path"])
        ]
        del commit["full_rev"]
    return {
        "board": board.name,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "commits": commits,
    }


def _parent(board: Board, rev: str) -> str | None:
    return board.parent(rev)


def _changes_netlist(commit: dict) -> bool:
    return any(f["path"].lower().endswith(NETLIST_SUFFIXES) for f in commit["files"])


def _analyze_all(board: Board, revs: set[str]) -> dict[str, dict]:
    """Analyze revisions in parallel: on a big board each takes about a second."""
    ordered = sorted(revs)
    with ThreadPoolExecutor(max(1, min(len(ordered), ANALYSES_AT_ONCE))) as pool:
        return dict(zip(ordered, pool.map(board.analyze, ordered)))


def board_changes(
    board: Board,
    since: datetime | None = None,
    until: datetime | None = None,
    from_rev: str | None = None,
    to_rev: str | None = None,
) -> dict:
    """Netlist changes over a range, overall and commit by commit.

    The range ends at ``to_rev``, or the last commit before ``until``, or the latest revision.
    It starts at ``from_rev``, or the state at ``since``, or else just before the last commit.
    """
    board.sync()
    if to_rev:
        end = board.find_commit(to_rev)
    elif until:
        end = board.last_commit_before(until, exact=True)
    else:
        end = board.resolve("HEAD")
    if not end:
        raise BoardError(f"{board.name} has no revisions in that range")
    if from_rev:
        start = board.find_commit(from_rev)
        if not start:
            raise BoardError(f"{board.name}: no revision {from_rev!r}")
    elif since:
        start = board.last_commit_before(since)
    else:
        start = _parent(board, end)

    commits = board.log(revision_range=f"{start}..{end}" if start else end, limit=1000)
    commits.reverse()  # oldest first
    result: dict = {
        "board": board.name,
        "from": board.commit_info(start) if start else None,
        "to": board.commit_info(end),
        "commit_count": len(commits),
        "commits": [],
    }
    if not commits:
        result["overall"] = ["No commits in this range."]
        return result

    shown = commits[-MAX_COMMITS:]
    # Analyze only the revisions whose netlist can differ: the commits that change
    # universal-netlist's inputs, the state before the first one shown, and the ends.
    state = _parent(board, shown[0]["full_rev"])  # its netlist is the current one
    needed = {state, start, end} | {c["full_rev"] for c in shown if _changes_netlist(c)}
    analyses = _analyze_all(board, {rev for rev in needed if rev})
    for commit in shown:
        entry = {k: commit[k] for k in ("rev", "date", "author", "message")}
        entry["files"] = [f"{f['status']} {f['path']}" for f in commit["files"]]
        if not any(is_design_file(f["path"]) for f in commit["files"]):
            entry["changes"] = ["No design files changed"]
        elif not _changes_netlist(commit):
            entry["changes"] = ["No connectivity change"]
        else:
            before = analyses[state]["netlist"] if state else None
            after = analyses[commit["full_rev"]]["netlist"]
            changes = diff.diff_netlists(before or {}, after or {})
            entry["changes"] = diff.summarize(changes, after) or [
                "No connectivity change"
            ]
            state = commit["full_rev"]
        result["commits"].append(entry)
    if len(commits) > MAX_COMMITS:
        result["note"] = (
            f"Only the last {MAX_COMMITS} of {len(commits)} commits are broken down."
        )

    before = analyses[start]["netlist"] if start else None
    last = analyses[end]
    overall = diff.diff_netlists(before or {}, last["netlist"] or {})
    result["overall"] = diff.summarize(overall, last["netlist"]) or [
        "No connectivity change"
    ]
    result["overall_details"] = overall
    pcb_changed = sorted(
        {
            f["path"]
            for c in commits
            for f in c["files"]
            if f["path"].lower().endswith(".pcbdoc")
        }
    )
    result["pcb"] = {
        "pcb_files_changed": pcb_changed,
        "schematic_vs_pcb_at_end": last["check"]["pcb"] if last["check"] else None,
    }
    return result
