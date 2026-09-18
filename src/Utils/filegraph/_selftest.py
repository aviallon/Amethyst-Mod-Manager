"""Focused regression tests for the Filegraph catalog's handling of Profile
Group symlink farms and the staging-dirty marker.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.filegraph._selftest -v

Requires the built native extension (``src/amethyst_filegraph.abi3.so`` or an
installed package); the tests skip cleanly when it is unavailable.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path


class _FakeGame:
    """Minimal game handler the Filegraph adapter needs for these tests."""

    name = "Fake Game"
    game_id = "fake_game"
    plugin_extensions = ("esp", "esm", "esl")
    mod_install_extensions = ()
    mod_folder_strip_prefixes = {"data"}
    filemap_exclude_dirs = ()
    frameworks = ()
    archive_extensions = frozenset()

    def __init__(self, staging_root: Path) -> None:
        self.staging_root = staging_root

    def get_game_path(self):
        return self.staging_root / "game"

    def get_mod_data_path(self):
        return self.staging_root / "game" / "Data"

    def get_mod_staging_path(self):
        return self.staging_root / "mods"

    def get_effective_mod_staging_path(self):
        return self.staging_root / "mods"

    def get_effective_overwrite_path(self):
        return self.staging_root / "overwrite"

    def get_effective_root_folder_path(self):
        return self.staging_root / "Root_Folder"

    def get_profile_root(self):
        return self.staging_root.parent

    def get_prefix_path(self):
        return None


def _native_available() -> bool:
    try:
        from Utils.filegraph.native import require_native
        require_native()
        return True
    except Exception:
        return False


@unittest.skipUnless(_native_available(), "native filegraph extension unavailable")
class GroupFarmStalenessTests(unittest.TestCase):
    """A wizard writing through a group's symlink farm must not be invisible.

    The group catalog and the member catalog both stay stale, so
    ``ensure_ready`` (the deploy fast path) reuses the old winner generation.
    A full ``refresh`` scans the farm link and picks the new file up - which is
    exactly what the staging-dirty marker forces the next deploy to do.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="filegraph-selftest-"))
        profiles = self.tmp / "profiles"
        adult = profiles / "Adult"
        (adult / "mods" / "Pandora_output" / "Meshes" / "Actors").mkdir(
            parents=True)
        (adult / "mods" / "Pandora_output" / "Meshes" / "Actors"
         / "idle.hkx").write_bytes(b"OLD")
        (adult / "profile_state.json").write_text(
            '{"profile_settings": {"profile_specific_mods": true}}',
            encoding="utf-8")
        (adult / "modlist.txt").write_text("+Pandora_output\n", encoding="utf-8")
        (adult / "plugins.txt").write_text("", encoding="utf-8")

        group = profiles / "Vanilla +"
        (group / "mods").mkdir(parents=True)
        os.symlink(
            os.path.relpath(adult / "mods" / "Pandora_output", group / "mods"),
            group / "mods" / "Pandora_output")
        (group / "profile_state.json").write_text(
            '{"profile_settings": {"is_group": true, '
            '"group_members": ["Adult"], "profile_specific_mods": true}}',
            encoding="utf-8")
        (group / "modlist.txt").write_text("+Pandora_output\n", encoding="utf-8")
        (group / "plugins.txt").write_text("", encoding="utf-8")

        self.group = group
        self.game = _FakeGame(profiles / "Vanilla +")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _catalog_files(self, library) -> list[str]:
        profile = library.open_profile(self.group)
        profile.ensure_reconciled(operation_hint={"kind": "selftest"})
        snapshot = profile.snapshot()
        return sorted(
            bytes(record.source_rel).decode("utf-8", "replace")
            for record in snapshot.mod_files("Pandora_output"))

    def test_wizard_output_through_group_link_needs_refresh(self) -> None:
        from Utils.filegraph.service import FileGraphService

        library = FileGraphService.open_library(self.game, self.group)
        library.refresh(self.group)
        self.assertEqual(
            self._catalog_files(library), ["Meshes/Actors/idle.hkx"])

        # Simulate Pandora writing through the group's symlink farm.
        (self.group / "mods" / "Pandora_output" / "Meshes"
         / "new.hkx").write_bytes(b"NEW")

        library.ensure_ready(self.group)   # deploy fast path
        self.assertNotIn(
            "Meshes/new.hkx", self._catalog_files(library),
            "ensure_ready should not see wizard output written through the "
            "group farm (regression documents why the marker is needed)")

        library.refresh(self.group)        # what the marker forces
        self.assertIn("Meshes/new.hkx", self._catalog_files(library))


class StagingDirtyMarkerTests(unittest.TestCase):
    def test_consume_is_atomic_and_clears(self) -> None:
        from Utils.filegraph import staleness
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            staleness.mark_staging_dirty(profile, "wizard output")
            self.assertEqual(
                staleness.staging_dirty_reason(profile), "wizard output")
            self.assertEqual(
                staleness.consume_staging_dirty(profile), "wizard output")
            self.assertIsNone(staleness.staging_dirty_reason(profile))


if __name__ == "__main__":
    unittest.main(verbosity=2)
