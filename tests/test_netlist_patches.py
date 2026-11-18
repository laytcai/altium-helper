import pytest

from altium_helper import netlist_patches
from altium_helper.netlist_patches import PATCHES, PatchError


def _pristine(folder):
    """Files holding exactly the text each patch replaces, as universal-netlist 1.12.0 ships it."""
    files = {}
    for patch in PATCHES:
        files[patch.file] = (
            files.get(patch.file, "// before\n") + patch.old + "// between\n"
        )
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return files


def test_patches_apply_once_and_keep_the_original(tmp_path):
    original = _pristine(tmp_path)
    assert not netlist_patches.is_applied(tmp_path)
    assert sorted(netlist_patches.apply(tmp_path)) == sorted(original)
    assert netlist_patches.is_applied(tmp_path)
    for name, text in original.items():
        assert (tmp_path / f"{name}.orig").read_text(encoding="utf-8") == text
    assert netlist_patches.apply(tmp_path) == []  # applying again changes nothing


def test_project_patch_declares_the_rename_once(tmp_path):
    _pristine(tmp_path)
    netlist_patches.apply(tmp_path)
    netlist_patches.apply(tmp_path)
    project = (tmp_path / "project.js").read_text(encoding="utf-8")
    assert project.count("const renameReusedDesignators") == 1
    assert (
        project.count("renameReusedDesignators(pending, documents, instances, ranks);")
        == 1
    )


def test_a_drifted_file_is_repatched_from_its_original(tmp_path):
    _pristine(tmp_path)
    netlist_patches.apply(tmp_path)
    (tmp_path / "project.js").write_text("something else", encoding="utf-8")
    assert not netlist_patches.is_applied(tmp_path)
    assert netlist_patches.apply(tmp_path) == ["project.js"]
    assert netlist_patches.is_applied(tmp_path)


def test_a_different_version_is_refused(tmp_path):
    _pristine(tmp_path)
    (tmp_path / "sheet-hierarchy.js").write_text(
        "// rewritten upstream\n", encoding="utf-8"
    )
    with pytest.raises(PatchError, match="sheet-hierarchy.js"):
        netlist_patches.apply(tmp_path)


def test_a_patched_file_without_its_original_is_refused(tmp_path):
    _pristine(tmp_path)
    netlist_patches.apply(tmp_path)
    for backup in tmp_path.glob("*.orig"):
        backup.unlink()
    with pytest.raises(PatchError, match="original is gone"):
        netlist_patches.apply(tmp_path)


def test_the_level_changes_with_the_patches(monkeypatch):
    level = netlist_patches.level()
    assert len(level) == 10
    monkeypatch.setattr(netlist_patches, "PATCHES", PATCHES[:-1])
    assert netlist_patches.level() != level
