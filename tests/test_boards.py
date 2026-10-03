"""Boards end to end on a synthetic repository: clone, history, changes, guards.

The design is a Universal Netlist file, which universal-netlist reads like any project,
so these tests need no Altium files.
"""

import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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


def _push_url(board) -> str:
    return subprocess.run(
        ["git", "-C", str(board.repo), "remote", "get-url", "--push", "origin"],
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_an_interrupted_clone_is_cloned_again(board):
    """What a clone left behind when the server stopped and git finished on its own:
    no checkout, no push guard."""
    board.root.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", board.git_url, str(board.repo)],
        check=True,
    )
    assert not board.cloned()
    result = board.sync()
    assert result["updated"] is True
    assert result["latest_revision"]["message"] == "Add CAN terminator"
    assert _push_url(board) == git.PUSH_DISABLED
    assert board.cloned()


def test_a_killed_clone_is_cloned_again(board):
    """A git killed during a clone leaves an empty repository, with read-only files."""
    (board.repo / ".git" / "objects" / "pack").mkdir(parents=True)
    leftover = board.repo / ".git" / "objects" / "pack" / "tmp_pack_x"
    leftover.write_bytes(b"partial")
    leftover.chmod(stat.S_IREAD)  # as git writes them; Windows won't delete it as is
    assert board.sync()["latest_revision"]["message"] == "Add CAN terminator"
    assert not leftover.exists()


def test_stale_git_lock_files_are_cleared(board, add_commit):
    """A git killed while it held a lock file would otherwise stop every later sync."""
    board.sync()
    add_commit("Swap back", "Carol", tx="CAN_RX", rx="CAN_TX")
    old = time.time() - boards.STALE_GIT_LOCK_SECONDS - 60
    for name in ("index.lock", "HEAD.lock", "refs/remotes/origin/master.lock"):
        lock = board.repo / ".git" / name
        lock.write_text("", encoding="utf-8")
        os.utime(lock, (old, old))
    assert board.sync(force=True)["latest_revision"]["message"] == "Swap back"
    assert not list((board.repo / ".git").glob("**/*.lock"))


def test_freshness_counts_from_the_last_fetch(board, add_commit, monkeypatch):
    """A board in constant use still fetches once per sync interval."""
    start = boards._now()
    board.sync()
    add_commit("Later", "Carol", tx="CAN_TX", rx="CAN_RX")
    monkeypatch.setattr(boards, "_now", lambda: start + timedelta(minutes=3))
    assert board.sync()["updated"] is False  # fresh: no fetch
    monkeypatch.setattr(boards, "_now", lambda: start + timedelta(minutes=6))
    assert board.sync()["updated"] is True  # 6 minutes after the last fetch, not 3


def test_a_key_that_names_another_project_now_gets_a_new_copy(board, tmp_path):
    """Keys follow the API's listing order, so a key can come to name another project."""
    board.sync()
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "master", str(other)], check=True)
    (other / "board").mkdir()
    (other / DESIGN).write_bytes((board.repo / DESIGN).read_bytes())
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.name=Dana",
            "-c",
            "user.email=dana@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "Another board",
        ],
        check=True,
    )
    settings = config.Settings.load()
    settings.boards["daq"].git_url = str(other)
    settings.save()
    moved = boards.find("daq")
    assert not moved.cloned()
    assert moved.sync()["latest_revision"]["message"] == "Another board"
    assert _push_url(moved) == git.PUSH_DISABLED


def test_sync_all_updates_only_boards_fetched_before(board, capsys):
    settings = config.Settings.load()
    settings.boards["never"] = config.BoardConfig(git_url="http://127.0.0.1:9/x.git")
    settings.save()
    assert cli.main(["sync", "--all"]) == 0
    assert "No boards fetched yet" in capsys.readouterr().out
    board.sync()
    assert cli.main(["sync", "--all"]) == 0
    assert "DAQ: up to date" in capsys.readouterr().out
    assert not boards.find("never").cloned()


@pytest.mark.skipif(sys.platform == "win32", reason="git can't inherit the lock there")
def test_git_keeps_a_passed_lock_open(tmp_path):
    with boards._exclusive(tmp_path / ".lock") as held:
        assert held and git.run(["--version"], keep_fds=held).startswith("git")


@pytest.fixture
def git_calls(monkeypatch):
    """The first argument of every git command run from here on."""
    calls = []
    real = git.run

    def run(args, *rest, **options):
        calls.append(args[0])
        return real(args, *rest, **options)

    monkeypatch.setattr(git, "run", run)
    return calls


def test_a_copy_made_before_the_markers_is_kept(board, git_calls):
    board.sync()
    git_calls.clear()
    for marker in (boards.COMPLETE_MARKER, boards.DOWNLOADED_MARKER):
        (board.repo / ".git" / marker).unlink()
    assert board.cloned()
    assert board.sync(force=True)["updated"] is False
    assert "clone" not in git_calls


