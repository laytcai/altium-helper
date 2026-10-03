"""Boards end to end on a synthetic repository: clone, history, changes, guards.

The design is a Universal Netlist file, which universal-netlist reads like any project,
so these tests need no Altium files.
"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from altium_helper import boards, cli, config, git
from altium_helper.boards import BoardError
from altium_helper.history import board_changes, board_history
from altium_helper.timeparse import parse_when

pytestmark = pytest.mark.network  # needs universal-netlist, installed from npm

DESIGN = "board/DAQ.netlist.json"


def test_find_by_name_ignoring_case_and_partial(board):
    assert boards.find("DAQ").key == "daq"
    assert boards.find("da").key == "daq"
    with pytest.raises(BoardError, match="No board called"):
        boards.find("pdu")


def test_sync_clones_read_only(board):
    result = board.sync()
    assert result["updated"] is True
    assert result["latest_revision"]["message"] == "Add CAN terminator"
    push_url = subprocess.run(
        ["git", "-C", str(board.repo), "remote", "get-url", "--push", "origin"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert push_url == git.PUSH_DISABLED
    hook = board.repo / ".git" / "hooks" / "pre-push"
    assert "exit 1" in hook.read_text(encoding="utf-8")
    assert board.sync()["updated"] is False  # fresh copy: no fetch


def test_history_since_yesterday(board):
    result = board_history(board, since=parse_when("yesterday"))
    assert [c["message"] for c in result["commits"]] == [
        "Add CAN terminator",
        "Fix flipped CAN pins",
    ]
    assert result["commits"][1]["author"] == "Alice"
    assert result["commits"][1]["design_files_changed"] == [DESIGN]


def test_changes_since_yesterday_find_the_swap(board):
    result = board_changes(board, since=parse_when("yesterday"))
    assert result["commit_count"] == 2
    fix, terminator = result["commits"]
    assert fix["author"] == "Alice"
    assert fix["changes"] == [
        "Swapped: U3.12 (PB12) and U3.13 (PB13) traded nets CAN_RX <-> CAN_TX"
    ]
    assert terminator["changes"] == ["Part added: R9 (120)"]
    assert result["overall"][0].startswith("Swapped: U3.12 (PB12) and U3.13 (PB13)")
    assert "Part added: R9 (120)" in result["overall"]


def test_commits_without_a_message(board, add_commit, capsys):
    """Altium doesn't ask for a message when someone saves a project to the server."""
    add_commit("", "Carol", tx="CAN_RX", rx="CAN_TX")
    assert board.sync()["latest_revision"]["message"] == ""
    assert board_history(board)["commits"][0]["message"] == ""
    assert board_changes(board)["commits"][0]["changes"][0].startswith("Swapped:")
    assert cli.main(["history", "daq"]) == 0
    assert cli.main(["changes", "daq"]) == 0
    assert "Carol: (no message)" in capsys.readouterr().out


def test_parallel_calls_on_a_board_nobody_fetched_yet(board, daq_origin, add_commit):
    """Claude often calls two tools at once, and both may be first to fetch a board."""
    # As the API lists boards: the first clone finds the project file.
    (daq_origin / "board" / "DAQ.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    add_commit("Add the project file", "Bob", tx="CAN_TX", rx="CAN_RX")
    settings = config.Settings.load()
    settings.boards["daq"].project_file = ""
    settings.save()
    start = threading.Barrier(2)

    def call(_) -> str:
        found = boards.find("daq")
        start.wait()  # both look the board up before either fetches it
        return found.sync()["project"]

    with ThreadPoolExecutor(2) as pool:
        projects = list(pool.map(call, range(2)))
    assert projects == [str(board.repo / "board" / "DAQ.PrjPcb")] * 2


def test_parallel_analyses_of_one_revision(board):
    board.sync()
    start = threading.Barrier(2)

    def call(_) -> dict:
        start.wait()
        return board.analyze("HEAD")["netlist"]

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(call, range(2))
    assert first == second and "U3" in first["components"]


def test_revisions_are_cached(board):
    board.sync()
    first = board.analyze("HEAD")
    cached = Path(first["netlist_path"])
    stamp = cached.stat().st_mtime_ns
    assert board.analyze("HEAD")["netlist"] == first["netlist"]
    assert cached.stat().st_mtime_ns == stamp


def test_excluded_board_is_never_fetched(board):
    settings = config.Settings.load()
    settings.exclude = ["DAQ"]
    settings.save()
    with pytest.raises(BoardError, match="exclude"):
        board.sync()
    assert not board.cloned()


def test_local_edits_stop_a_sync(board):
    board.sync()
    (board.repo / DESIGN).write_text("edited", encoding="utf-8")
    with pytest.raises(BoardError, match="local changes"):
        board.sync(force=True)
