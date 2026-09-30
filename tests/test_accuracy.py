"""universal-netlist's schematic netlist must match each public board's PCB exactly."""

import pytest

from altium_helper import checks

# Nets with two or more pads on each board's PCB. All of them must match, with the same names.
EXPECTED_NETS = {"mb1364": 287, "ubc-mainboard-v2": 227, "ubc-bob-breakout": 79}


@pytest.mark.network
@pytest.mark.parametrize("board", sorted(EXPECTED_NETS))
def test_schematic_matches_pcb(board, public_boards, universal_netlist):
    report = checks.check_project(public_boards[board])
    (pcb,) = report["pcb"]
    assert pcb["pcb_nets"] == EXPECTED_NETS[board]
    assert pcb["matching"] == EXPECTED_NETS[board]
    assert pcb["same_names"] == EXPECTED_NETS[board]
    assert pcb["differences"] == []
