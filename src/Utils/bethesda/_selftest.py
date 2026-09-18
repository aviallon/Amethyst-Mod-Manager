"""Focused regression tests for the xEdit QAC output redirect, fixed-point
cleaning loop, and the Filegraph staging-dirty marker.

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.bethesda._selftest -v
"""

from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path

from Utils.bethesda import xedit
from Utils.filegraph import staleness


class _FakeGame:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_core = root / "game" / "Data_Core"
        self.data_core.mkdir(parents=True, exist_ok=True)
        self.staging = root / "profiles" / "default" / "mods"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.data = root / "game" / "Data"
        self.data.mkdir(parents=True, exist_ok=True)
        (root / "profiles" / "default" / "modlist.txt").write_text(
            "", encoding="utf-8")
        self.deployed = False

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging

    def get_mod_staging_path(self) -> Path:
        return self.staging

    def get_profile_root(self) -> Path:
        return self.root

    def get_effective_filemap_path(self) -> Path:
        return self.root / "profiles" / "default" / "filemap.txt"

    def get_mod_data_path(self) -> Path:
        return self.data

    def get_deploy_active(self) -> bool:
        return self.deployed


class PluginCrcTests(unittest.TestCase):
    def test_plugin_crc32_matches_zlib(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Plugin.esp"
            payload = b"TES4" + b"\x00" * 128
            path.write_bytes(payload)
            self.assertEqual(
                xedit.plugin_crc32(path),
                zlib.crc32(payload) & 0xFFFFFFFF)

    def test_missing_file_is_none(self) -> None:
        self.assertIsNone(
            xedit.plugin_crc32(Path("/nonexistent/Plugin.esp")))


class FixedPointTests(unittest.TestCase):
    def test_iterates_until_crc_stable(self) -> None:
        # Emulate the Dawnguard sequence: three distinct CRCs, then stable.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Dawnguard.esm"
            states = [b"A", b"B", b"C"]
            path.write_bytes(b"0x6CEC879A")
            calls = {"n": 0}

            def run_pass() -> None:
                if calls["n"] < len(states):
                    path.write_bytes(states[calls["n"]])
                calls["n"] += 1

            logs: list[str] = []
            result = xedit.clean_plugin_to_fixed_point(
                path, run_pass, max_passes=5,
                label="Dawnguard.esm", log_fn=logs.append)

            self.assertEqual(calls["n"], 4)   # 3 changes + 1 stable check
            self.assertTrue(result["converged"])
            self.assertEqual(result["passes"], 4)
            self.assertNotEqual(result["crc_before"], result["crc_after"])
            self.assertTrue(any("Dawnguard.esm" in line for line in logs))

    def test_stops_at_cap_when_never_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Plugin.esp"
            path.write_bytes(b"seed")
            calls = {"n": 0}

            def run_pass() -> None:
                calls["n"] += 1
                path.write_bytes(b"x" * calls["n"])

            result = xedit.clean_plugin_to_fixed_point(
                path, run_pass, max_passes=3, label="Plugin.esp")
            self.assertEqual(calls["n"], 3)
            self.assertFalse(result["converged"])
            self.assertEqual(result["passes"], 3)

    def test_already_clean_needs_one_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Plugin.esp"
            path.write_bytes(b"clean")
            calls = {"n": 0}

            def run_pass() -> None:
                calls["n"] += 1

            result = xedit.clean_plugin_to_fixed_point(
                path, run_pass, max_passes=3, label="Plugin.esp")
            self.assertEqual(calls["n"], 1)
            self.assertTrue(result["converged"])


class PluginChangeDetectionTests(unittest.TestCase):
    def test_changed_since_detects_edit_and_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / "Old.esp").write_bytes(b"old")
            (data / "Untouched.esm").write_bytes(b"same")
            baseline = xedit.snapshot_plugin_stats(data)
            (data / "Old.esp").write_bytes(b"old-but-longer")
            (data / "New.esl").write_bytes(b"new")
            changed = set(xedit.plugins_changed_since(data, baseline))
            self.assertEqual(changed, {"Old.esp", "New.esl"})


