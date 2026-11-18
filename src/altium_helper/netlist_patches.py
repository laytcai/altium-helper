"""Fixes applied to the pinned universal-netlist after ``npm ci``.

universal-netlist 1.12.0 (and 1.15.0, checked 2026-11-14) gets two things wrong on PER boards:

1. **Sheet symbols that name their file without the extension.** Altium writes ``Power`` (not
   ``Power.SchDoc``) in a sheet symbol's file-name record. universal-netlist matches placements
   against ``power.schdoc``, finds none, and treats every sheet as a top-level sheet: sheets
   placed several times (CAN ×3, analog channels ×3) are never expanded into channels, and
   their ports aren't linked to the parent. On REV12 PCM that hid 69 parts and showed the
   analog buffers' outputs as unconnected.
2. **Designators reused across separately placed sheets.** Shared/managed sheets (the debug
   probe, a copied power sheet) reuse ``C1…``, ``J1``, ``U3``. Altium tells them apart by board
   annotation; universal-netlist merges same-named parts, so one ``C10`` keeps one sheet's
   connections and loses the other's. The patch renames a reused designator per sheet instance
   as ``<designator>_<sheet symbol>`` (``C10_Power``, ``C10_U_Rev12 Debug``), the names
   :mod:`altium_helper.pcbdoc` gives the same parts.

Each patch names the exact text it replaces. If an upgrade changes that text, :func:`apply`
refuses rather than guessing, so a version bump must revisit these (and drop the ones the new
version fixes).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Patch:
    file: str  # relative to the package's dist/parsers/altium folder
    old: str
    new: str


SHEET_FILE_EXTENSION = Patch(
    file="sheet-hierarchy.js",
    old='    return fileName ? path.basename(fileName.replace(/\\\\/g, "/")).toLowerCase() : undefined;\n',
    new=(
        "    // altium-helper patch: a sheet symbol may name its file without the extension.\n"
        "    if (!fileName)\n"
        "        return undefined;\n"
        '    const base = path.basename(fileName.replace(/\\\\/g, "/")).toLowerCase();\n'
        '    return base.endsWith(".schdoc") ? base : `${base}.schdoc`;\n'
    ),
)

REUSED_DESIGNATORS_IMPORTS = [
    Patch(
        file="project.js",
        old='import { nameRanks } from "./net-naming.js";\n',
        new='import { nameRanks, pinNetName } from "./net-naming.js";\n',
    ),
]

REUSED_DESIGNATORS_FUNCTION = Patch(
    file="project.js",
    old="/** Parse an Altium project into one netlist. */\n",
    new="""/**
 * altium-helper patch: a designator used on more than one separately placed sheet names a
 * different part on each. Rename it `<designator>_<room>` per instance (`C10_Power`), the
 * room being the placing sheet symbol's designator (the top sheet's file name for the top
 * sheet), and rename the nets its pins named, before the instances merge. Not the project's
 * channel designator format: `$Component$ChannelAlpha` would name both parts `C10A`.
 */
