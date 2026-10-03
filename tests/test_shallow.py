"""A first clone holds only the latest revision; older history comes when a tool needs it.

Altium 365's git server has no partial clone, and most of a board's history is old
versions of large binary files, so a shallow first clone is several times faster.
"""

import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from altium_helper import boards, git
from altium_helper.history import board_changes, board_history
from altium_helper.timeparse import parse_when

pytestmark = pytest.mark.network  # the board fixture needs universal-netlist

HISTORY_FETCHES = ("--deepen", "--shallow-since", "--unshallow")


@pytest.fixture
def shallow(board):
    if not board.git_url.startswith("file:"):
        pytest.skip("git clones a plain path in full")
    board.sync()
    return board


@pytest.fixture
def fetches(monkeypatch):
    """The arguments of every git fetch from here on."""
    calls = []
    real = git.run

    def run(args, *rest, **options):
        if args[0] == "fetch":
            calls.append(args)
        return real(args, *rest, **options)

    monkeypatch.setattr(git, "run", run)
    return calls


def _count(board) -> int:
    return int(board._git(["rev-list", "--count", "HEAD"]))


def _history_fetches(calls) -> list[str]:
    return [a for args in calls for a in args if a.startswith(HISTORY_FETCHES)]


def test_a_first_clone_holds_only_the_latest_revision(shallow):
    assert shallow.is_shallow() and _count(shallow) == 1
    assert shallow.sync()["latest_revision"]["message"] == "Add CAN terminator"


def test_the_latest_change_fetches_one_more_commit(shallow, fetches):
    result = board_changes(shallow)
    assert result["commits"][0]["changes"] == ["Part added: R9 (120)"]
    assert _history_fetches(fetches) == ["--deepen=1"]
    assert shallow.is_shallow() and _count(shallow) == 2


def test_changes_since_a_date_fetch_back_to_it(shallow, fetches):
    result = board_changes(shallow, since=parse_when("yesterday"))
    assert [c["message"] for c in result["commits"]] == [
        "Fix flipped CAN pins",
        "Add CAN terminator",
    ]
    assert result["commits"][0]["changes"][0].startswith("Swapped:")
    assert _history_fetches(fetches)[0].startswith("--shallow-since=")
    assert "--unshallow" not in _history_fetches(fetches)


def test_no_fetch_when_nothing_changed_since(shallow, fetches):
    newest = parse_when("yesterday") + timedelta(hours=30)  # see conftest.daq_origin
    result = board_changes(shallow, since=newest + timedelta(minutes=1))
    assert result["commit_count"] == 0
    assert _history_fetches(fetches) == []


def test_history_shows_what_each_commit_really_changed(shallow):
    """A shallow copy's first commit has no parent, so git lists every file as added."""
    commits = board_history(shallow, limit=1)["commits"]
    assert [c["message"] for c in commits] == ["Add CAN terminator"]
    assert [f["status"] for f in commits[0]["files"]] == ["M"]
    assert shallow.is_shallow()


