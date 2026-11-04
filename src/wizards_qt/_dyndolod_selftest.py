"""Focused self-tests for the TexGen/DynDOLOD wizard's fail-loud guards.

Run from the repository root with::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 src/wizards_qt/_dyndolod_selftest.py

Covers the guarantees a code review demanded as real oracles:
  * the deploy ABORTS when the output mod cannot be deactivated (locked
    entry) - run_deploy must not start and the old "delete the previous
    output first" guidance is surfaced;
  * the deactivate/deploy/restore dance re-enables the output mod even when
    the deploy machinery raises (try/finally), and re-enables the buttons;
  * the deploy-restore lock is held for the whole tool run and always
    released - including when the worker thread fails to start;
  * the run aborts loudly when the output mod would stay disabled (output
    installed into a disabled mod silently never deploys);
  * the "cannot scan its own previous output" guarantee is checked against
    the deployed filemap (ground truth), not a stale wizard flag.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


class _FakeGame:
    name = "Test Game"
    game_id = "skyrimspecialedition"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.game = root / "game"
        (self.game / "Data").mkdir(parents=True)
        self.staging = root / "profiles" / "mods"
        self.staging.mkdir(parents=True)
        self.profile_root = root / "profiles"
        self.filemap = self.profile_root / "filemap.txt"

    def get_mod_staging_path(self) -> Path:
        return self.staging

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging

    def get_profile_root(self) -> Path:
        return self.profile_root

    def get_game_path(self) -> Path | None:
        return self.game

    def get_effective_filemap_path(self) -> Path:
        return self.filemap


class _FakeCtx:
    def __init__(self, run_deploy=None) -> None:
        self.profile_name = "default"
        self.run_deploy = run_deploy
        self.lock_calls: list[tuple[str, bool]] = []
        self.refreshes = 0

    def current_profile(self) -> str:
        return self.profile_name

    def set_tool_lock(self, key: str, label: str, held: bool) -> None:
        self.lock_calls.append((key, held))

    def refresh_modlist(self) -> None:
        self.refreshes += 1


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _view(game: _FakeGame, ctx: _FakeCtx, logs: list[str], tool: str = "texgen"):
    from wizards_qt.dyndolod_view import DynDOLODView
    view = DynDOLODView(game, log_fn=logs.append, ctx=ctx, tool=tool)
    view._exe = Path("/fake/TexGenx64.exe")
    return view


def _wait_until(app, cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _run_tool_once(view, logs: list[str], app) -> bool:
    """Drive one worker run with the toolchain mocked out (rc=1 path)."""
    with mock.patch("Utils.executables.launch.resolve_tool_prefix",
                    return_value=(Path("/fake/proton"),
                                  Path("/fake/compat"), {})), \
            mock.patch("Utils.bethesda.xedit.prepare_xedit_prefix"), \
            mock.patch("Utils.wizards.textures."
                       "apply_discrete_gpu_environment",
                       return_value="gpu: test"), \
            mock.patch("Utils.executables.launch.run_tool_logged",
                       return_value=1), \
            mock.patch("Utils.executables.launch."
                       "shutdown_prefix_wineserver"):
        view._start_run()
        return _wait_until(
            app, lambda: any("exited with code" in line for line in logs))


def _modlist(game: _FakeGame, content: str) -> Path:
    path = game.profile_root / "profiles" / "default" / "modlist.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_deploy_aborts_when_the_output_mod_cannot_be_deactivated() -> None:
    _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        # A LOCKED output-mod entry: the deactivation cannot take effect.
        modlist = _modlist(game, "*TexGen_Output_default\n")
        logs: list[str] = []
        calls: list = []
        ctx = _FakeCtx(run_deploy=lambda done: calls.append(done) or True)
        view = _view(game, ctx, logs)
        view._start_deploy()
        assert not calls, "run_deploy must NOT start when the deactivate failed"
        assert view._deploy_btn.isEnabled() and view._skip_btn.isEnabled(), \
            "the buttons must come back after the abort"
        assert any("ABORTED" in line for line in logs), logs
        assert any("delete the previous output" in line for line in logs), logs
        # The locked entry is untouched.
        assert modlist.read_text(encoding="utf-8") == "*TexGen_Output_default\n"
    print("✓ deploy aborts when the output mod cannot be deactivated")


def test_deploy_restores_the_output_mod_when_the_deploy_raises() -> None:
    _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        modlist = _modlist(game, "+TexGen_Output_default\n")
        logs: list[str] = []

        def raising_run_deploy(_done):
            raise RuntimeError("deploy machinery exploded")

        ctx = _FakeCtx(run_deploy=raising_run_deploy)
        view = _view(game, ctx, logs)
        view._start_deploy()          # must NOT raise (caught in the slot)
        assert view._deploy_btn.isEnabled() and view._skip_btn.isEnabled(), \
            "the buttons must come back when the deploy raises"
        assert any("deploy machinery exploded" in line for line in logs), logs
        # The try/finally guarantee: the output mod is RE-ENABLED for the
        # game's normal deploys even though the deploy raised mid-dance.
        text = modlist.read_text(encoding="utf-8")
        assert "+TexGen_Output_default" in text, text
    print("✓ the output mod is restored when the deploy raises")


def test_tool_lock_is_held_for_the_run_and_always_released() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        with mock.patch("Utils.executables.launch.resolve_tool_prefix",
                        return_value=None):
            view._start_run()
            released = _wait_until(
                app, lambda: any(held is False for _key, held in ctx.lock_calls))
        assert released, f"the lock was never released: {ctx.lock_calls}"
        assert ctx.lock_calls[0][1] is True, ctx.lock_calls
        assert ctx.lock_calls[-1][1] is False, ctx.lock_calls
        assert ctx.lock_calls[0][0] == ctx.lock_calls[-1][0], ctx.lock_calls
    print("✓ the tool lock is held for the run and always released")


def test_tool_lock_is_released_when_the_thread_fails_to_start() -> None:
    _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        with mock.patch("threading.Thread.start",
                        side_effect=OSError("no threads available")):
            view._start_run()
        # start() failed: the worker - and its finally - never ran, so the
        # slot itself must have released the lock (and stayed loud).
        assert [(held) for _key, held in ctx.lock_calls] == [True, False], \
            ctx.lock_calls
        assert any("no threads available" in line for line in logs), logs
    print("✓ the tool lock is released when the thread fails to start")


def test_run_aborts_when_the_output_mod_cannot_be_enabled() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        # The enable does not take effect: output installed into a disabled
        # mod silently never deploys - abort loud.
        with mock.patch("Utils.mods.modlist.set_mod_enabled",
                        return_value=False):
            view._start_run()
        assert not ctx.lock_calls, "the run must not start at all"
        assert "never deploy" in view._run_status.text(), \
            view._run_status.text()

    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        # No modlist at all: ensure_output_mod CREATES and registers the
        # entry - "enable the mod" must never be an instruction the user
        # cannot act on.
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        with mock.patch("Utils.executables.launch.resolve_tool_prefix",
                        return_value=None):
            view._start_run()
            started = _wait_until(
                app, lambda: any(held for _key, held in ctx.lock_calls))
        assert started, "the run must start once the entry is registered"
        modlist = game.profile_root / "profiles" / "default" / "modlist.txt"
        assert "+TexGen_Output_default" in modlist.read_text(encoding="utf-8")
    print("✓ the run aborts on a failed enable and self-registers without a "
          "modlist")


def test_guarantee_follows_the_deployed_view() -> None:
    app = _app()
    # A: a legacy 'TexGen_Output' (pre-rename output mod) is deployed: the
    # guarantee is ABSENT and the old "delete the previous output first"
    # instruction is back - it must never be claimed as verified.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        game.filemap.write_text(
            "textures/old.dds\tTexGen_Output\n", encoding="utf-8")
        legacy = game.staging / "TexGen_Output"
        legacy.mkdir()
        (legacy / "old.dds").write_text("old", encoding="utf-8")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert any("ABSENT" in line for line in logs), logs
        assert any("delete the legacy 'TexGen_Output' mod once by hand" in line
                   for line in logs), logs
        assert not any("delete the previous output by hand" in line
                       for line in logs), logs
        assert not any("verified:" in line for line in logs), logs

    # B: clean and redeployed by THIS wizard -> the positive claim.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        game.filemap.write_text("textures/keep.dds\tSomeOtherMod\n",
                                encoding="utf-8")
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        view._redeployed = True
        assert _run_tool_once(view, logs, app), logs
        assert any("verified: no deployed copy" in line for line in logs), logs

    # C: clean but NOT redeployed - the filemap is the NEXT deploy's plan,
    # not the published view: UNVERIFIED, never "verified".
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        game.filemap.write_text("textures/keep.dds\tSomeOtherMod\n",
                                encoding="utf-8")
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert any("UNVERIFIED" in line for line in logs), logs
        assert not any("verified:" in line for line in logs), logs

    # D: no readable filemap at all - even with a redeploy the fourth
    # tri-state must say UNVERIFIED instead of staying silent.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        view._redeployed = True
        assert _run_tool_once(view, logs, app), logs
        assert any("UNVERIFIED" in line for line in logs), logs
    print("✓ the guarantee follows the deployed view, never a stale flag")


def test_threat_rules_for_parallel_outputs() -> None:
    _app()
    from wizards_qt.dyndolod_view import _previous_output_risk
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        modlist = _modlist(
            game, "-TexGen_Output_beta\n+TexGen_Output_gamma\n")
        # A DISABLED sibling cannot merge into this profile's view: not a
        # threat even though its folder holds another profile's live output.
        beta = game.staging / "TexGen_Output_beta"
        beta.mkdir()
        (beta / "out.bsa").write_text("beta output")
        # An ENABLED sibling merges on the next deploy: threat.
        gamma = game.staging / "TexGen_Output_gamma"
        gamma.mkdir()
        (gamma / "out.bsa").write_text("gamma output")
        # No entry at all: unknown -> fail-safe threat.
        orphan = game.staging / "TexGen_Output_orphan"
        orphan.mkdir()
        (orphan / "out.bsa").write_text("orphan output")
        # Unrelated names never match the '<base>' / '<base>_<x>' boundary.
        fix = game.staging / "TexGen_OutputFix"
        fix.mkdir()
        (fix / "f.bsa").write_text("not output")
        # The legacy base name is a threat (superseded by the per-profile mod).
        legacy = game.staging / "TexGen_Output"
        legacy.mkdir()
        (legacy / "old.bsa").write_text("legacy output")
        own, threats = _previous_output_risk(
            game, "TexGen_Output_default", "TexGen_Output", modlist)
        assert own is None, own       # no filemap: unknown, NOT zero
        assert threats == {"TexGen_Output": 0, "TexGen_Output_gamma": 0,
                           "TexGen_Output_orphan": 0}, threats
    print("✓ parallel outputs: disabled siblings and foreign names are safe")


def test_advice_never_tells_to_delete_a_parallel_output() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")        # sibling has no entry -> fail-safe threat
        beta = game.staging / "TexGen_Output_beta"
        beta.mkdir()
        (beta / "out.bsa").write_text("beta's live output")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert any("ABSENT" in line for line in logs), logs
        joined = "\n".join(logs)
        # The advice is disable-in-modlist - NEVER the destructive blanket
        # for a parallel profile's live output.
        assert "disable 'TexGen_Output_beta' in this profile's modlist" \
            in joined, joined
        assert "delete the previous output by hand" not in joined, joined
    print("✓ the advice never tells the user to delete a parallel output")


def test_legacy_scratch_sweep_never_touches_real_mods() -> None:
    app = _app()
    legacy_name = "TexGen_Output_default.build"
    # A: registered in the modlist -> a real mod, untouched.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, f"+{legacy_name}\n")
        legacy = game.staging / legacy_name
        legacy.mkdir()
        (legacy / "mod.esp").write_text("real mod")
        logs: list[str] = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert (legacy / "mod.esp").is_file(), "a registered mod was deleted"
        assert any("NOT touching it" in line for line in logs), logs
    # B: carries MO2's meta.ini -> a real mod, untouched.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        legacy = game.staging / legacy_name
        legacy.mkdir()
        (legacy / "meta.ini").write_text("[General]\n")
        (legacy / "mod.esp").write_text("real mod")
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert (legacy / "mod.esp").is_file(), "a meta.ini mod was deleted"
        assert any("NOT touching it" in line for line in logs), logs
    # C: bare legacy scratch -> removed, with a log.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        _modlist(game, "")
        legacy = game.staging / legacy_name
        legacy.mkdir()
        (legacy / "partial.bsa").write_text("scratch")
        logs = []
        ctx = _FakeCtx()
        view = _view(game, ctx, logs)
        assert _run_tool_once(view, logs, app), logs
        assert not legacy.exists(), "legacy scratch was not removed"
        assert any("removing the legacy scratch folder" in line
                   for line in logs), logs
    print("✓ the legacy sweep never deletes a real mod")



def main() -> None:
    _app()
    test_deploy_aborts_when_the_output_mod_cannot_be_deactivated()
    test_deploy_restores_the_output_mod_when_the_deploy_raises()
    test_tool_lock_is_held_for_the_run_and_always_released()
    test_tool_lock_is_released_when_the_thread_fails_to_start()
    test_run_aborts_when_the_output_mod_cannot_be_enabled()
    test_guarantee_follows_the_deployed_view()
    test_threat_rules_for_parallel_outputs()
    test_advice_never_tells_to_delete_a_parallel_output()
    test_legacy_scratch_sweep_never_touches_real_mods()
    print("All DynDOLOD wizard self-tests passed.")


if __name__ == "__main__":
    main()
