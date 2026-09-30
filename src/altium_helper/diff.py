"""What changed between two netlists: pins that moved, nets renamed, parts added or changed.

Works on Universal Netlist JSON (``{"nets": {net: {ref: [pin]}}, "components": {...}}``).
"""

from __future__ import annotations

import collections
import re

Pin = tuple[str, str]  # (designator, pin number)

# Names Altium makes up for unnamed nets (e.g. NetU1_5); their appearing or vanishing is noise.
AUTO_NET = re.compile(r"^Net[A-Za-z0-9_]*\d$")


def _pin_nets(netlist: dict) -> dict[Pin, str]:
    return {
        (ref, str(pin)): net
        for net, parts in netlist.get("nets", {}).items()
        for ref, pins in parts.items()
        for pin in pins
    }


def _net_pins(netlist: dict) -> dict[str, frozenset[Pin]]:
    return {
        net: frozenset((ref, str(pin)) for ref, pins in parts.items() for pin in pins)
        for net, parts in netlist.get("nets", {}).items()
    }


def _pin_names(netlist: dict) -> dict[Pin, str]:
    names = {}
    for ref, component in netlist.get("components", {}).items():
        for number, entry in component.get("pins", {}).items():
            if isinstance(entry, dict) and entry.get("name"):
                names[(ref, str(number))] = entry["name"]
    return names


def _match_nets(
    old: dict[str, frozenset[Pin]], new: dict[str, frozenset[Pin]]
) -> dict[str, str]:
    """Pair each old net with the net it became.

    A net that keeps its name is the same net, however its pins changed. Among the
    rest, an old net whose pins mostly (more than half) ended up in one new net was
    renamed to it.
    """
    matched = {net: net for net in old if net in new}
    unmatched_new = {net: pins for net, pins in new.items() if net not in old}
    new_net_of = {pin: net for net, pins in unmatched_new.items() for pin in pins}
    candidates: dict[str, tuple[str, int]] = {}
    for net, pins in old.items():
        if net in matched:
            continue
        counts = collections.Counter(new_net_of[p] for p in pins if p in new_net_of)
        if not counts:
            continue
        best, overlap = max(counts.items(), key=lambda item: (item[1], item[0]))
        if overlap * 2 > len(pins):
            candidates[net] = (best, overlap)
    # A new net can continue only one old net: keep the strongest claim.
    claims: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for old_net, (new_net, overlap) in candidates.items():
        claims[new_net].append((overlap, old_net))
    matched.update({max(claim)[1]: new_net for new_net, claim in claims.items()})
    return matched


def _net_cycles(moved: list[dict], matched: dict[str, str]) -> list[dict]:
    """Pins of one part that passed their nets around in a circle.

    Two pins that traded nets are the classic swapped TX/RX or MOSI/MISO; three or more
    form a rotation. In each result, ``pins[i]`` had ``nets[i]`` and now has the net
    ``pins[i + 1]`` had.
    """
    by_ref = collections.defaultdict(list)
    for change in moved:
        by_ref[change["ref"]].append(change)
    cycles = []
    for changes in by_ref.values():
        holders = collections.defaultdict(list)
        for change in changes:
            holders[matched.get(change["from"], change["from"])].append(change)
        # The pin whose old net this pin now has, when exactly one pin had it.
        takes_from = {
            c["pin"]: holders[c["to"]][0] for c in changes if len(holders[c["to"]]) == 1
        }
        in_cycle: set[str] = set()
        for start in changes:
            if start["pin"] in in_cycle:
                continue
            chain, current = [start], takes_from.get(start["pin"])
            while current is not None and current is not start:
                if current["pin"] in in_cycle or current in chain:
                    current = None
                    break
                chain.append(current)
                current = takes_from.get(current["pin"])
            if current is start and len(chain) >= 2:
                in_cycle.update(c["pin"] for c in chain)
                cycles.append(
                    {
                        "pins": [c["pin"] for c in chain],
                        "nets": [c["from"] for c in chain],
                    }
                )
    return cycles


def _describe_pin(pin: Pin, names: dict[Pin, str]) -> str:
    ref, number = pin
    name = names.get(pin)
    return f"{ref}.{number} ({name})" if name and name != number else f"{ref}.{number}"


