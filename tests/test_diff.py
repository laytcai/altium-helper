from altium_helper import diff


def netlist(components: dict) -> dict:
    nets: dict = {}
    for ref, component in components.items():
        for number, entry in component["pins"].items():
            net = entry["net"] if isinstance(entry, dict) else entry
            nets.setdefault(net, {}).setdefault(ref, []).append(number)
    return {"nets": nets, "components": components}


def mcu(tx: str, rx: str) -> dict:
    return {
        "pins": {
            "12": {"name": "PB12", "net": tx},
            "13": {"name": "PB13", "net": rx},
            "14": {"name": "PB14", "net": "LED"},
        }
    }


BASE = {
    "U3": mcu("CAN_TX", "CAN_RX"),
    "U4": {"pins": {"1": "CAN_TX", "4": "CAN_RX"}, "mpn": "TCAN1051"},
    "R1": {"pins": {"1": "LED", "2": "GND"}, "value": "1k"},
}


def test_identical_netlists_have_no_changes():
    assert diff.is_empty(diff.diff_netlists(netlist(BASE), netlist(BASE)))


def test_swapped_pins_are_reported_as_a_swap():
    swapped = {**BASE, "U3": mcu("CAN_RX", "CAN_TX")}
    changes = diff.diff_netlists(netlist(BASE), netlist(swapped))
    assert changes["swapped_pins"] == [
        {"pins": ["U3.12 (PB12)", "U3.13 (PB13)"], "nets": ["CAN_TX", "CAN_RX"]}
    ]
    lines = diff.summarize(changes)
    assert lines == [
        "Swapped: U3.12 (PB12) and U3.13 (PB13) traded nets CAN_TX <-> CAN_RX"
    ]


def test_renamed_net_is_not_reported_as_moved_pins():
    renamed = {
        "U3": mcu("CAN1_TX", "CAN_RX"),
        "U4": {"pins": {"1": "CAN1_TX", "4": "CAN_RX"}, "mpn": "TCAN1051"},
        "R1": BASE["R1"],
    }
    changes = diff.diff_netlists(netlist(BASE), netlist(renamed))
    assert changes["renamed_nets"] == [{"from": "CAN_TX", "to": "CAN1_TX"}]
    assert changes["moved_pins"] == []
    assert changes["added_nets"] == [] and changes["removed_nets"] == []


def test_parts_added_removed_and_changed():
    new = {
        "U3": BASE["U3"],
        "U4": {"pins": {"1": "CAN_TX", "4": "CAN_RX"}, "mpn": "TCAN1044"},
        "R2": {"pins": {"1": "LED", "2": "GND"}, "value": "330"},
    }
    changes = diff.diff_netlists(netlist(BASE), netlist(new))
    assert changes["added_parts"] == ["R2"]
    assert changes["removed_parts"] == ["R1"]
    assert changes["changed_parts"] == [
        {"ref": "U4", "changes": {"mpn": ["TCAN1051", "TCAN1044"]}}
    ]
    assert "Part added: R2 (330)" in diff.summarize(changes, netlist(new))


def test_single_pin_nets_altium_named_are_not_noise():
    old = {"U3": mcu("CAN_TX", "CAN_RX"), "U4": BASE["U4"]}
    new = {
        "U3": mcu("CAN_TX", "NetU3_13"),
        "U4": {"pins": {"1": "CAN_TX", "4": "NetU4_4"}},
    }
    changes = diff.diff_netlists(netlist(old), netlist(new))
    assert changes["added_nets"] == []
    assert {m["pin"] for m in changes["moved_pins"]} == {"U3.13 (PB13)", "U4.4"}
