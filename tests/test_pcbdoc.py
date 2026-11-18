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


def test_channel_alpha_numbers_channels_in_room_order():
    components = [
        {"SOURCEDESIGNATOR": "C32", "SOURCEHIERARCHICALPATH": "DASH\\U_CAN_PRIORITY"},
        {"SOURCEDESIGNATOR": "C32", "SOURCEHIERARCHICALPATH": "DASH\\U_CAN_GP"},
    ]
    naming = pcbdoc.ChannelNaming(format="$Component$ChannelAlpha")
    assert pcbdoc._physical_designators(components, naming) == ["C32B", "C32A"]


def test_annotation_names_channels_that_share_a_path():
    # Three sheet symbols all named "V": one path, told apart only by the annotation.
    components = [
        {
            "SOURCEDESIGNATOR": "U9",
            "SOURCEHIERARCHICALPATH": "PCM\\V",
            "SOURCEUNIQUEID": f"\\S{i}\\P",
        }
        for i in (1, 2, 3)
    ]
    naming = pcbdoc.ChannelNaming(
        placements={
            "\\S1\\P": ("V3", "analog input channel.schdoc"),
            "\\S2\\P": ("V1", "analog input channel.schdoc"),
            "\\S3\\P": ("V2", "analog input channel.schdoc"),
        }
    )
    assert pcbdoc._physical_designators(components, naming) == [
        "U9_V3",
        "U9_V1",
        "U9_V2",
    ]


def test_a_designator_reused_on_different_sheets_takes_its_room():
    components = [
        {
            "SOURCEDESIGNATOR": "C10",
            "SOURCEHIERARCHICALPATH": "PCM\\Power",
            "SOURCEUNIQUEID": "\\A\\X",
        },
        {
            "SOURCEDESIGNATOR": "C10",
            "SOURCEHIERARCHICALPATH": "PCM\\U_Rev12 Debug",
            "SOURCEUNIQUEID": "\\B\\Y",
        },
    ]
    naming = pcbdoc.ChannelNaming(
        format="$Component_$ChannelAlpha",  # would give C10_A twice
        placements={
            "\\A\\X": ("Power", "power.schdoc"),
            "\\B\\Y": ("U_Rev12 Debug", "sch-0000-7.schdoc"),
        },
    )
    assert pcbdoc._physical_designators(components, naming) == [
        "C10_Power",
        "C10_U_Rev12 Debug",
    ]
