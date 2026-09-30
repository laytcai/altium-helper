"""Compare the prototype's schematic netlist with the .PcbDoc's pad->net data.

Usage: python3 validate.py Board.PrjPcb Board.PcbDoc
Only meaningful when the PCB is in sync with the schematic.
"""
import sys, collections
from schnet import build
from pcbnet import pcb_netlist
prj, pcbdoc = sys.argv[1], sys.argv[2]
sheets, sch = build(prj)
pcb = pcb_netlist(pcbdoc)
print(f"{len(sheets)} sheets, {sum(len(s.pins) for s in sheets.values())} placed pins")
pcb_pins = {p for v in pcb.values() for p in v}
sch_sets = {}
for n in sch:
    s = frozenset((p['des'], p['num']) for p in n['pins']) & pcb_pins   # ignore parts not on the PCB
    if len(s) >= 2: sch_sets[s] = n['name']
pcb_sets = {frozenset(v): k for k, v in pcb.items() if len(v) >= 2}
exact = set(sch_sets) & set(pcb_sets)
print(f"PCB nets (>=2 pads): {len(pcb_sets)}  schematic nets: {len(sch_sets)}  identical: {len(exact)} ({100*len(exact)/max(1,len(pcb_sets)):.1f}% of PCB nets)")
# classify mismatches: which pins are split/merged
pin2sch = {p: s for s in sch_sets for p in s}
bad = [s for s in pcb_sets if s not in exact]
for s in bad[:6]:
    parts = collections.Counter(sch_sets.get(pin2sch.get(p), '(unconnected in sch)') for p in s)
    print(f"  PCB net {pcb_sets[s]!r} ({len(s)} pads) is split across schematic nets: {dict(parts)}")