class SeedVanillaMasterTests(unittest.TestCase):
    def _seed(self, game, plugins, owners: dict[str, str]):
        from unittest import mock
        with mock.patch.object(
                xedit, "_top_level_plugin_owners", return_value=owners), \
             mock.patch("Utils.games.registry._vanilla_plugins_for_game",
                        return_value={"Dawnguard.esm": "Dawnguard.esm"}), \
             mock.patch("Utils.vfs.effective_tool_data_root",
                        return_value=game.get_mod_data_path()):
            return xedit.seed_vanilla_master_output(
                game, "default", plugins, log_fn=lambda _m: None)

    def test_vanilla_master_is_copied_to_output_mod(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = _FakeGame(Path(temporary))
            # Deployed vanilla: Data/Dawnguard.esm -> Data_Core/Dawnguard.esm
            game.deployed = True
            (game.data_core / "Dawnguard.esm").write_bytes(b"vanilla")
            (game.data / "Dawnguard.esm").symlink_to(
                game.data_core / "Dawnguard.esm")
            seeded = self._seed(game, ["Dawnguard.esm"], owners={})
            self.assertEqual(seeded, ["Dawnguard.esm"])
            output = game.staging / xedit.XEDIT_OUTPUT_MOD
            self.assertTrue((output / "Dawnguard.esm").is_file())
            self.assertEqual(
                (output / "Dawnguard.esm").read_bytes(), b"vanilla")
            modlist = (game.root / "profiles" / "default" / "modlist.txt")
            self.assertIn(xedit.XEDIT_OUTPUT_MOD, modlist.read_text())
            self.assertIsNotNone(
                staleness.staging_dirty_reason(
                    game.root / "profiles" / "default"))

    def test_undeployed_game_seeds_plain_vanilla_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = _FakeGame(Path(temporary))
            game.deployed = False
            (game.data / "Dawnguard.esm").write_bytes(b"vanilla")
            seeded = self._seed(game, ["Dawnguard.esm"], owners={})
            self.assertEqual(seeded, ["Dawnguard.esm"])

    def test_mod_owned_plugin_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = _FakeGame(Path(temporary))
            game.deployed = True
            (game.data / "Dawnguard.esm").write_bytes(b"vanilla")
            seeded = self._seed(
                game, ["Dawnguard.esm"],
                owners={"dawnguard.esm": "Some Cleaner Mod"})
            self.assertEqual(seeded, [])
            self.assertFalse(
                (game.staging / xedit.XEDIT_OUTPUT_MOD).exists())

    def test_deployed_unknown_ownership_regular_file_is_left_alone(self) -> None:
        # Hardlink/copy deploy with no catalog info: not positive proof.
        with tempfile.TemporaryDirectory() as temporary:
            game = _FakeGame(Path(temporary))
            game.deployed = True
            (game.data / "Dawnguard.esm").write_bytes(b"vanilla")
            seeded = self._seed(game, ["Dawnguard.esm"], owners={})
            self.assertEqual(seeded, [])

    def test_non_vanilla_plugin_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = _FakeGame(Path(temporary))
            (game.data / "CoolMod.esp").write_bytes(b"mod")
            seeded = self._seed(game, ["CoolMod.esp"], owners={})
            self.assertEqual(seeded, [])

class StagingDirtyMarkerTests(unittest.TestCase):
    def test_marker_round_trip_and_consume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            self.assertIsNone(staleness.staging_dirty_reason(profile))
            staleness.mark_staging_dirty(profile, "Pandora wrote output")
            self.assertEqual(
                staleness.staging_dirty_reason(profile), "Pandora wrote output")
            reason = staleness.consume_staging_dirty(profile)
            self.assertEqual(reason, "Pandora wrote output")
            self.assertIsNone(staleness.staging_dirty_reason(profile))
            # Consuming twice is a no-op.
            self.assertIsNone(staleness.consume_staging_dirty(profile))

    def test_mark_is_idempotent_newest_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            staleness.mark_staging_dirty(profile, "first")
            staleness.mark_staging_dirty(profile, "second")
            self.assertEqual(
                staleness.staging_dirty_reason(profile), "second")


if __name__ == "__main__":
    unittest.main(verbosity=2)
