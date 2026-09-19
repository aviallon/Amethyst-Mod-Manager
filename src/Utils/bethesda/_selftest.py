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

from Utils.bethesda import bodyslide_auto, xedit
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


_ANY_GAME = object()   # bodyslide_dir() is patched in these tests


class BodySlideChunkTests(unittest.TestCase):
    """Chunk planning, discovery and the temporary-group lifecycle.

    The load-bearing invariant is that chunking never silently drops an outfit: a
    dropped outfit means a body built wrong, with no error anywhere.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # the module reads the DEPLOYED tree; point it at a scratch one
        self._saved = bodyslide_auto.bodyslide_dir
        bodyslide_auto.bodyslide_dir = lambda _game: self.deployed  # type: ignore[assignment]
        self.deployed = self.root / "CalienteTools" / "BodySlide"
        (self.deployed / "SliderSets").mkdir(parents=True)
        (self.deployed / "SliderGroups").mkdir(parents=True)

    def tearDown(self) -> None:
        bodyslide_auto.bodyslide_dir = self._saved  # type: ignore[assignment]
        self._tmp.cleanup()

    def _set(self, name: str, data: int) -> None:
        body = "\n".join(f'<Data name="{name}_{i}"/>' for i in range(data))
        (self.deployed / "SliderSets" / "t.osp").write_text(
            f'<SliderSetInfo><SliderSet name="{name}">{body}</SliderSet></SliderSetInfo>',
            encoding="utf-8")

    def _group(self, name: str, members: list[str]) -> None:
        body = "\n".join(f'<Member name="{m}"/>' for m in members)
        (self.deployed / "SliderGroups" / f"{name}.xml").write_text(
            f'<SliderGroups><Group name="{name}">{body}</Group></SliderGroups>',
            encoding="utf-8")

    def _write_sets(self, specs: list[tuple[str, int]]) -> None:
        parts = []
        for name, data in specs:
            body = "".join(f'<Data name="{name}_{i}"/>' for i in range(data))
            parts.append(f'<SliderSet name="{name}">{body}</SliderSet>')
        (self.deployed / "SliderSets" / "a.osp").write_text(
            "<SliderSetInfo>" + "".join(parts) + "</SliderSetInfo>",
            encoding="utf-8")

    def test_discovery_reads_sets_and_groups(self) -> None:
        self._write_sets([("Alpha", 3), ("Beta", 5)])
        self._group("G", ["Alpha", "Beta"])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        self.assertEqual([o.name for o in outfits], ["Alpha", "Beta"])
        self.assertEqual({o.name: o.data for o in outfits}, {"Alpha": 3, "Beta": 5})
        self.assertEqual(bodyslide_auto.discover_groups(_ANY_GAME), {"G": ["Alpha", "Beta"]})

    def test_discovery_dedupes_the_same_outfit(self) -> None:
        self._write_sets([("Alpha", 4)])
        (self.deployed / "ConversionSets").mkdir()
        (self.deployed / "ConversionSets" / "b.osp").write_text(
            '<SliderSetInfo><SliderSet name="Alpha"><Data name="x"/></SliderSet></SliderSetInfo>',
            encoding="utf-8")
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        self.assertEqual(len(outfits), 1, "a duplicate outfit must not be counted twice")

    def test_chunking_never_drops_an_outfit(self) -> None:
        specs = [("A", 5000), ("B", 5000), ("C", 5000), ("D", 10)]
        self._write_sets(specs)
        self._group("Heavy", ["A", "B", "C"])     # 15000 > budget -> split
        self._group("Light", ["D"])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        chunks = bodyslide_auto.plan_chunks(
            bodyslide_auto.discover_groups(_ANY_GAME), outfits,
            max_data_per_chunk=8000)

        planned = [n for c in chunks for n in c.outfits]
        self.assertEqual(sorted(planned), ["A", "B", "C", "D"])
        self.assertEqual(len(planned), len(set(planned)), "no outfit twice")
        for chunk in chunks:
            self.assertLessEqual(chunk.data, 8000, f"{chunk.group} over budget")
        self.assertTrue(any(c.temporary for c in chunks), "heavy group must be split")

    def test_chunking_collects_ungrouped_outfits(self) -> None:
        self._write_sets([("A", 1), ("Orphan", 1)])
        self._group("G", ["A"])
        chunks = bodyslide_auto.plan_chunks(
            bodyslide_auto.discover_groups(_ANY_GAME),
            bodyslide_auto.discover_outfits(_ANY_GAME))
        planned = sorted(n for c in chunks for n in c.outfits)
        self.assertEqual(planned, ["A", "Orphan"],
                         "an outfit in no group must still be built")

    def test_chunk_files_are_written_then_cleared(self) -> None:
        self._write_sets([("A", 9000), ("B", 9000)])
        self._group("Heavy", ["A", "B"])
        chunks = bodyslide_auto.plan_chunks(
            bodyslide_auto.discover_groups(_ANY_GAME),
            bodyslide_auto.discover_outfits(_ANY_GAME),
            max_data_per_chunk=8000)

        written = bodyslide_auto.write_chunk_groups(_ANY_GAME, chunks)
        self.assertTrue(written, "split chunks need synthetic group files")
        for path in written:
            self.assertTrue(path.exists())

        # a leftover from a crashed run must be swept, not read back as a group
        leftover = self.deployed / "SliderGroups" / f"{bodyslide_auto.CHUNK_PREFIX}old.xml"
        leftover.write_text("<SliderGroups/>", encoding="utf-8")
        removed = bodyslide_auto.clear_chunk_groups(_ANY_GAME)
        self.assertEqual(removed, len(written) + 1)
        self.assertFalse(leftover.exists())
        self.assertFalse(any(p.exists() for p in written))

    def test_synthetic_chunk_groups_are_not_discovered_as_groups(self) -> None:
        self._write_sets([("A", 1)])
        self._group("Real", ["A"])
        (self.deployed / "SliderGroups" / f"{bodyslide_auto.CHUNK_PREFIX}stale.xml").write_text(
            f'<SliderGroups><Group name="{bodyslide_auto.CHUNK_PREFIX}stale">'
            '<Member name="A"/></Group></SliderGroups>', encoding="utf-8")
        self.assertEqual(list(bodyslide_auto.discover_groups(_ANY_GAME)), ["Real"])

    def test_fingerprint_changes_when_an_input_changes(self) -> None:
        self._write_sets([("A", 1)])
        self._group("G", ["A"])
        game = _ANY_GAME
        groups, outfits = bodyslide_auto.discover_groups(game), bodyslide_auto.discover_outfits(game)
        before = bodyslide_auto.fingerprint(game, "p", groups, outfits)
        self.assertEqual(before, bodyslide_auto.fingerprint(game, "p", groups, outfits))

        self._write_sets([("A", 2)])          # slider data changed
        after = bodyslide_auto.fingerprint(game, "p", groups,
                                           bodyslide_auto.discover_outfits(game))
        self.assertNotEqual(before, after, "changed slider data must invalidate")

    # -- the launch-hook contract -----------------------------------------

    def test_run_automatic_never_raises(self) -> None:
        """A broken automatic step must not stop the game launching."""
        logs: list[str] = []
        saved, saved_enabled = bodyslide_auto.is_stale, bodyslide_auto.is_enabled
        try:
            bodyslide_auto.is_enabled = lambda *_a, **_k: True
            bodyslide_auto.is_stale = lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("boom"))
            bodyslide_auto.run_automatic(_ANY_GAME, "p", log_fn=logs.append)
        finally:
            bodyslide_auto.is_stale, bodyslide_auto.is_enabled = saved, saved_enabled
        self.assertTrue(any("failed" in m for m in logs), logs)

    def test_run_automatic_skips_when_up_to_date(self) -> None:
        """An unchanged profile must not pay for a rebuild on every launch."""
        logs: list[str] = []
        called: list[int] = []
        saved = (bodyslide_auto.is_stale, bodyslide_auto.is_enabled,
                 bodyslide_auto.run_chunked)
        try:
            bodyslide_auto.is_enabled = lambda *_a, **_k: True
            bodyslide_auto.is_stale = lambda *_a, **_k: (False, "up to date")
            bodyslide_auto.run_chunked = lambda *_a, **_k: called.append(1) or 0
            bodyslide_auto.run_automatic(_ANY_GAME, "p", log_fn=logs.append)
        finally:
            (bodyslide_auto.is_stale, bodyslide_auto.is_enabled,
             bodyslide_auto.run_chunked) = saved
        self.assertEqual(called, [], "an up-to-date profile must not rebuild")
        self.assertTrue(any("skipped" in m for m in logs), logs)

    def test_is_enabled_needs_the_tool_installed(self) -> None:
        from Utils.bethesda import bodyslide_linux
        saved = bodyslide_linux.is_installed
        try:
            bodyslide_linux.is_installed = lambda: False
            self.assertFalse(bodyslide_auto.is_enabled(_ANY_GAME))
        finally:
            bodyslide_linux.is_installed = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
