"""Read which pad connects to which net from an Altium .PcbDoc file.

A .PcbDoc is an OLE compound file. Three of its streams matter here:
``Nets6/Data`` and ``Components6/Data`` hold ``|KEY=VALUE|`` text records, and
``Pads6/Data`` holds binary pad records that point at a net and a component by index.
"""

from __future__ import annotations

import collections
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import olefile

NO_INDEX = 0xFFFF  # a pad with no net, or a free pad with no component


class PcbDocError(ValueError):
    """The file isn't a .PcbDoc this reader understands."""


@dataclass(frozen=True)
class ChannelNaming:
    """How a multi-channel design names each copy of a part.

    Read from the project's ``ChannelDesignatorFormatString``,
    ``ChannelRoomNamingStyle`` and ``ChannelRoomLevelSeperator`` options.
    """

    format: str = "$Component_$RoomName"
    room_style: int = 0
    level_separator: str = "_"
    # Where each part was placed, by SOURCEUNIQUEID ("\\SYMBOL\\PART"): (room, document),
    # from the project's .Annotation file. Sheet symbols that share a designator (three
    # symbols named "V") give their parts the same hierarchical path; only the annotation
    # says which is V1, V2 or V3, and which document a part came from.
    placements: dict[str, tuple[str, str]] = field(default_factory=dict, compare=False)


def _text_records(data: bytes) -> list[dict[str, str]]:
    records, i = [], 0
    while i + 4 <= len(data):
        (length,) = struct.unpack_from("<I", data, i)
        i += 4
        text = data[i : i + length].rstrip(b"\0").decode("latin-1")
        i += length
        record = {}
        for part in text.split("|"):
            if "=" in part:
                key, value = part.split("=", 1)
                record[key.upper()] = value
        records.append(record)
    return records


_FORMAT_TOKEN = re.compile(
    r"\$(ComponentPrefix|ComponentIndex|ChannelIndex|ChannelAlpha|Component|RoomName)"
)


def _natural(text: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _channel_designator(
    naming: ChannelNaming, designator: str, room: str, channel: int
) -> str:
    """Apply the project's channel designator format (``$Component$ChannelAlpha`` → ``C32B``)."""
    prefix = re.match(r"\D*", designator).group(0)
    tokens = {
        "ComponentPrefix": prefix,
        "ComponentIndex": designator[len(prefix) :],
        "ChannelIndex": str(channel),
        "ChannelAlpha": chr(64 + max(1, channel)),
        "Component": designator,
        "RoomName": room,
    }
    return _FORMAT_TOKEN.sub(lambda m: tokens[m.group(1)], naming.format)


def _physical_designators(
    components: list[dict[str, str]], naming: ChannelNaming
) -> list[str]:
    """Give each component the designator the schematic netlist uses for it.

    The file stores the designator from the schematic sheet (``SOURCEDESIGNATOR``).
    - A sheet placed several times repeats it in every channel (``J4`` five times): the
      project's channel designator format names each copy (``J4_U_bob_0``, ``C32A``),
      channels numbered in their rooms' natural order.
    - A designator reused on different sheets (a shared debug sheet's ``C10`` and the power
      sheet's) names a different part on each: ``<designator>_<room>`` (``C10_Power``).
    These match the names altium-helper's patched universal-netlist gives the same parts.
    """
    logical = [c.get("SOURCEDESIGNATOR", "?") for c in components]
    counts = collections.Counter(logical)
    where: list[tuple[str, str | None]] = []
    for component in components:
        placed = naming.placements.get(component.get("SOURCEUNIQUEID", ""))
        if placed:
            where.append(placed)
            continue
        path = [p for p in component.get("SOURCEHIERARCHICALPATH", "").split("\\") if p]
        # The first element is the top sheet; the rest are the channel rooms.
        rooms = path[1:] if len(path) > 1 else path
        room = (
            ""
            if not rooms
            else (
                rooms[-1]
                if naming.room_style == 0
                else naming.level_separator.join(rooms)
            )
        )
        where.append((room, None))
    # The rooms of each document (without an annotation, of each designator), in channel order.
    groups: dict[str, set[str]] = collections.defaultdict(set)
    for designator, (room, document) in zip(logical, where):
        groups[document or f"#{designator}"].add(room)
    ordered = {key: sorted(rooms, key=_natural) for key, rooms in groups.items()}
    names = []
    for designator, (room, document) in zip(logical, where):
        if counts[designator] == 1 or not room:
            names.append(designator)
            continue
        rooms = ordered[document or f"#{designator}"]
        if len(rooms) > 1:
            names.append(
                _channel_designator(naming, designator, room, rooms.index(room) + 1)
            )
        else:
            names.append(f"{designator}_{room}")
    return names


def pad_nets(
    path: str | Path, naming: ChannelNaming | None = None
) -> dict[str, set[tuple[str, str]]]:
    """Map each net name to the set of ``(designator, pad)`` pairs on it."""
    naming = naming or ChannelNaming()
    try:
        ole = olefile.OleFileIO(str(path))
    except OSError as e:
        raise PcbDocError(f"{path}: not an Altium PCB file ({e})") from e
    with ole:
        for stream in ("Nets6/Data", "Components6/Data", "Pads6/Data"):
            if not ole.exists(stream):
                raise PcbDocError(f"{path}: missing stream {stream}")
        nets = [
            r.get("NAME", "")
            for r in _text_records(ole.openstream("Nets6/Data").read())
        ]
        components = _physical_designators(
            _text_records(ole.openstream("Components6/Data").read()), naming
        )
        data = ole.openstream("Pads6/Data").read()

    by_net: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
    i = 0
    try:
        while i < len(data):
            record_type = data[i]
            i += 1
            if record_type != 2:
                raise PcbDocError(
                    f"{path}: unexpected pad record type {record_type} at byte {i - 1}"
                )
            (name_block,) = struct.unpack_from("<I", data, i)
            i += 4
            name_length = data[i]
            pad = data[i + 1 : i + 1 + name_length].decode("latin-1")
            i += name_block
            for _ in range(3):  # layer and shape blocks this reader doesn't need
                (block,) = struct.unpack_from("<I", data, i)
                i += 4 + block
            (geometry_block,) = struct.unpack_from("<I", data, i)
            i += 4
            net, component = struct.unpack_from("<H2xH", data, i + 3)
            i += geometry_block
            if i < len(data) and data[i] != 2:  # optional size-and-shape table
                (table,) = struct.unpack_from("<I", data, i)
                i += 4 + table
            if component != NO_INDEX and net != NO_INDEX:
                by_net[nets[net]].add((components[component], pad))
    except (struct.error, IndexError) as e:
        raise PcbDocError(f"{path}: pad data ends early or is malformed ({e})") from e
    return dict(by_net)