def test_an_old_revision_is_fetched_when_asked_for(shallow, daq_origin):
    first = subprocess.run(
        ["git", "-C", str(daq_origin), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    analysis = shallow.analyze(first)
    assert analysis["netlist"]["components"]["U3"]["pins"]["12"]["net"] == "CAN_RX"
    assert shallow.analyze("HEAD~1")["netlist"]


def test_an_unknown_revision_is_still_an_error(shallow):
    with pytest.raises(boards.BoardError, match="no revision"):
        shallow.analyze("not-a-revision")
    assert shallow.is_shallow()  # a name that can't be a commit fetches nothing


def test_fetching_history_never_shortens_it(shallow):
    board_changes(shallow)  # 2 commits
    shallow.ensure_since(parse_when("today"))  # later than the oldest commit held
    shallow.ensure_commits(1)
    assert _count(shallow) == 2


def test_a_stale_shallow_lock_doesnt_block_fetching_history(shallow):
    lock = shallow.repo / ".git" / "shallow.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - boards.STALE_GIT_LOCK_SECONDS - 60
    os.utime(lock, (old, old))
    assert board_changes(shallow)["commits"][0]["changes"] == ["Part added: R9 (120)"]


def test_full_copies_never_fetch_history(board, fetches):
    if board.git_url.startswith("file:"):
        pytest.skip("only a plain path gives a full copy")
    board.sync()
    board_history(board)
    board_changes(board, since=parse_when("yesterday"))
    board.analyze("HEAD~2")
    assert _history_fetches(fetches) == []


# The fixture's commits (conftest.daq_origin): First DAQ layout at yesterday 10:00 minus a
# day, Fix flipped CAN pins at yesterday 10:00, Add CAN terminator at today 06:00.
def _yesterday(hours: float) -> datetime:
    return parse_when("yesterday") + timedelta(hours=hours)


def _commit_other_files(repo: Path, message: str, when: datetime) -> None:
    """A commit that touches only docs/, outside the board's folder (board/)."""
    notes = repo / "docs" / "notes.txt"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text(message, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dana",
        "GIT_AUTHOR_EMAIL": "dana@example.com",
        "GIT_COMMITTER_NAME": "Dana",
        "GIT_COMMITTER_EMAIL": "dana@example.com",
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    for args in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)


def test_since_a_commits_own_time_lists_that_commit(board):
    """git log --since counts a commit at exactly that second."""
    result = board_history(board, since=_yesterday(30))
    assert [c["message"] for c in result["commits"]] == ["Add CAN terminator"]


def test_until_reads_back_to_that_time(board):
    until = _yesterday(10) + timedelta(minutes=1)
    history = board_history(board, until=until, limit=1)
    assert [c["message"] for c in history["commits"]] == ["Fix flipped CAN pins"]
    changes = board_changes(board, until=until)
    assert [c["message"] for c in changes["commits"]] == ["Fix flipped CAN pins"]
    assert changes["commits"][0]["changes"][0].startswith("Swapped:")


def test_until_finds_the_last_change_to_the_board_folder(board, daq_origin):
    """A shallow copy's first commit seems to change every folder; it isn't the answer."""
    for minutes in (10, 20):
        _commit_other_files(
            daq_origin,
            f"Notes {minutes}",
            _yesterday(30.5) + timedelta(minutes=minutes),
        )
    result = board_changes(board, until=_yesterday(31))
    assert result["to"]["message"] == "Add CAN terminator"
    assert [c["message"] for c in result["commits"]] == ["Add CAN terminator"]
    assert result["commits"][0]["changes"] == ["Part added: R9 (120)"]


def test_a_limit_counts_the_board_folders_commits(board, daq_origin):
    for minutes in (10, 20, 30, 40):
        _commit_other_files(
            daq_origin,
            f"Notes {minutes}",
            _yesterday(30.5) + timedelta(minutes=minutes),
        )
    result = board_history(board, limit=3)
    assert [c["message"] for c in result["commits"]] == [
        "Add CAN terminator",
        "Fix flipped CAN pins",
        "First DAQ layout",
    ]


def test_an_old_revision_is_fetched_in_growing_steps(board, daq_origin, fetches):
    """Without Altium's commit list, there is no way to know how far back a commit is."""
    if not board.git_url.startswith("file:"):
        pytest.skip("git clones a plain path in full")
    for minute in range(40):
        _commit_other_files(
            daq_origin, f"Notes {minute}", _yesterday(31) + timedelta(minutes=minute)
        )
    board.sync()  # a copy of the latest revision only
    old = subprocess.run(
        ["git", "-C", str(daq_origin), "rev-parse", "HEAD~30"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert board.analyze(old)["netlist"]["components"]["R9"]
    assert _history_fetches(fetches) == ["--deepen=16", "--deepen=32"]


def test_an_old_revision_fetches_everything_last(board, daq_origin, fetches):
    """Past 16 + 32 + 64 commits, git alone fetches the rest of the history."""
    if not board.git_url.startswith("file:"):
        pytest.skip("git clones a plain path in full")
    for minute in range(120):
        _commit_other_files(
            daq_origin, f"Notes {minute}", _yesterday(31) + timedelta(minutes=minute)
        )
    board.sync()
    first = subprocess.run(
        ["git", "-C", str(daq_origin), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert board.analyze(first)["netlist"]["components"]["U3"]
    assert _history_fetches(fetches) == [
        "--deepen=16",
        "--deepen=32",
        "--deepen=64",
        "--unshallow",
    ]


def test_until_far_back_fetches_by_date_not_everything(board, daq_origin, fetches):
    """Counting back from the latest commit would guess five times, then fetch it all."""
    for minute in range(60):
        _commit_other_files(
            daq_origin, f"Notes {minute}", _yesterday(31) + timedelta(minutes=minute)
        )
    result = board_history(board, until=_yesterday(10) + timedelta(minutes=1), limit=1)
    assert [c["message"] for c in result["commits"]] == ["Fix flipped CAN pins"]
    assert "--unshallow" not in _history_fetches(fetches)
