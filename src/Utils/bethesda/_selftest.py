"""Focused regression tests for the xEdit QAC output redirect, fixed-point
cleaning loop, the Filegraph staging-dirty marker, and the wizard output-mod
helpers (per-profile name, clean-build-dir swap, modlist toggling).

Run from the repository root with::

    PYTHONPATH=src python3 -m Utils.bethesda._selftest -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

from Utils.bethesda import bodyslide_auto, xedit
from Utils.bethesda.bodyslide_linux import process_tree_rss_mb
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


    def test_ungrouped_outfits_are_chunked_too(self) -> None:
        """Outfits in NO group must still be split by the budget.

        Several packs (CBBE, for one) ship no slider groups at all, so lumping
        everything else into one chunk would rebuild the unbounded batch this
        module exists to avoid - and the first automatic run builds everything.
        """
        self._write_sets([("A", 5000), ("B", 5000), ("C", 5000)])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        chunks = bodyslide_auto.plan_chunks({}, outfits, max_data_per_chunk=8000)

        self.assertGreater(len(chunks), 1, "ungrouped outfits must be split")
        for chunk in chunks:
            self.assertLessEqual(chunk.data, 8000, f"{chunk.group} over budget")
            self.assertTrue(chunk.temporary)
        planned = sorted(n for c in chunks for n in c.outfits)
        self.assertEqual(planned, ["A", "B", "C"])

    def test_ensure_output_dir_reports_why_it_failed(self) -> None:
        """A failure must carry a reason, not just return None.

        Regression: a wrong-arity call to sanitize_output_name() raised, was
        swallowed, and reported only "could not determine the output mod" - which
        said nothing about what to fix.
        """
        class _BrokenGame:
            def get_effective_mod_staging_path(self):
                raise RuntimeError("no staging path")

            def get_profile_root(self):
                raise RuntimeError("no profile root")

        path, reason = bodyslide_auto.ensure_output_dir(_BrokenGame(), "p")
        self.assertIsNone(path)
        self.assertTrue(reason, "the reason must be non-empty")
        self.assertIn("staging", reason.lower(), reason)


    def test_alternatives_detected_by_member_overlap(self) -> None:
        """Same outfits under two group names = one choice, not two builds."""
        self._write_sets([("A", 1), ("B", 1), ("C", 1), ("D", 1)])
        self._group("CBBE", ["A", "B", "C"])
        self._group("3BA", ["A", "B", "C"])
        self._group("Unrelated", ["D"])
        groups = bodyslide_auto.discover_groups(_ANY_GAME)
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)

        alts = bodyslide_auto.detect_alternatives(groups, outfits)
        self.assertEqual(len(alts), 1, alts)
        self.assertEqual(alts[0].groups, ["3BA", "CBBE"])

        # default: build the first, skip the sibling
        skip = bodyslide_auto.skipped_groups(alts, {})
        self.assertEqual(skip, {"CBBE"})
        # the user's stored choice wins
        self.assertEqual(bodyslide_auto.skipped_groups(alts, {alts[0].key: "CBBE"}),
                         {"3BA"})

    def test_skipped_alternative_outfits_are_not_built(self) -> None:
        self._write_sets([("A", 1), ("B", 1), ("C", 1)])
        self._group("CBBE", ["A", "B"])
        self._group("3BA", ["A", "B"])
        groups = bodyslide_auto.discover_groups(_ANY_GAME)
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        alts = bodyslide_auto.detect_alternatives(groups, outfits)
        skip = bodyslide_auto.skipped_groups(alts, {alts[0].key: "CBBE"})

        chunks = bodyslide_auto.plan_chunks(groups, outfits, skip_groups=skip)
        built = [n for c in chunks for n in c.outfits]
        self.assertEqual(sorted(built), ["A", "B", "C"], "C is ungrouped, must remain")
        self.assertEqual([c.group for c in chunks if c.group == "CBBE"], ["CBBE"])
        self.assertNotIn("3BA", [c.group for c in chunks])

    def test_outfit_count_caps_a_chunk(self) -> None:
        """Memory tracks outfit count (measured), so it must bound chunks too."""
        self._write_sets([(f"O{i}", 1) for i in range(25)])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        chunks = bodyslide_auto.plan_chunks(
            {}, outfits, max_data_per_chunk=10_000_000,
            max_outfits_per_chunk=10)
        self.assertGreater(len(chunks), 1, "25 outfits with a cap of 10 must split")
        for chunk in chunks:
            self.assertLessEqual(len(chunk.outfits), 10, chunk.group)
        self.assertEqual(sorted(n for c in chunks for n in c.outfits),
                         sorted(f"O{i}" for i in range(25)))

    def test_choices_round_trip(self) -> None:
        saved = bodyslide_auto.choices_path
        try:
            path = self.root / "choices.json"
            bodyslide_auto.choices_path = lambda *_a: path  # type: ignore[assignment]
            self.assertEqual(bodyslide_auto.load_choices(_ANY_GAME, "p"), {})
            bodyslide_auto.save_choices(_ANY_GAME, "p", {"CBBE": "3BA"})
            self.assertEqual(bodyslide_auto.load_choices(_ANY_GAME, "p"),
                             {"CBBE": "3BA"})
        finally:
            bodyslide_auto.choices_path = saved  # type: ignore[assignment]


    def test_a_subset_group_is_not_an_alternative(self) -> None:
        """A small group inside a bigger one is a SUBSET, not a variant.

        Regression, found on a real load order: normalising overlap by the
        smaller group merged the 5-outfit 'CBBE' group with the 32-outfit
        'CBBE Vanilla Outfits', and skipping the latter would have silently
        dropped 32 wanted outfits.
        """
        self._write_sets([(f"V{i}", 1) for i in range(6)] + [("Base", 1)])
        self._group("CBBE", ["Base"])
        self._group("CBBE Vanilla Outfits", [f"V{i}" for i in range(6)])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        alts = bodyslide_auto.detect_alternatives(
            bodyslide_auto.discover_groups(_ANY_GAME), outfits)

        for alt in alts:
            self.assertNotIn("CBBE Vanilla Outfits", alt.groups, alt.groups)
        self.assertEqual(bodyslide_auto.skipped_groups(alts, {}), set())

    def test_a_weak_candidate_is_offered_but_never_skipped(self) -> None:
        """A shared name prefix is not good enough to drop someone's outfits."""
        self._write_sets([("S1", 1), ("S2", 1), ("V1", 1), ("V2", 1)])
        self._group("HIMBO Body for SOS", ["S1", "S2"])
        self._group("HIMBO Body for Vanilla", ["V1", "V2"])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        alts = bodyslide_auto.detect_alternatives(
            bodyslide_auto.discover_groups(_ANY_GAME), outfits)

        self.assertEqual(len(alts), 1, alts)
        self.assertFalse(alts[0].certain, "a name guess must not be certain")
        self.assertEqual(bodyslide_auto.skipped_groups(alts, {}), set(),
                         "nothing may be skipped without being certain")
        # but an explicit choice is honoured
        self.assertEqual(
            bodyslide_auto.skipped_groups(alts, {alts[0].key: "HIMBO Body for SOS"}),
            {"HIMBO Body for Vanilla"})

    def test_identical_groups_are_certain_and_reduced_to_one(self) -> None:
        self._write_sets([("A", 1), ("B", 1)])
        self._group("CBBE Vanilla Outfits", ["A", "B"])
        self._group("CBBE Vanilla Outfits Physics", ["A", "B"])
        outfits = bodyslide_auto.discover_outfits(_ANY_GAME)
        alts = bodyslide_auto.detect_alternatives(
            bodyslide_auto.discover_groups(_ANY_GAME), outfits)
        self.assertEqual(len(alts), 1, alts)
        self.assertTrue(alts[0].certain)
        skip = bodyslide_auto.skipped_groups(alts, {})
        self.assertEqual(len(skip), 1, "exactly one variant is built")


    def test_isolate_failures_names_the_offending_outfit(self) -> None:
        """A dying chunk must end up as ONE named outfit, and the rest must build.

        This is the behaviour the whole chunked build rests on: BodySlide asks
        for memory as a function of the outfit it is on, so a chunk failing says
        nothing about which of its 48 outfits did it, and reporting the chunk
        would leave the user with no idea what to look at.
        """
        bad = "CBBE Dragonborn - Body - Telvanni"
        members = [f"Outfit {i:02d}" for i in range(15)] + [bad]
        calls: list[list[str]] = []

        def run(_group: str, names: list[str]) -> int:
            calls.append(list(names))
            return -100 if bad in names else 0  # -100 = the memory watchdog

        logs: list[str] = []
        failed = bodyslide_auto.isolate_failures(run, members, log_fn=logs.append,
                                                 tag="t")
        self.assertEqual(failed, [bad])
        self.assertIn([bad], calls, "the culprit must be tried on its own")
        # Every OTHER outfit must have been in a call that succeeded. Halving
        # does also try some siblings alone on the way down - that is fine and
        # free - but none of them may be left unbuilt or reported as failed.
        built: set[str] = set()
        for names in calls:
            if bad not in names:
                built.update(names)
        self.assertEqual(built, set(members) - {bad},
                         "only the offending outfit may be left unbuilt")
        self.assertTrue(any("failed on its own" in m for m in logs), logs)

    def test_isolate_failures_is_a_noop_when_the_chunk_builds(self) -> None:
        calls: list[list[str]] = []

        def run(_group: str, names: list[str]) -> int:
            calls.append(list(names))
            return 0

        self.assertEqual(
            bodyslide_auto.isolate_failures(run, ["A", "B", "C", "D"]), [])
        self.assertEqual(len(calls), 1, "a healthy chunk is run exactly once")

    def test_process_tree_rss_mb_reports_our_own_memory(self) -> None:
        rss = process_tree_rss_mb(os.getpid())
        self.assertGreater(rss, 0.0)
        self.assertLess(rss, 100_000.0)

    def test_process_tree_rss_mb_survives_a_missing_pid(self) -> None:
        self.assertEqual(process_tree_rss_mb(999_999_998), 0.0)


