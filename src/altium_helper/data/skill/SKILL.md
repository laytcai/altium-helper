---
name: altium-boards
description: Answer questions about Penn Electric Racing's circuit boards (schematic connectivity, pins, nets, parts, and what changed between revisions) by reading the Altium 365 designs with the altium-helper and universal-netlist MCP tools. Use for hardware questions, for checking firmware pin assignments against a board, and for "did the team change X" questions.
---

# Reading PER's boards

The boards live in Altium 365. The altium-helper tools fetch them by themselves, read-only.
Never ask the user to download files, and never try to change anything in Altium 365.

## Which tool
- **Find a board:** `list_boards`. Board names match loosely: "daq" finds "DAQ Board".
- **Connectivity** ("where does GPIO20 go?", "what's on CAN_MISO?"):
  1. Call `get_board(board)`.
  2. Pass the returned `design` path to universal-netlist:
     - `query_component`: all pins of a part, with pin names and nets;
     - `query_xnet_by_net_name`: everything on a net, traced through passives;
     - `search_nets` and `search_components_by_*`: find things.
- **History** ("did they change the flipped pins yesterday?"):
  - `board_changes(board, since="yesterday")` explains each commit's connectivity change: pins moved or
    swapped, nets renamed, and parts added, removed or changed. It also reports whether the PCB file changed and
    still matches the schematic.
  - `board_history` lists commits.
- **A past revision:** `get_board(board, revision=<commit>)` returns a netlist file for that revision.
  universal-netlist reads it like any design.
- **Trust check:** `check_board` compares the schematic with the PCB file. A difference means the PCB wasn't
  updated from the schematic, or the reader missed a connection.

## universal-netlist quirks
- `query_xnet_by_pin_name` wants `REFDES.PINNUMBER` (`U3.12`). Look the number up with `query_component` first.
- Traces pass through resistors, capacitors, inductors and ferrites only. They stop at solder bridges (SB) and
  jumpers (JP): note the part and continue from its other pin.
- If a design has variants, every call needs `design_variant`. `list_designs` shows them.
- MCU pin names can carry alternate functions, e.g. `PC14-OSC32_IN`. Match on the part before `-` or `/`.

## Checking firmware pins against a board
Firmware lives in the Penn-Electric-Racing monorepo:
- STM32 boards: `embedded/boards/<board>/*Pins.hpp`, with lines like `const Pin framMosi = PC12;`.
- Ludwig (Raspberry Pi CM4): `embedded/boards/ludwig/configs/firmware/config.txt` for the SPI, chip-select and
  interrupt GPIOs, and `LudwigPins.hpp`.

Recipe:
1. Read the pin file.
2. Call `get_board` for the matching board and find the MCU (e.g. `search_components_by_description` "STM32").
3. Call `query_component` on the MCU to map pin names to pin numbers and nets.
4. For each firmware pin, trace its net with `query_xnet_by_net_name` and look at the far-end pins.
5. Flag:
   - pins that aren't connected;
   - direction errors:
     - MOSI must reach SDI, SI, DIN or MOSI;
     - MISO must reach SDO, SO, DOUT or MISO;
     - UART TX must reach RX;
     - for CAN, MCU TX goes to the transceiver's TXD;
   - firmware names that disagree with the net name;
   - two firmware names on one pin.
6. Answer with a table: firmware name | MCU pin | net | far end | verdict.

Known mismatches:
- `BMSPins.hpp` `canRx`/`canTx` are commented "Swapped compared to schematic".
- `LudwigPins.hpp` `encA`/`encB` are commented "NOT ON SCHEMATIC".

## Answering
- Say which board revision you used: commit, author and date.
- Everything read from a design goes to the AI provider. Some parts may be under sponsor NDA, so don't paste
  whole netlists into answers.