def diff_netlists(old: dict, new: dict) -> dict:
    """Compare two netlists. All lists are sorted, so equal inputs give equal outputs."""
    old_components, new_components = old.get("components", {}), new.get(
        "components", {}
    )
    old_pins, new_pins = _pin_nets(old), _pin_nets(new)
    old_nets, new_nets = _net_pins(old), _net_pins(new)
    names = {**_pin_names(old), **_pin_names(new)}
    matched = _match_nets(old_nets, new_nets)
    matched_new = set(matched.values())

    renamed = sorted(
        (old_net, new_net) for old_net, new_net in matched.items() if old_net != new_net
    )
    moved = []
    for pin in sorted(set(old_pins) & set(new_pins)):
        before, after = old_pins[pin], new_pins[pin]
        if matched.get(before, before) != after:
            moved.append(
                {
                    "pin": _describe_pin(pin, names),
                    "ref": pin[0],
                    "from": before,
                    "to": after,
                }
            )

    cycles = _net_cycles(moved, matched)
    swaps = [c for c in cycles if len(c["pins"]) == 2]
    rotations = [c for c in cycles if len(c["pins"]) > 2]

    def interesting(net: str, pins: frozenset[Pin]) -> bool:
        return len(pins) > 1 or not AUTO_NET.match(net)

    added_nets = sorted(
        net
        for net, pins in new_nets.items()
        if net not in matched_new and interesting(net, pins)
    )
    removed_nets = sorted(
        net
        for net, pins in old_nets.items()
        if net not in matched and interesting(net, pins)
    )

    changed_parts = []
    for ref in sorted(set(old_components) & set(new_components)):
        before = {k: v for k, v in old_components[ref].items() if k != "pins"}
        after = {k: v for k, v in new_components[ref].items() if k != "pins"}
        fields = sorted(set(before) | set(after))
        differences = {
            f: [before.get(f), after.get(f)]
            for f in fields
            if before.get(f) != after.get(f)
        }
        pins_before = {str(p) for p in old_components[ref].get("pins", {})}
        pins_after = {str(p) for p in new_components[ref].get("pins", {})}
        if pins_before != pins_after:
            differences["pins"] = [
                sorted(pins_before - pins_after),
                sorted(pins_after - pins_before),
            ]
        if differences:
            changed_parts.append({"ref": ref, "changes": differences})

    return {
        "moved_pins": moved,
        "swapped_pins": swaps,
        "rotated_pins": rotations,
        "renamed_nets": [{"from": a, "to": b} for a, b in renamed],
        "added_nets": added_nets,
        "removed_nets": removed_nets,
        "added_parts": sorted(set(new_components) - set(old_components)),
        "removed_parts": sorted(set(old_components) - set(new_components)),
        "changed_parts": changed_parts,
    }


def is_empty(changes: dict) -> bool:
    return not any(changes.values())


def summarize(changes: dict, new: dict | None = None, limit: int = 40) -> list[str]:
    """One human-readable line per change, most important first."""
    lines = []
    for swap in changes["swapped_pins"]:
        lines.append(
            f"Swapped: {swap['pins'][0]} and {swap['pins'][1]} traded nets {swap['nets'][0]} <-> {swap['nets'][1]}"
        )
    for rotation in changes.get("rotated_pins", []):
        pins, nets = rotation["pins"], rotation["nets"]
        steps = ", ".join(
            f"{pin} {nets[i]} -> {nets[(i + 1) % len(pins)]}"
            for i, pin in enumerate(pins)
        )
        lines.append(f"Rotated: {len(pins)} pins passed their nets around: {steps}")
    in_cycles = {
        pin
        for cycle in changes["swapped_pins"] + changes.get("rotated_pins", [])
        for pin in cycle["pins"]
    }
    for move in changes["moved_pins"]:
        if move["pin"] not in in_cycles:
            lines.append(f"Moved: {move['pin']} from {move['from']} to {move['to']}")
    for rename in changes["renamed_nets"]:
        lines.append(f"Net renamed: {rename['from']} -> {rename['to']}")
    new_components = (new or {}).get("components", {})
    for ref in changes["added_parts"]:
        component = new_components.get(ref, {})
        detail = (
            component.get("mpn")
            or component.get("value")
            or component.get("description")
            or ""
        )
        lines.append(f"Part added: {ref}" + (f" ({detail})" if detail else ""))
    for ref in changes["removed_parts"]:
        lines.append(f"Part removed: {ref}")
    for part in changes["changed_parts"]:
        for field, (before, after) in part["changes"].items():
            if field == "pins":
                lines.append(
                    f"Part changed: {part['ref']} pins removed {before or '-'}, added {after or '-'}"
                )
            else:
                lines.append(
                    f"Part changed: {part['ref']} {field}: {before!r} -> {after!r}"
                )
    for net in changes["added_nets"]:
        lines.append(f"Net added: {net}")
    for net in changes["removed_nets"]:
        lines.append(f"Net removed: {net}")
    if len(lines) > limit:
        lines = lines[:limit] + [f"... and {len(lines) - limit} more changes"]
    return lines