class OutputModSwapTests(unittest.TestCase):
    """swap_output_into_mod: atomic whole-directory replace of the mod content."""

    @staticmethod
    def _layout(root: Path) -> tuple[Path, Path]:
        build = root / "staging" / "Mod.build"
        mod = root / "staging" / "Mod"
        build.mkdir(parents=True)
        (build / "new1.txt").write_text("new1", encoding="utf-8")
        (build / "new2.txt").write_text("new2", encoding="utf-8")
        mod.mkdir(parents=True)
        (mod / "old.txt").write_text("old", encoding="utf-8")
        return build, mod

    def test_swap_replaces_old_content_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            installed = xedit.swap_output_into_mod(build, mod)
            self.assertEqual(installed, 2)
            self.assertEqual(
                {p.name for p in mod.iterdir()}, {"new1.txt", "new2.txt"})
            self.assertEqual((mod / "new1.txt").read_text(encoding="utf-8"),
                             "new1")
            # The build dir is consumed by the rename swap: nothing is left
            # for a modlist sync to adopt as a junk '<name>.build' entry.
            self.assertFalse(build.exists())
            self.assertFalse(
                (build.parent / (build.name + ".previous")).exists())

    def test_swap_refuses_an_empty_build_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            for child in build.iterdir():
                child.unlink()
            with self.assertRaises(RuntimeError):
                xedit.swap_output_into_mod(build, mod)
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "old")
            self.assertTrue(build.is_dir())

    def test_swap_refuses_a_missing_build_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            shutil.rmtree(build)
            with self.assertRaises(RuntimeError):
                xedit.swap_output_into_mod(build, mod)
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "old")

    def test_swap_rolls_back_when_the_final_rename_fails(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                if Path(src) == build:
                    raise OSError("injected rename failure")
                return real_replace(src, dst, **kwargs)

            with mock.patch("Utils.bethesda.xedit.os.replace",
                            side_effect=flaky_replace):
                with self.assertRaises(RuntimeError):
                    xedit.swap_output_into_mod(
                        build, mod, log_fn=lambda _m: None)
            # Rolled back with the single restore rename: the mod holds the
            # old content again and the new output is untouched in the build
            # dir - never a half-swapped mix.
            self.assertEqual({p.name for p in mod.iterdir()}, {"old.txt"})
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "old")
            self.assertEqual({p.name for p in build.iterdir()},
                             {"new1.txt", "new2.txt"})
            self.assertFalse(
                (build.parent / (build.name + ".previous")).exists())

    def test_swap_recovers_from_a_crash_between_the_renames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            # Simulate a crash between the two renames: the old content is
            # parked in the backup (the ONLY complete copy), the mod dir is
            # gone, the new output is still in the build dir.
            mod.rename(backup)
            logs: list[str] = []
            installed = xedit.swap_output_into_mod(
                build, mod, log_fn=logs.append)
            self.assertEqual(installed, 2)
            self.assertEqual({p.name for p in mod.iterdir()},
                             {"new1.txt", "new2.txt"})
            self.assertFalse(backup.exists())
            self.assertFalse(build.exists())
            # Discriminative: the backup was RESTORED first (and logged),
            # never silently dropped.
            self.assertTrue(any("restoring" in line for line in logs), logs)

    def test_swap_recovers_the_previous_output_over_a_placeholder(self) -> None:
        """ensure_output_mod recreates the mod dir as an EMPTY placeholder on
        the next wizard run after a crashed swap. A placeholder is "no
        previous content", never proof that the backup is stale - the
        recovery must leave the PREVIOUS output installed (a real state
        oracle, not just a log line)."""
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            mod.rename(backup)
            # Next wizard run: ensure_output_mod recreates an empty dir.
            mod.mkdir()
            logs: list[str] = []
            xedit.recover_swap_leftovers(build, mod, log_fn=logs.append)
            # STATE: the previous output is back in the mod dir now.
            self.assertEqual({p.name for p in mod.iterdir()}, {"old.txt"})
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "old")
            self.assertFalse(backup.exists())
            self.assertTrue(any("restoring" in line for line in logs), logs)
            # And the swap then installs the new output as usual.
            installed = xedit.swap_output_into_mod(
                build, mod, log_fn=logs.append)
            self.assertEqual(installed, 2)
            self.assertEqual({p.name for p in mod.iterdir()},
                             {"new1.txt", "new2.txt"})
            self.assertFalse(build.exists())

    def test_swap_preserves_the_previous_output_when_the_install_fails(
            self) -> None:
        """C1 preservation property: in the placeholder case a FAILED install
        must roll back to the PREVIOUS output (the backup), not to the empty
        placeholder - the backup is the only complete copy of it."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            mod.rename(backup)
            mod.mkdir()          # ensure_output_mod's empty placeholder
            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                if Path(src) == build:
                    raise OSError("injected rename failure")
                return real_replace(src, dst, **kwargs)

            with mock.patch("Utils.bethesda.xedit.os.replace",
                            side_effect=flaky_replace):
                with self.assertRaises(RuntimeError):
                    xedit.swap_output_into_mod(
                        build, mod, log_fn=lambda _m: None)
            self.assertEqual({p.name for p in mod.iterdir()}, {"old.txt"})
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "old")
            self.assertEqual({p.name for p in build.iterdir()},
                             {"new1.txt", "new2.txt"})
            self.assertFalse(backup.exists())

    def test_swap_removes_a_stale_backup_when_the_mod_survived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            backup.mkdir()
            (backup / "older.txt").write_text("older", encoding="utf-8")
            # Touch the mod dir LAST so it is the later one (renames preserve
            # mtimes: in a real completed swap the mod holds the newer build
            # output).
            os.utime(mod, None)
            # Non-empty mod dir = the previous swap completed its install but
            # crashed before cleanup: the mod is the later content and only
            # then is the older backup removed (discriminative: logged).
            logs: list[str] = []
            installed = xedit.swap_output_into_mod(
                build, mod, log_fn=logs.append)
            self.assertEqual(installed, 2)
            self.assertEqual({p.name for p in mod.iterdir()},
                             {"new1.txt", "new2.txt"})
            self.assertFalse(backup.exists())
            self.assertTrue(any("stale swap backup" in line for line in logs),
                            logs)

    def test_swap_keeps_the_later_content_when_a_stale_backup_lingers(
            self) -> None:
        """Content is authority, not existence: with a NON-EMPTY mod dir the
        backup is the OLDER output, so a failed install rolls back to the mod's
        own later content."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            backup.mkdir()
            (backup / "older.txt").write_text("older", encoding="utf-8")
            (mod / "old.txt").write_text("later", encoding="utf-8")
            os.utime(mod, None)      # the mod dir is the later one
            real_replace = os.replace

            def flaky_replace(src, dst, **kwargs):
                if Path(src) == build:
                    raise OSError("injected rename failure")
                return real_replace(src, dst, **kwargs)

            with mock.patch("Utils.bethesda.xedit.os.replace",
                            side_effect=flaky_replace):
                with self.assertRaises(RuntimeError):
                    xedit.swap_output_into_mod(
                        build, mod, log_fn=lambda _m: None)
            self.assertEqual({p.name for p in mod.iterdir()}, {"old.txt"})
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "later")
            self.assertFalse(backup.exists())
            self.assertEqual({p.name for p in build.iterdir()},
                             {"new1.txt", "new2.txt"})

    def test_swap_refuses_to_drop_a_backup_newer_than_the_mod(self) -> None:
        """Recency guard: a hand-edited (older) non-empty mod dir is not proof
        of later content - when the backup is NEWER the call refuses loudly
        instead of discarding either copy."""
        import time
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            backup.mkdir()
            (backup / "newer.txt").write_text("newer", encoding="utf-8")
            (mod / "old.txt").write_text("hand-edited", encoding="utf-8")
            old = time.time() - 3600
            os.utime(mod, (old, old))       # the mod dir looks OLDER
            with self.assertRaises(RuntimeError) as caught:
                xedit.swap_output_into_mod(build, mod, log_fn=lambda _m: None)
            self.assertIn("NEWER", str(caught.exception))
            # Nothing was discarded, nothing moved.
            self.assertTrue((backup / "newer.txt").is_file())
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "hand-edited")
            self.assertEqual({p.name for p in build.iterdir()},
                             {"new1.txt", "new2.txt"})

    def test_swap_cleanup_failure_never_blocks_the_next_run(self) -> None:
        """A failed backup cleanup leaves INERT trash (never
        '<build>.previous'): a partial rmtree bumps mtimes, which would make
        the next run's recency guard refuse a state the tool created
        itself."""
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            old_time = time.time() - 3600
            os.utime(build, (old_time, old_time))   # will become the mod
            # The swap succeeds but the trash removal fails afterwards.
            with mock.patch("Utils.bethesda.xedit.shutil.rmtree",
                            side_effect=OSError("injected cleanup failure")):
                installed = xedit.swap_output_into_mod(
                    build, mod, log_fn=lambda _m: None)
            self.assertEqual(installed, 2)
            self.assertEqual({p.name for p in mod.iterdir()},
                             {"new1.txt", "new2.txt"})
            # No '.previous' backup remains - only inert trash.
            self.assertFalse(backup.exists())
            self.assertTrue(
                any(p.name.startswith(build.name + ".previous.trash-")
                    for p in build.parent.iterdir()),
                list(build.parent.iterdir()))
            # The next run's recovery must not refuse this state.
            xedit.recover_swap_leftovers(build, mod, log_fn=lambda _m: None)
            self.assertEqual({p.name for p in mod.iterdir()},
                             {"new1.txt", "new2.txt"})

    def test_recovery_refuses_when_ages_are_unknown(self) -> None:
        """Uncertainty must never pick the destructive branch (dropping the
        backup deletes data): when the ages cannot be compared the recovery
        refuses loudly and both copies survive."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            backup = build.parent / (build.name + ".previous")
            backup.mkdir()
            (backup / "older.txt").write_text("older", encoding="utf-8")
            (mod / "old.txt").write_text("later", encoding="utf-8")
            os.utime(mod, None)
            real_stat = Path.stat

            class _Ageless:
                def __init__(self, real):
                    self._real = real

                def __getattr__(self, name):
                    return getattr(self._real, name)

                @property
                def st_mtime(self):
                    raise OSError("injected stat failure")

            def fake_stat(self, *args, **kwargs):
                st = real_stat(self, *args, **kwargs)
                return _Ageless(st) if self == mod else st

            with mock.patch("pathlib.Path.stat", fake_stat):
                with self.assertRaises(RuntimeError) as caught:
                    xedit.swap_output_into_mod(build, mod,
                                               log_fn=lambda _m: None)
            self.assertIn("refusing to discard either copy",
                          str(caught.exception))
            # Nothing was discarded.
            self.assertTrue((backup / "older.txt").is_file())
            self.assertEqual((mod / "old.txt").read_text(encoding="utf-8"),
                             "later")

    def test_swap_refuses_cross_filesystem_layout(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            build, mod = self._layout(Path(temporary))
            real_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                st = real_stat(self, *args, **kwargs)
                if self.name == "Mod.build":
                    return os.stat_result((
                        st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime,
                        st.st_mtime, st.st_ctime))
                return st

            with mock.patch("pathlib.Path.stat", fake_stat):
                with self.assertRaises(RuntimeError) as caught:
                    xedit.swap_output_into_mod(build, mod)
            self.assertIn("filesystem", str(caught.exception))
            # Loud check before anything moved.
            self.assertEqual({p.name for p in mod.iterdir()}, {"old.txt"})
            self.assertEqual({p.name for p in build.iterdir()},
                             {"new1.txt", "new2.txt"})


class StagingScanFilterTests(unittest.TestCase):
    def test_staging_mod_dirs_skips_reserved_names(self) -> None:
        """The shared staging scan (modlist sync + filegraph) never adopts or
        scans reserved infrastructure/scratch names, but keeps every real mod
        - ".build" names included."""
        from Utils.mods.names import staging_mod_dirs
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            (staging / "RealMod").mkdir()
            (staging / "X.build").mkdir()
            (staging / "Sep_separator").mkdir()
            (staging / ".amethyst-build").mkdir()
            (staging / "Overwrite").mkdir()      # reserved, case-insensitive
            (staging / "a-file.txt").write_text("x", encoding="utf-8")
            names = sorted(p.name for p in staging_mod_dirs(staging))
            self.assertEqual(names, ["RealMod", "X.build"])


class WizardScratchSyncTests(unittest.TestCase):
    def test_sync_keeps_x_build_mods_and_skips_the_scratch_root(self) -> None:
        """C-1 regression: a real mod ending in ".build" keeps its entry
        (enabled/order/locked state is unrecoverable once dropped), while the
        reserved ".amethyst-build" scratch root is never adopted or renamed.
        A NEW "X.build" folder is an ordinary mod: adopted (disabled) like
        any other."""
        from Utils.mods.modlist import (
            read_modlist, sync_modlist_with_mods_folder)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mods = root / "mods"
            mods.mkdir()
            (mods / "RealMod").mkdir()
            real = mods / "MyMod.build"
            real.mkdir()
            (real / "content.esp").write_text("real mod content",
                                              encoding="utf-8")
            weird = mods / "X.build .build"
            weird.mkdir()
            (weird / "x.esp").write_text("x", encoding="utf-8")
            scratch = mods / ".amethyst-build"
            (scratch / "TexGen_Output_default.build").mkdir(parents=True)
            backup = scratch / "TexGen_Output_default.build.previous"
            backup.mkdir()
            (backup / "old.txt").write_text("old", encoding="utf-8")
            modlist = root / "modlist.txt"
            modlist.write_text("+MyMod.build\n*RealMod\n", encoding="utf-8")
            sync_modlist_with_mods_folder(modlist, mods)
            names = [e.name for e in read_modlist(modlist)]
            entries = {e.name: e for e in read_modlist(modlist)}
            # The legit ".build" mod keeps its entry AND its flags/order (the
            # odd "X.build .build" name is adopted as an ordinary disabled
            # mod, like any unlisted folder).
            self.assertEqual(names,
                             ["X.build .build", "MyMod.build", "RealMod"])
            self.assertTrue(entries["MyMod.build"].enabled)
            self.assertTrue(entries["RealMod"].locked)
            # The reserved scratch root is invisible to the sync - its
            # content is untouched (the wizard recovers it).
            self.assertNotIn(".amethyst-build", entries)
            self.assertTrue((backup / "old.txt").is_file())
            # A brand-new X.build folder is adopted like any other mod.
            (mods / "X.build").mkdir()
            (mods / "X.build" / "new.esp").write_text("new", encoding="utf-8")
            sync_modlist_with_mods_folder(modlist, mods)
            names = [e.name for e in read_modlist(modlist)]
            entries = {e.name: e for e in read_modlist(modlist)}
            self.assertEqual(names[0], "X.build")
            self.assertFalse(entries["X.build"].enabled)


class OutputModNamingTests(unittest.TestCase):
    def test_per_profile_output_mod_name(self) -> None:
        self.assertEqual(
            xedit.per_profile_output_mod_name("TexGen_Output", "default"),
            "TexGen_Output_default")
        self.assertEqual(
            xedit.per_profile_output_mod_name("TexGen_Output", "heavy"),
            "TexGen_Output_heavy")
        # Filesystem-hostile profile names are sanitized, never passed through.
        name = xedit.per_profile_output_mod_name("TexGen_Output", "a/b:c")
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)

    def test_sanitized_profiles_never_collide(self) -> None:
        # "a/b" and "ab" sanitize to the same base name - the hash suffix
        # derived from the raw spelling keeps their output mods distinct.
        slashed = xedit.per_profile_output_mod_name("TexGen_Output", "a/b")
        plain = xedit.per_profile_output_mod_name("TexGen_Output", "ab")
        self.assertNotEqual(slashed, plain)
        self.assertTrue(slashed.startswith("TexGen_Output_ab_"), slashed)
        # Deterministic across calls.
        self.assertEqual(
            slashed, xedit.per_profile_output_mod_name("TexGen_Output", "a/b"))

    def test_output_mod_build_dir_is_staging_side(self) -> None:
        staging = Path("/profiles/game/mods")
        self.assertEqual(
            xedit.output_mod_build_dir(staging, "TexGen_Output_default"),
            staging / ".amethyst-build" / "TexGen_Output_default.build")

    def test_set_mod_enabled_toggles_in_place(self) -> None:
        from Utils.mods.modlist import (
            ModEntry, prepend_mod, read_modlist, set_mod_enabled, write_modlist,
        )
        with tempfile.TemporaryDirectory() as temporary:
            modlist = Path(temporary) / "modlist.txt"
            prepend_mod(modlist, "Other", enabled=True)
            prepend_mod(modlist, "Output", enabled=True)
            self.assertTrue(set_mod_enabled(modlist, "Output", False))
            entries = read_modlist(modlist)
            self.assertEqual([e.name for e in entries], ["Output", "Other"])
            self.assertFalse(entries[0].enabled)
            self.assertTrue(entries[1].enabled)
            self.assertTrue(set_mod_enabled(modlist, "Output", True))
            self.assertTrue(read_modlist(modlist)[0].enabled)
            self.assertFalse(set_mod_enabled(modlist, "Missing", False))

            # Locked ("*") entries are never toggled - and a toggle that did
            # not take effect must REPORT False: the requested state is NOT
            # in effect, so an enforcing caller cannot assume it (B2: a locked
            # output mod defeats the deactivation while looking successful).
            write_modlist(modlist, [
                ModEntry(name="Locked", enabled=True, locked=True),
                ModEntry(name="Plain", enabled=True, locked=False),
            ])
            self.assertFalse(set_mod_enabled(modlist, "Locked", False))
            self.assertTrue(set_mod_enabled(modlist, "Plain", False))
            entries = {e.name: e for e in read_modlist(modlist)}
            self.assertTrue(entries["Locked"].enabled)
            self.assertFalse(entries["Plain"].enabled)
            # A locked entry ALREADY in the requested state reports truthfully:
            # the flag IS in the requested state, nothing needed toggling.
            self.assertTrue(set_mod_enabled(modlist, "Locked", True))

            # Separators are never toggled either (never a matching entry).
            write_modlist(modlist, [
                ModEntry(name="Sep_separator", enabled=True, locked=False,
                         is_separator=True),
            ])
            self.assertFalse(set_mod_enabled(modlist, "Sep_separator", False))

    def test_set_mod_enabled_writes_only_on_change(self) -> None:
        from unittest import mock
        from Utils.mods.modlist import prepend_mod, set_mod_enabled
        with tempfile.TemporaryDirectory() as temporary:
            modlist = Path(temporary) / "modlist.txt"
            prepend_mod(modlist, "Output", enabled=True)
            with mock.patch("Utils.mods.modlist.write_modlist") as write:
                # Already in the requested state: no rewrite.
                self.assertTrue(set_mod_enabled(modlist, "Output", True))
                write.assert_not_called()
                # A real change writes exactly once.
                self.assertTrue(set_mod_enabled(modlist, "Output", False))
                write.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
