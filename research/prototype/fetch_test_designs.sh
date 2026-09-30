#!/usr/bin/env bash
# Download the public Altium designs used to test schematic readers into test-designs/ (gitignored).
# Source: https://github.com/UBC-Thunderbots/PCB_MainBoard. Downloaded for local testing only; don't commit them.
set -euo pipefail

dest="${1:-$(cd "$(dirname "$0")/../.." && pwd)/test-designs}"
base="https://raw.githubusercontent.com/UBC-Thunderbots/PCB_MainBoard/HEAD"

fetch() {  # fetch <path-in-repo> <local-dir> <files...>
    local src="$1" out="$dest/$2"
    shift 2
    mkdir -p "$out"
    for f in "$@"; do
        curl -fsSL -o "$out/$f" "$base/$src/${f// /%20}"
    done
}

# ST NUCLEO-144 board (STM32H7A3), 8 sheets, hierarchical with buses. PCB is in sync.
fetch _archive/mb1364/pcb mb1364 \
    MB1364.PrjPcb MB1364.PcbDoc MB1364.OutJob MB1364_Top.SchDoc MCU_144_IOs.SchDoc \
    MCU_144_POWER.SchDoc Power_Board.SchDoc ST_LINK_V3E.SchDoc USB.SchDoc Ethernet.SchDoc Connectors.SchDoc

# Student-designed STM32 mainboard, 14 sheets in the project. PCB is in sync.
fetch mainboard-v2.0/pcb ubc-mainboard-v2 \
    MainBoard.PrjPCB MainBoard_Layout_V2.PcbDoc MainBoard_MCU_Power.SchDoc MainBoard_Top.SchDoc \
    Mainboard_Breakbeam_Interface.SchDoc Mainboard_Debug_Interface.SchDoc \
    Mainboard_Encoder_Interface.SchDoc Mainboard_Geneva_Interface.SchDoc \
    "Mainboard_JTAG Connector.SchDoc" Mainboard_MCU_Breakout.SchDoc Mainboard_MCU_Func.SchDoc \
    Mainboard_Motordriver_Interface.SchDoc Mainboard_Power_Interface.SchDoc \
    Mainboard_RMII_Interface.SchDoc Mainboard_ThermistorAmp.SchDoc \
    Mainboard_User_Interface.SchDoc Mainboard_Wifi_Interface.SchDoc

# Student breakout board using multi-channel (repeated) sheets and harnesses.
# Its PCB is OUT OF SYNC with the schematic (33 parts vs 95), so it can't validate connectivity.
fetch bob-breakout-v1.0/pcb ubc-bob-breakout \
    PCB_BobBreakout.PrjPcb PCB_BobBreakout.PrjPcbStructure bob_breakout-layout.PcbDoc \
    bob.SchDoc bob_breakout-top.SchDoc bob_breakout.SchDoc encoder.SchDoc isolation.SchDoc \
    jetson-nano.SchDoc low-pass-filter.SchDoc power.SchDoc ui.SchDoc \
    bob.Harness encoder.Harness jetson-nano.Harness power.Harness ui.Harness

echo "Downloaded to $dest"
