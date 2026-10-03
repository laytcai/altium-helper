"""Shared test setup. altium-helper's data and config folders point into .pytest_cache,
so tests never touch the real ones."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from altium_helper import boards, config, unformat
from altium_helper.timeparse import parse_when

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".pytest_cache" / "altium-helper"
os.environ["ALTIUM_HELPER_DATA"] = str(STATE / "data")
os.environ["ALTIUM_HELPER_CONFIG"] = str(STATE / "config")


def _load_fetch_script():
    spec = importlib.util.spec_from_file_location(
        "fetch_test_designs", ROOT / "scripts" / "fetch_test_designs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def universal_netlist():
    """The pinned universal-netlist, installed into the test data folder."""
    from altium_helper import netlist

    try:
        netlist.install()
    except netlist.NetlistError as e:
        pytest.skip(f"universal-netlist couldn't be installed: {e}")
    return netlist


@pytest.fixture(scope="session")
def public_boards() -> dict[str, Path]:
    """Board name -> .PrjPcb of the public test boards, downloaded into test-designs/."""
    fetch = _load_fetch_script()
    destination = ROOT / "test-designs"
    try:
        fetch.fetch(destination)
    except OSError as e:
        pytest.skip(f"couldn't download the public test boards: {e}")
    return {
        board: destination / board / project
        for board, project in fetch.PROJECTS.items()
    }


# A synthetic board repository, standing in for an Altium 365 project.

DESIGN = "board/DAQ.netlist.json"  # the synthetic board's project file


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
    for args in (
        ["add", "-A"],
        ["commit", "-q", "--allow-empty-message", "-m", message],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)


@pytest.fixture
def daq_origin(tmp_path):
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
def add_commit(daq_origin):
    """Add a commit to the 'Altium 365' repository, after the others."""

    def add(message: str, author: str, tx: str, rx: str) -> None:
        when = parse_when("yesterday") + timedelta(hours=31)
        _commit(daq_origin, _design(tx, rx, extra=True), message, author, when)

    return add


@pytest.fixture(params=["full", "shallow"])
def board(request, daq_origin, tmp_path, monkeypatch, universal_netlist):
    """The synthetic DAQ board, registered in a temporary config.

    Every test runs on both kinds of copy. Git clones a plain path in full whatever
    --depth says, like the copies made before clones became shallow. A file:// URL
    honours --depth, as Altium 365 does.
    """
    monkeypatch.setenv("ALTIUM_HELPER_DESIGNS", str(tmp_path / "designs"))
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    url = daq_origin.as_uri() if request.param == "shallow" else str(daq_origin)
    settings = config.Settings.load()
    settings.boards["daq"] = config.BoardConfig(
        git_url=url, project_file=DESIGN, name="DAQ"
    )
    settings.save()
    return boards.find("daq")
