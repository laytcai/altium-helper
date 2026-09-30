"""Boards end to end on a synthetic repository: clone, history, changes, guards.

The design is a Universal Netlist file, which universal-netlist reads like any project,
so these tests need no Altium files.
"""

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from altium_helper import boards, config, git, unformat
from altium_helper.boards import Board, BoardError
from altium_helper.history import board_changes, board_history
from altium_helper.timeparse import parse_when

pytestmark = pytest.mark.network  # needs universal-netlist, installed from npm

DESIGN = "board/DAQ.netlist.json"


def _design(tx: str, rx: str, extra: bool = False) -> dict:
    components = {
        "U3": {
            "pins": {
                "12": {"name": "PB12", "net": tx},
                "13": {"name": "PB13", "net": rx},
            }
        },
        "U4": {"pins": {"1": "CAN_TX", "4": "CAN_RX"}, "mpn": "TCAN1051"},
    }
    if extra:
        components["R9"] = {"pins": {"1": "CAN_TX", "2": "CAN_RX"}, "value": "120"}
    return unformat.build(components)


def _commit(
    repo: Path, design: dict, message: str, author: str, when: datetime
) -> None:
    unformat.write(repo / DESIGN, design)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.com",
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": f"{author.lower()}@example.com",
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    for args in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)


@pytest.fixture
def origin(tmp_path):
    """An 'Altium 365' repository: an old commit, yesterday's pin fix, today's new part."""
    repo = tmp_path / "origin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True
    )
    yesterday = parse_when("yesterday") + timedelta(hours=10)
    _commit(
        repo,
        _design("CAN_RX", "CAN_TX"),
        "First DAQ layout",
        "Bob",
        yesterday - timedelta(days=1),
    )
    _commit(
        repo, _design("CAN_TX", "CAN_RX"), "Fix flipped CAN pins", "Alice", yesterday
    )
    _commit(
        repo,
        _design("CAN_TX", "CAN_RX", extra=True),
        "Add CAN terminator",
        "Bob",
        yesterday + timedelta(hours=20),
    )
    return repo


@pytest.fixture
def board(origin, tmp_path, monkeypatch, universal_netlist):
    monkeypatch.setenv("ALTIUM_HELPER_DESIGNS", str(tmp_path / "designs"))
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    settings = config.Settings.load()
    settings.boards["daq"] = config.BoardConfig(
        git_url=str(origin), project_file=DESIGN, name="DAQ"
    )
    settings.save()
    return boards.find("daq")


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
