"""Checks on an Altium project: are all its documents present, and does the schematic match the PCB."""

from __future__ import annotations

import collections
import configparser
from pathlib import Path

from . import netlist as netlist_tool
from . import pcbdoc


def read_project(prjpcb: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep Altium's key case
    parser.read_string(prjpcb.read_bytes().decode("utf-8-sig", errors="replace"))
    return parser


def project_documents(prjpcb: Path) -> list[str]:
    """Document paths listed in a .PrjPcb, relative to the project folder, with forward slashes."""
    parser = read_project(prjpcb)
    paths = []
    for section in parser.sections():
        if section.startswith("Document") and parser[section].get("DocumentPath"):
            paths.append(parser[section]["DocumentPath"].replace("\\", "/"))
    return paths


def resolve_document(root: Path, relative: str) -> Path | None:
    """Find a project document, ignoring letter case (projects made on Windows often differ)."""
    exact = root / relative
    if exact.exists():
        return exact
    current = root
    for part in Path(relative).parts:
        matches = (
            [p for p in current.iterdir() if p.name.lower() == part.lower()]
            if current.is_dir()
            else []
        )
        if not matches:
            return None
        current = matches[0]
    return current


def channel_naming(prjpcb: Path) -> pcbdoc.ChannelNaming:
    parser = read_project(prjpcb)
    design = parser["Design"] if parser.has_section("Design") else {}
    style = design.get("ChannelRoomNamingStyle") or "0"
    return pcbdoc.ChannelNaming(
        format=design.get("ChannelDesignatorFormatString") or "$Component_$RoomName",
        room_style=int(style) if style.isdigit() else 0,
        level_separator=design.get("ChannelRoomLevelSeperator") or "_",
    )


def compare_with_pcb(
    netlist: dict, pcb: dict[str, set[tuple[str, str]]], limit: int = 20
) -> dict:
    """Compare schematic nets with PCB nets that have two or more pads.

    Pins of parts that aren't on the PCB are ignored, so a part left off the board
    doesn't count as a difference.
    """
    pcb_pins = set().union(*pcb.values()) if pcb else set()
    schematic: dict[frozenset, str] = {}
    for net, parts in netlist.get("nets", {}).items():
        pins = (
            frozenset(
                (ref, str(pin)) for ref, part_pins in parts.items() for pin in part_pins
            )
            & pcb_pins
        )
        if len(pins) >= 2:
            schematic[pins] = net
    board = {frozenset(pads): name for name, pads in pcb.items() if len(pads) >= 2}
    same = set(schematic) & set(board)
    net_of_pin = {pin: schematic[pins] for pins in schematic for pin in pins}
    differences = []
    for pins, name in sorted(board.items(), key=lambda item: item[1]):
        if pins in same:
            continue
        where = collections.Counter(
            net_of_pin.get(p, "(not connected in the schematic)") for p in pins
        )
        differences.append(
            {"pcb_net": name, "pads": len(pins), "schematic_nets": dict(where)}
        )
    return {
        "pcb_nets": len(board),
        "schematic_nets": len(schematic),
        "matching": len(same),
        "same_names": sum(1 for pins in same if schematic[pins] == board[pins]),
        "differences": differences[:limit],
        "more_differences": max(0, len(differences) - limit),
    }


DESIGN_DOCUMENTS = (
    ".schdoc",
    ".pcbdoc",
    ".harness",
)  # the ones that carry connectivity


def check_project(prjpcb: str | Path, netlist: dict | None = None) -> dict:
    """Report missing design documents and how well the schematic matches each .PcbDoc."""
    prjpcb = Path(prjpcb)
    documents = [
        d for d in project_documents(prjpcb) if d.lower().endswith(DESIGN_DOCUMENTS)
    ]
    missing = [d for d in documents if resolve_document(prjpcb.parent, d) is None]
    report: dict = {
        "project": prjpcb.name,
        "documents": len(documents),
        "missing_documents": missing,
        "pcb": [],
    }
    pcb_documents = [
        d for d in documents if d.lower().endswith(".pcbdoc") and d not in missing
    ]
    if not pcb_documents:
        return report
    if netlist is None:
        netlist = netlist_tool.read_netlist(prjpcb)
    naming = channel_naming(prjpcb)
    for document in pcb_documents:
        path = resolve_document(prjpcb.parent, document)
        try:
            pads = pcbdoc.pad_nets(path, naming)
        except pcbdoc.PcbDocError as e:
            report["pcb"].append({"document": document, "error": str(e)})
            continue
        report["pcb"].append({"document": document, **compare_with_pcb(netlist, pads)})
    return report


def summarize(report: dict) -> str:
    lines = [f"{report['project']}: {report['documents']} design documents"]
    if report["missing_documents"]:
        lines.append(f"  missing: {', '.join(report['missing_documents'])}")
    if not report["pcb"]:
        lines.append("  no PCB document to compare with")
    for pcb in report["pcb"]:
        if "error" in pcb:
            lines.append(f"  {pcb['document']}: {pcb['error']}")
            continue
        lines.append(
            f"  {pcb['document']}: {pcb['matching']}/{pcb['pcb_nets']} PCB nets match the schematic"
            f" ({pcb['same_names']} with the same name)"
        )
        for difference in pcb["differences"]:
            nets = ", ".join(
                f"{net} ({count})"
                for net, count in difference["schematic_nets"].items()
            )
            lines.append(
                f"    PCB net {difference['pcb_net']} ({difference['pads']} pads) is in schematic: {nets}"
            )
        if pcb["more_differences"]:
            lines.append(f"    ... and {pcb['more_differences']} more")
    return "\n".join(lines)
