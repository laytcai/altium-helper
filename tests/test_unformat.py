import json

import pytest

from altium_helper import unformat


def test_hash_orders_pin_numbers_like_javascript():
    # JavaScript lists integer-like keys numerically: "2" before "10".
    assert unformat._js_key_order(["10", "2", "B", "A", "01"]) == [
        "2",
        "10",
        "01",
        "A",
        "B",
    ]


def test_build_derives_nets_from_pins():
    doc = unformat.build(
        {
            "U1": {
                "pins": {"1": {"name": "VCC", "net": "3V3"}, "2": "GND"},
                "mpn": "X",
            },
            "C1": {
                "pins": {"1": "3V3", "2": "GND"},
                "value": "100n",
                "unknown": "dropped",
            },
        }
    )
    assert doc["nets"] == {
        "3V3": {"U1": ["1"], "C1": ["1"]},
        "GND": {"U1": ["2"], "C1": ["2"]},
    }
    assert doc["components"]["C1"] == {
        "value": "100n",
        "pins": {"1": "3V3", "2": "GND"},
    }
    assert doc["metadata"]["netlistHash"] == unformat.netlist_hash(
        doc["nets"], doc["components"]
    )


@pytest.mark.network
def test_universal_netlist_accepts_our_files(tmp_path, universal_netlist):
    components = {
        f"R{i}": {"pins": {"1": f"N{i}", "2": "GND"}, "value": "1k"}
        for i in range(1, 12)
    }
    written = unformat.write(
        tmp_path / "board.netlist.json", unformat.build(components)
    )
    exported = universal_netlist.export_json(written, tmp_path / "again.netlist.json")
    again = json.loads(exported.read_text(encoding="utf-8"))
    assert again["nets"]["GND"] == {f"R{i}": ["2"] for i in range(1, 12)}
