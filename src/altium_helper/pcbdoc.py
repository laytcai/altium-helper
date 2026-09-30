"""Read which pad connects to which net from an Altium .PcbDoc file.

A .PcbDoc is an OLE compound file. Three of its streams matter here:
``Nets6/Data`` and ``Components6/Data`` hold ``|KEY=VALUE|`` text records, and
``Pads6/Data`` holds binary pad records that point at a net and a component by index.
"""

from __future__ import annotations

import collections
import struct
from dataclasses import dataclass
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


def _physical_designators(
    components: list[dict[str, str]], naming: ChannelNaming
) -> list[str]:
    """Give each component the designator printed on the board.

    The file stores the designator from the schematic sheet (``SOURCEDESIGNATOR``).
    In a multi-channel design every copy of a sheet repeats it (``J4`` five times),
    and the board designator adds the channel's room name (``J4_U_bob_0``).
    """
    logical = [c.get("SOURCEDESIGNATOR", "?") for c in components]
    counts = collections.Counter(logical)
    names = []
    for component, designator in zip(components, logical):
        if counts[designator] == 1:
            names.append(designator)
            continue
        path = [p for p in component.get("SOURCEHIERARCHICALPATH", "").split("\\") if p]
        # The first element is the top sheet; the rest are the channel rooms.
        rooms = path[1:] if len(path) > 1 else path
        if not rooms:
            names.append(designator)
            continue
        room = (
            rooms[-1] if naming.room_style == 0 else naming.level_separator.join(rooms)
        )
        names.append(
            naming.format.replace("$Component", designator).replace("$RoomName", room)
        )
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
