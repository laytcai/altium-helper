import struct

from altium_helper import pcbdoc


def _record(text: str) -> bytes:
    body = text.encode("latin-1") + b"\0"
    return struct.pack("<I", len(body)) + body


def test_text_records_parse_keys_case_insensitively():
    data = _record("|name=GND|Kind=0|") + _record("|NAME=VCC|")
    assert pcbdoc._text_records(data) == [{"NAME": "GND", "KIND": "0"}, {"NAME": "VCC"}]


def test_physical_designators_add_the_channel_room():
    components = [
        {"SOURCEDESIGNATOR": "U1", "SOURCEHIERARCHICALPATH": "Top"},
        {"SOURCEDESIGNATOR": "J4", "SOURCEHIERARCHICALPATH": "Top\\U_bob_0"},
        {"SOURCEDESIGNATOR": "J4", "SOURCEHIERARCHICALPATH": "Top\\U_bob_1"},
        {"SOURCEDESIGNATOR": "C8", "SOURCEHIERARCHICALPATH": "Top\\U_jetson\\U_iso_0"},
        {"SOURCEDESIGNATOR": "C8", "SOURCEHIERARCHICALPATH": "Top\\U_jetson\\U_iso_1"},
    ]
    names = pcbdoc._physical_designators(components, pcbdoc.ChannelNaming())
    assert names == ["U1", "J4_U_bob_0", "J4_U_bob_1", "C8_U_iso_0", "C8_U_iso_1"]


def test_hierarchical_room_naming_joins_every_level():
    components = [
        {"SOURCEDESIGNATOR": "C8", "SOURCEHIERARCHICALPATH": "Top\\U_jetson\\U_iso_0"},
        {"SOURCEDESIGNATOR": "C8", "SOURCEHIERARCHICALPATH": "Top\\U_jetson\\U_iso_1"},
    ]
    naming = pcbdoc.ChannelNaming(
        format="$Component_$RoomName", room_style=1, level_separator="."
    )
    assert pcbdoc._physical_designators(components, naming) == [
        "C8_U_jetson.U_iso_0",
        "C8_U_jetson.U_iso_1",
    ]


def test_non_pcb_file_is_rejected(tmp_path):
    bogus = tmp_path / "Board.PcbDoc"
    bogus.write_bytes(b"not an OLE file")
    try:
        pcbdoc.pad_nets(bogus)
    except pcbdoc.PcbDocError as e:
        assert "Board.PcbDoc" in str(e)
    else:
        raise AssertionError("expected PcbDocError")
