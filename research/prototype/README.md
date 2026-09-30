# Prototype scripts (throwaway)

These scripts were written on 2026-09-29/30 to answer one question: can Altium schematics be read accurately on
any OS without Altium Designer? They are a measuring stick, not the product. The plan is to use
[universal-netlist](https://github.com/IntelligentElectron/universal-netlist) for reading (see `../../docs/research.md`).
These scripts remain useful for checking a reader's output against the board's PCB file.

| File | What it does |
|---|---|
| `schdoc_dump.py` | Reads the `FileHeader` stream of a `.SchDoc` (OLE compound file) into `|KEY=VALUE|` record dicts. Run it on files to print record-type counts. |
| `schnet.py` | Builds a netlist from a `.PrjPcb` and its sheets. Handles wires, T-connections, junctions, net labels (sheet-local), power ports (global), ports to sheet entries, and bus ports/entries by member name. Extra args trace pins or nets: `python3 schnet.py Board.PrjPcb PB0 U14.46`. |
| `pcbnet.py` | Reads pad-to-net assignments from a `.PcbDoc` (`Nets6`, `Components6`, `Pads6` streams). |
| `validate.py` | Compares `schnet.py` output with `pcbnet.py` output net by net. |
| `fetch_test_designs.sh` | Downloads the public test designs into `test-designs/` (gitignored). |

`schnet.py` doesn't handle harnesses, multi-channel (repeated) sheets, off-sheet connectors, the Flat/Global
net-scope modes, hidden power pins, or Altium's exact net-naming priority.

## Running

```bash
./fetch_test_designs.sh
pip install --target ../../pylib olefile      # or into a venv
PYTHONPATH=../../pylib python3 validate.py ../../test-designs/mb1364/MB1364.PrjPcb ../../test-designs/mb1364/MB1364.PcbDoc
```

Results on 2026-09-30 (nets with 2 or more pads that match the PCB exactly):

| Board | Sheets | PCB nets | universal-netlist v1.12.0 | `schnet.py` |
|---|---|---|---|---|
| ST NUCLEO-144 (mb1364) | 8 | 287 | 287 (all names match too) | 287 |
| UBC mainboard v2 | 14 | 227 | 227 (all names match too) | 227 |
| UBC bob breakout | 8 | 53 | 20 | 9 |

The breakout board's PCB is stale: the schematic expands to 95 parts, the PCB has 33. That row doesn't measure
reader accuracy. Multi-channel and harness support is still untested.

To compare universal-netlist the same way, export with
`universal-netlist export-json Board.PrjPcb out.netlist.json`. Its `nets` map is `{net: {refdes: [pin, ...]}}`.
Then compare pin sets against `pcbnet.pcb_netlist()`.

## Rendering a sheet to an image

[python-altium](https://github.com/vadmium/python-altium) (WTFPL, last commit 2021) renders one `.SchDoc` to SVG.
On Python 3.10+, change `from collections import Iterable` to `from collections.abc import Iterable` in
`vector/base.py` and `vector/svg.py`. Then:

```bash
python3 altium.py Sheet.SchDoc > sheet.svg                     # needs olefile
python3 -c "import cairosvg; cairosvg.svg2png(url='sheet.svg', write_to='sheet.png', output_width=2400, background_color='white')"
```

The PNG was legible to Claude, including a 144-pin MCU sheet. Some pin-name text overlaps and title-block fields
come out raw.
