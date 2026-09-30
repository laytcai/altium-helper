"""Shared test setup. altium-helper's data and config folders point into .pytest_cache,
so tests never touch the real ones."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

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