def test_a_finished_download_isnt_downloaded_again(
    board, daq_origin, add_commit, git_calls
):
    """A repository with no .PrjPcb fails after its download; later calls fail fast."""
    settings = config.Settings.load()
    settings.boards["daq"].project_file = ""
    settings.save()
    found = boards.find("daq")
    for _ in range(2):
        with pytest.raises(BoardError, match="no .PrjPcb"):
            found.sync()
    assert git_calls.count("clone") == 1
    (daq_origin / "board" / "DAQ.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    add_commit("Add the project file", "Bob", tx="CAN_TX", rx="CAN_RX")
    result = boards.find("daq").sync()
    assert result["project"].endswith("DAQ.PrjPcb")
    assert result["latest_revision"]["message"] == "Add the project file"
    assert git_calls.count("clone") == 1 and boards.find("daq").cloned()


def _repository(path: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    identity = ["-c", "user.name=Dana", "-c", "user.email=dana@example.com"]
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), *identity, "-c", "commit.gpgsign=false"]
        + ["commit", "-q", "-m", "First"],
        check=True,
    )
    return path


def test_an_api_board_whose_key_moved_finds_its_own_project(tmp_path, monkeypatch):
    """A key that comes to name another project must not keep the old project's file."""
    monkeypatch.setenv("ALTIUM_HELPER_DESIGNS", str(tmp_path / "designs"))
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    first = _repository(tmp_path / "a", {"A/A.PrjPcb": "[Design]\n"})
    second = _repository(tmp_path / "b", {"B/B.PrjPcb": "[Design]\n"})
    listing = config.designs_dir() / "boards.json"
    listing.parent.mkdir(parents=True)

    def listed(url: Path) -> boards.Board:
        entry = {"key": "pdu", "name": "PDU", "git_url": str(url), "project_id": "P"}
        listing.write_text(json.dumps([entry]), encoding="utf-8")
        return boards.find("pdu")

    assert listed(first).sync()["project"].endswith("A.PrjPcb")
    assert listed(first).project_file == "A/A.PrjPcb"
    moved = listed(second)
    assert moved.project_file == ""
    assert moved.sync()["project"].endswith("B.PrjPcb")


@pytest.mark.skipif(sys.platform == "win32", reason="git can't inherit the lock there")
def test_git_keeps_the_board_locked_after_the_server_dies(tmp_path):
    """A git still running when the MCP server is killed must keep the board locked."""
    lock, started = tmp_path / ".lock", tmp_path / "git-started"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from altium_helper import boards, git\n"
            "with boards._exclusive(Path(sys.argv[1])) as held:\n"
            "    git.run(['-c', f'alias.nap=!touch {sys.argv[2]}; sleep 4', 'nap'],"
            " keep_fds=held)\n",
            str(lock),
            started.as_posix(),
        ]
    )
    deadline = time.time() + 15
    while not started.exists():  # git is running its sleep once the file appears
        assert time.time() < deadline
        time.sleep(0.05)
    holder.kill()
    holder.wait()
    import fcntl

    with open(lock, "a+b") as handle:
        with pytest.raises(BlockingIOError):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.time() + 15
        while True:  # free again once git has exited
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.time() < deadline
                time.sleep(0.2)


def test_a_board_whose_repository_was_empty_recovers(tmp_path, monkeypatch):
    """Cloned before its first commit: the next sync after one must still work."""
    monkeypatch.setenv("ALTIUM_HELPER_DESIGNS", str(tmp_path / "designs"))
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "master", str(origin)], check=True)
    settings = config.Settings.load()
    settings.boards["new"] = config.BoardConfig(git_url=origin.as_uri(), name="New")
    settings.save()
    with pytest.raises((BoardError, git.GitError)):
        boards.find("new").sync()
    _repository(origin, {"New.PrjPcb": "[Design]\n"})
    assert boards.find("new").sync()["project"].endswith("New.PrjPcb")


def test_many_fetches_get_repacked(board, add_commit, monkeypatch):
    """git's background upkeep is off, so a copy repacks itself after many fetches."""
    monkeypatch.setattr(boards, "MAX_PACKS", 2)
    board.sync()
    for tx, rx in [("CAN_RX", "CAN_TX"), ("CAN_TX", "CAN_RX"), ("CAN_RX", "CAN_TX")]:
        add_commit(f"Swap to {tx}", "Carol", tx=tx, rx=rx)
        board.sync(force=True)
    packs = list((board.repo / ".git" / "objects" / "pack").glob("*.pack"))
    assert len(packs) == 1
    assert board_changes(board)["commits"][0]["changes"][0].startswith("Swapped:")
