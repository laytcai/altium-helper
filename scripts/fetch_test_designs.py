"""Download the public Altium boards used to test altium-helper into test-designs/ (git-ignored).

Source: https://github.com/UBC-Thunderbots/PCB_MainBoard. The repository has no license, so the
files are downloaded for local testing only and must never be committed.

Usage: python scripts/fetch_test_designs.py [destination]
"""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/UBC-Thunderbots/PCB_MainBoard/HEAD"

BOARDS = {
    # ST NUCLEO-144 (STM32H7A3): 8 hierarchical sheets with buses and design variants.
    "mb1364": (
        "_archive/mb1364/pcb",
        [
            "MB1364.PrjPcb",
            "MB1364.PcbDoc",
            "MB1364.OutJob",
            "MB1364_Top.SchDoc",
            "MCU_144_IOs.SchDoc",
            "MCU_144_POWER.SchDoc",
            "Power_Board.SchDoc",
            "ST_LINK_V3E.SchDoc",
            "USB.SchDoc",
            "Ethernet.SchDoc",
            "Connectors.SchDoc",
        ],
    ),
    # Student-designed STM32 mainboard, 14 sheets.
    "ubc-mainboard-v2": (
        "mainboard-v2.0/pcb",
        [
            "MainBoard.PrjPCB",
            "MainBoard_Layout_V2.PcbDoc",
            "MainBoard_MCU_Power.SchDoc",
            "MainBoard_Top.SchDoc",
            "Mainboard_Breakbeam_Interface.SchDoc",
            "Mainboard_Debug_Interface.SchDoc",
            "Mainboard_Encoder_Interface.SchDoc",
            "Mainboard_Geneva_Interface.SchDoc",
            "Mainboard_JTAG Connector.SchDoc",
            "Mainboard_MCU_Breakout.SchDoc",
            "Mainboard_MCU_Func.SchDoc",
            "Mainboard_Motordriver_Interface.SchDoc",
            "Mainboard_Power_Interface.SchDoc",
            "Mainboard_RMII_Interface.SchDoc",
            "Mainboard_ThermistorAmp.SchDoc",
            "Mainboard_User_Interface.SchDoc",
            "Mainboard_Wifi_Interface.SchDoc",
        ],
    ),
    # Student breakout board with multi-channel sheets and harness-typed ports.
    "ubc-bob-breakout": (
        "bob-breakout-v1.0/pcb",
        [
            "PCB_BobBreakout.PrjPcb",
            "PCB_BobBreakout.PrjPcbStructure",
            "bob_breakout-layout.PcbDoc",
            "bob.SchDoc",
            "bob_breakout-top.SchDoc",
            "bob_breakout.SchDoc",
            "encoder.SchDoc",
            "isolation.SchDoc",
            "jetson-nano.SchDoc",
            "low-pass-filter.SchDoc",
            "power.SchDoc",
            "ui.SchDoc",
            "bob.Harness",
            "encoder.Harness",
            "jetson-nano.Harness",
            "power.Harness",
            "ui.Harness",
        ],
    ),
}

PROJECTS = {
    "mb1364": "MB1364.PrjPcb",
    "ubc-mainboard-v2": "MainBoard.PrjPCB",
    "ubc-bob-breakout": "PCB_BobBreakout.PrjPcb",
}


def fetch(destination: Path) -> Path:
    """Download every board that isn't already complete. Returns the destination folder."""
    for board, (folder, files) in BOARDS.items():
        out = destination / board
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            target = out / name
            if target.exists() and target.stat().st_size > 0:
                continue
            url = f"{BASE}/{folder}/{urllib.parse.quote(name)}"
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
    return destination


if __name__ == "__main__":
    default = Path(__file__).resolve().parents[1] / "test-designs"
    print(f"Downloaded to {fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else default)}")