const renameReusedDesignators = (pending, documents, instances, ranks) => {
    // Two sheets' parts with one designator are different parts unless they are parts of one
    // multi-part symbol (a CM5 drawn across six sheets): same part, no pin number in common.
    const seen = new Map();
    pending.forEach((document, index) => {
        for (const [refdes, component] of Object.entries(document.netlist.components)) {
            (seen.get(refdes) ?? seen.set(refdes, []).get(refdes)).push({ index, component });
        }
    });
    const identity = (component) => `${component.mpn ?? ""}|${component.comment ?? ""}|${component.description ?? ""}`;
    const clashes = new Set();
    for (const [refdes, uses] of seen) {
        for (let a = 0; a < uses.length; a++) {
            for (let b = a + 1; b < uses.length; b++) {
                if (uses[a].index === uses[b].index)
                    continue;
                const pinsA = new Set(Object.keys(uses[a].component.pins ?? {}));
                const shared = Object.keys(uses[b].component.pins ?? {}).some((pin) => pinsA.has(pin));
                if (shared || identity(uses[a].component) !== identity(uses[b].component))
                    clashes.add(refdes);
            }
        }
    }
    const refdesSets = pending.map((document) => new Set(Object.keys(document.netlist.components)));
    const pathOf = new Map(documents.map((document) => [document.name, document.path]));
    pending.forEach((document, index) => {
        const reused = new Set([...refdesSets[index]].filter((refdes) => clashes.has(refdes)));
        if (reused.size === 0)
            return;
        const chains = instances.get(document.name) ?? [];
        const chain = chains.find((candidate) => candidate.key === document.placement) ?? chains[0];
        const room = chain && chain.path.length > 0
            ? chain.room
            : (pathOf.get(document.name) ?? document.name).replace(/\\\\/g, "/").split("/").pop().replace(/\\.schdoc$/i, "");
        const rename = (refdes) => (reused.has(refdes) ? `${refdes}_${room}` : refdes);
        const nets = {};
        for (const [netName, connections] of Object.entries(document.netlist.nets)) {
            const target = (nets[netName] ??= {});
            for (const [refdes, pins] of Object.entries(connections))
                target[rename(refdes)] = pins;
        }
        const components = {};
        for (const [refdes, component] of Object.entries(document.netlist.components))
            components[rename(refdes)] = component;
        document.netlist = { nets, components };
        const renames = new Map();
        for (const net of document.nets ?? []) {
            const source = net.pinNameSource;
            if (net.name && net.nameSource === "pin" && source && reused.has(source.refdes))
                renames.set(net.name, pinNetName({ refdes: rename(source.refdes), pin: source.pin }));
        }
        renameDocumentNets(document, renames, ranks);
    });
};
/** Parse an Altium project into one netlist. */
""",
)

REUSED_DESIGNATORS_CALL = Patch(
    file="project.js",
    old=(
        "    const pending = await parseInstances(documents, placements, instances, naming, "
        "options.channelFormat, scope);\n"
    ),
    new=(
        "    const pending = await parseInstances(documents, placements, instances, naming, "
        "options.channelFormat, scope);\n"
        "    renameReusedDesignators(pending, documents, instances, ranks);\n"
    ),
)

PATCHES: list[Patch] = [
    SHEET_FILE_EXTENSION,
    *REUSED_DESIGNATORS_IMPORTS,
    REUSED_DESIGNATORS_FUNCTION,
    REUSED_DESIGNATORS_CALL,
]


def level() -> str:
    """A short fingerprint of these patches; part of the netlister tag cached netlists carry."""
    digest = hashlib.sha256()
    for patch in PATCHES:
        digest.update(f"{patch.file}\0{patch.old}\0{patch.new}\0".encode())
    return digest.hexdigest()[:10]


class PatchError(RuntimeError):
    """A patch's text isn't in the installed universal-netlist (a different version?)."""


MARKER = "altium-helper patch"


def apply(altium_dir: Path) -> list[str]:
    """Apply every patch under ``dist/parsers/altium``; returns the files changed.

    Patches always start from the file as npm installed it, kept beside it as ``<file>.orig``
    the first time, so applying again (or applying a newer version of these patches) never
    stacks one patch on another. A patched file without its ``.orig`` can't be trusted:
    that raises, and the caller reinstalls.
    """
    originals: dict[str, str] = {}
    for patch in PATCHES:
        if patch.file in originals:
            continue
        path = altium_dir / patch.file
        backup = path.with_name(path.name + ".orig")
        if backup.exists():
            originals[patch.file] = backup.read_text(encoding="utf-8")
            continue
        if not path.exists():
            raise PatchError(f"universal-netlist file {path} is missing")
        text = path.read_text(encoding="utf-8")
        if MARKER in text:
            raise PatchError(f"{path} was patched before, and its original is gone")
        backup.write_text(text, encoding="utf-8")
        originals[patch.file] = text
    patched = dict(originals)
    for patch in PATCHES:
        text = patched[patch.file]
        if text.count(patch.old) != 1:
            raise PatchError(
                f"can't patch {patch.file}: the text to replace appears {text.count(patch.old)} "
                "times. The installed universal-netlist isn't the version these patches were "
                "written for; revisit altium_helper/netlist_patches.py."
            )
        patched[patch.file] = text.replace(patch.old, patch.new)
    changed = []
    for name, text in sorted(patched.items()):
        path = altium_dir / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(name)
    return changed


def is_applied(altium_dir: Path) -> bool:
    """Whether every patched file is exactly its kept original with these patches applied."""
    try:
        for name in {patch.file for patch in PATCHES}:
            path = altium_dir / name
            text = path.with_name(path.name + ".orig").read_text(encoding="utf-8")
            for patch in PATCHES:
                if patch.file == name:
                    if text.count(patch.old) != 1:
                        return False
                    text = text.replace(patch.old, patch.new)
            if path.read_text(encoding="utf-8") != text:
                return False
        return True
    except OSError:
        return False
