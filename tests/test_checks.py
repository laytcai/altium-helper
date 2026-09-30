from altium_helper import checks

NETLIST = {
    "nets": {
        "SPI_MOSI": {"U1": ["10"], "U2": ["3"]},
        "SPI_MISO": {"U1": ["11"], "U2": ["4"]},
        "LED": {"U1": ["12"], "R1": ["1"]},
        "ONLY_IN_SCHEMATIC": {"U9": ["1"], "U1": ["20"]},  # U9 isn't on the PCB
    }
}


def test_identical_nets_match():
    pcb = {
        "SPI_MOSI": {("U1", "10"), ("U2", "3")},
        "SPI_MISO": {("U1", "11"), ("U2", "4")},
        "NET_LED": {("U1", "12"), ("R1", "1")},  # same pads, different name
    }
    result = checks.compare_with_pcb(NETLIST, pcb)
    assert result["pcb_nets"] == 3
    assert result["matching"] == 3
    assert result["same_names"] == 2
    assert result["differences"] == []


def test_swapped_wiring_is_reported():
    pcb = {
        "SPI_MOSI": {("U1", "10"), ("U2", "4")},
        "SPI_MISO": {("U1", "11"), ("U2", "3")},
    }
    result = checks.compare_with_pcb(NETLIST, pcb)
    assert result["matching"] == 0
    assert {d["pcb_net"] for d in result["differences"]} == {"SPI_MOSI", "SPI_MISO"}
    mosi = next(d for d in result["differences"] if d["pcb_net"] == "SPI_MOSI")
    assert mosi["schematic_nets"] == {"SPI_MOSI": 1, "SPI_MISO": 1}


def test_project_documents_and_case_insensitive_lookup(tmp_path):
    (tmp_path / "Sheets").mkdir()
    (tmp_path / "Sheets" / "power.schdoc").write_bytes(b"")
    project = tmp_path / "Board.PrjPcb"
    project.write_text(
        "[Design]\nChannelDesignatorFormatString=$Component$RoomName\nChannelRoomNamingStyle=1\n"
        "ChannelRoomLevelSeperator=.\n"
        "[Document1]\nDocumentPath=Sheets\\Power.SchDoc\n"
        "[Document2]\nDocumentPath=Board.PcbDoc\n",
        encoding="utf-8",
    )
    assert checks.project_documents(project) == ["Sheets/Power.SchDoc", "Board.PcbDoc"]
    assert (
        checks.resolve_document(tmp_path, "Sheets/Power.SchDoc")
        == tmp_path / "Sheets" / "power.schdoc"
    )
    assert checks.resolve_document(tmp_path, "Board.PcbDoc") is None
    naming = checks.channel_naming(project)
    assert (naming.format, naming.room_style, naming.level_separator) == (
        "$Component$RoomName",
        1,
        ".",
    )
    report = checks.check_project(project)
    assert report["missing_documents"] == ["Board.PcbDoc"]
    assert report["pcb"] == []
