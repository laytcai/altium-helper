"""Write Universal Netlist files, the JSON format universal-netlist reads as a design.

Used for boards known only through Nexar, and by tests. The content hash must match what
universal-netlist computes in JavaScript: SHA-256 of ``JSON.stringify`` over nets and
components with object keys sorted. JavaScript lists integer-like keys (pin numbers such
as "2" before "10") first and in numeric order, whatever order they were added in.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
TEXT_FIELDS = ("mpn", "internal_pn", "manufacturer", "description", "comment", "value")
_ARRAY_INDEX = re.compile(r"(0|[1-9][0-9]*)\Z")


def _js_key_order(keys) -> list[str]:
    index = sorted(
        (k for k in keys if _ARRAY_INDEX.match(k) and int(k) < 2**32 - 1), key=int
    )
    taken = set(index)
    rest = sorted(
        (k for k in keys if k not in taken), key=lambda k: k.encode("utf-16-be")
    )
    return index + rest


def _canonical(value):
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in _js_key_order(value)}
    return value


def netlist_hash(nets: dict, components: dict) -> str:
    text = json.dumps(
        _canonical({"nets": nets, "components": components}),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(components: dict[str, dict]) -> dict:
    """Build a netlist from components alone.

    ``components`` maps a designator to ``{"pins": {number: net | {"name", "net"}}}`` plus
    any of TEXT_FIELDS and ``"dns": True``. Nets are derived from the pins, so the two
    always agree, as the format requires.
    """
    nets: dict[str, dict[str, list[str]]] = {}
    clean: dict[str, dict] = {}
    for ref, component in components.items():
        entry: dict = {f: str(component[f]) for f in TEXT_FIELDS if component.get(f)}
        if component.get("dns"):
            entry["dns"] = True
        pins = {}
        for number, value in component.get("pins", {}).items():
            number = str(number)
            net = value["net"] if isinstance(value, dict) else value
            name = value.get("name") if isinstance(value, dict) else None
            pins[number] = (
                {"name": name, "net": net} if name and name != number else net
            )
            nets.setdefault(net, {}).setdefault(ref, []).append(number)
        entry["pins"] = pins
        clean[ref] = entry
    generated = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return {
        "universalNetlistSchemaVersion": SCHEMA_VERSION,
        "metadata": {
            "generatedAt": generated,
            "netlistHash": netlist_hash(nets, clean),
            "origin": {"type": "native"},
        },
        "nets": nets,
        "components": clean,
    }


def write(path: str | Path, netlist: dict) -> Path:
    path = Path(path)
    if not path.name.endswith(".netlist.json"):
        raise ValueError(
            f"{path.name}: a Universal Netlist file must end in .netlist.json"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(netlist, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
