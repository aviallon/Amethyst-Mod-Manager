"""TexGen / DynDOLOD / xLODGen wizards - Qt port of wizards/dyndolod.py.

One view serves all three tools (``tool="texgen" | "dyndolod" | "xlodgen"``);
TexGen and DynDOLOD ship in the same DynDOLOD archive (manual Nexus download),
xLODGen auto-downloads the latest release from GitHub.

Steps (plugins-panel-scoped tab; install steps are skipped when the exe is
already extracted under Profiles/<game>/Applications/<app_dir>/):
  1. Download - manual Nexus page → locate in ~/Downloads → extract, OR
     (xLODGen) auto-download from GitHub with progress → extract.
  2. Deploy the modlist (explicit Deploy button through
     QtWizardContext.run_deploy). The per-profile output mod
     <Tool>_Output_<profile> is created/enabled automatically (the BodySlide
     ensure_output_mod pattern) and deactivated for this deploy only, so the
     tool cannot scan its own previous output - this replaces the old
     "delete the previous output mod by hand" reminder. The old output files
     are kept on disk until a successful run replaces them. If the mod cannot
     be deactivated (e.g. a locked entry) the deploy is ABORTED with the old
     "delete the previous output first" guidance - the wizard never claims
     the guarantee when it is not in effect. Skipping the deploy gives NO
     guarantee for that run and the run log says so plainly.
  3. Choose Proton version + prefix placement (shared ProtonStepWidget).
  4. Run the tool via Proton with ``-d:<game>/Data``
     ``-o:<staging>/.amethyst-build/<mod>.build`` (a clean build folder
     under the reserved scratch root) and its game-mode flag after prefix prep
     (registry seed, plugins.txt + My Games links). The enforcement is the
     temporary deactivation, NOT a namespace mask: Bethesda mods merge into
     Data/, so a per-mod mask cannot hide anything there. The deploy lock is
     held for the whole run so no deploy can re-merge under the running tool.
     On exit code 0 the build folder is swapped into the output mod with
     whole-directory atomic renames (old content kept on any failure/crash);
     a failed run's partial output is discarded (logged), a complete output
     whose install failed is kept for a retry, and the build folder never
     lingers as a mod (the modlist sync skips wizard scratch names). The
     wizard closes and refreshes the modlist when the tool exits.

All blocking work happens on daemon threads with Signals marshalling back to
the UI thread; the portal file picker's callback also fires on a worker
thread and is marshalled via Signal.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QStackedWidget,
)

from gui_qt.theme_qt import (
    active_palette, _c, button_qss, close_button, ok_text, err_text,
)
from gui_qt.safe_emit import safe_emit
from wizards_qt import release_on_main
from Utils.bethesda.xedit import tool_exe_path
from Utils.executables.arguments import XLODGEN_GAME_FLAGS

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/68518?tab=files"
_XLODGEN_GITHUB_API = "https://api.github.com/repos/sheson/xLODGen/releases/latest"

# tool id → (title, exe, output mod base name, app dir, archive keyword,
# auto-download)
_TOOLS = {
    "texgen":   ("TexGen",   "TexGenx64.exe",   "TexGen_Output",   "DynDOLOD", "dyndolod", False),
    "dyndolod": ("DynDOLOD", "DynDOLODx64.exe", "DynDOLOD_Output", "DynDOLOD", "dyndolod", False),
    "xlodgen":  ("xLODGen",  "xLODGenx64.exe",  "xLODGen_Output",  "xLODGen",  "xlodgen",  True),
}

_PG_DL_MANUAL, _PG_DL_AUTO, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(7)


def _looks_like_real_mod(path: Path, modlist_path: Path) -> bool:
    """Whether *path* is a real mod the wizard must never delete: registered
    in the profile's modlist or carrying MO2's meta.ini marker."""
    try:
        from Utils.mods.modlist import read_modlist
        if any(entry.name == path.name
               for entry in read_modlist(modlist_path)):
            return True
    except Exception:
        pass
    return (path / "meta.ini").exists()


def _previous_output_risk(game, output_mod_name: str, base_name: str,
                          modlist_path: Path
                          ) -> "tuple[int | None, dict[str, int]]":
    """Ground truth for "could the tool scan its own previous output?".

    Returns ``(own_deployed_rows, threats)``:
      * ``own_deployed_rows``: deployed filemap rows owned by the output mod
        (None when there is no readable filemap - unknown, NOT zero);
      * ``threats``: every OTHER producer still threatening THIS profile's
        view, name -> deployed rows (0 = folder only):
        - deployed filemap rows from a legacy ``<base>`` install (output mods
          from before the per-profile rename) or a ``<base>_<x>`` sibling
          (any profile's output) - rows mean it merged at plan time;
        - non-empty sibling folders in staging, BUT only when the sibling is
          not DISABLED in this profile's modlist: a disabled entry proves the
          folder cannot merge into this profile's view, and flagging a
          sibling output the user must keep (another profile's live
          ``<base>_<profile>``) would make the guarantee unreachable and its
          advice destructive. No entry at all stays a threat (unknown ->
          fail-safe).
        The name boundary is exact: ``<base>`` or ``<base>_<x>`` - an
        unrelated mod like ``<base>Fix`` never matches.

    The filemap alone is not the published view - it is regenerated as the
    NEXT deploy's plan - so callers may treat "0 rows" as a positive claim
    only together with a redeploy done in this wizard.
    """
    def _is_sibling(name: str) -> bool:
        return name == base_name or name.startswith(base_name + "_")

    rows: dict[str, int] = {}
    seen = False
    getter = getattr(game, "get_effective_filemap_path", None)
    filemap = getter() if callable(getter) else None
    try:
        if filemap is not None and filemap.is_file():
            seen = True
            with filemap.open(encoding="utf-8", errors="surrogateescape") as fh:
                for line in fh:
                    _rel, sep, mod_name = line.rstrip("\n").partition("\t")
                    if sep:
                        rows[mod_name] = rows.get(mod_name, 0) + 1
    except OSError:
        seen = False
        rows = {}
    own = rows.get(output_mod_name, 0) if seen else None
    threats: dict[str, int] = {}
    for owner, count in rows.items():
        if owner != output_mod_name and _is_sibling(owner):
            threats[owner] = count
    try:
        from Utils.mods.modlist import read_modlist
        entries = {entry.name: entry for entry in read_modlist(modlist_path)}
    except Exception:
        entries = {}
    # Staging scan: non-empty sibling folders (the legacy base name included)
    # merge on the next deploy even when the filemap does not list them yet -
    # except ones this profile's modlist keeps DISABLED: they cannot be in
    # this profile's view, and another profile's live output must never be
    # flagged for deletion.
    staging_getter = getattr(game, "get_effective_mod_staging_path", None)
    staging = staging_getter() if callable(staging_getter) else None
    if staging is not None:
        try:
            candidates = list(staging.iterdir())
        except OSError:
            candidates = []
        for candidate in candidates:
            name = candidate.name
            if name == output_mod_name or not _is_sibling(name):
                continue
            entry = entries.get(name)
            if entry is not None and not entry.enabled:
                continue        # disabled here: cannot merge into this view
            try:
                non_empty = candidate.is_dir() and any(candidate.iterdir())
            except OSError:
                non_empty = True    # cannot inspect: assume it threatens
            if non_empty:
                threats.setdefault(name, 0)
    return own, threats


class DynDOLODView(QWidget):
    """Install (if needed), deploy and run TexGen / DynDOLOD / xLODGen."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)        # percent 0-100
    _dl_done_sig = Signal(bool)
    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # tool launched → enable Done
    _run_finished_sig = Signal()          # tool exited → close + refresh

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "texgen", **_extra):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx

        (self._name, self._exe_name, self._output_dir, self._app_dir,
         self._archive_kw, self._auto_dl) = _TOOLS[tool]
        self._tool_id = tool

        self._exe = tool_exe_path(game, self._exe_name, self._app_dir)
        self._archive_path: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefer_discrete_gpu = False
        self._proton_step = None
        self._ran = False
        self._redeployed = False
        self._closing = False

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        self._dl_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(_guard(self._on_dl_progress))
        self._dl_done_sig.connect(_guard(self._on_dl_done))
        self._locate_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(_guard(self._on_picked))
        self._extract_done_sig.connect(_guard(self._on_extract_done))
        self._run_started_sig.connect(_guard(self._on_run_started))
        self._run_finished_sig.connect(_guard(self._finish))

        self.setObjectName("DynDOLODView")
        self._build()

    # ---- layout -------------------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("{0} - {1}").format(self._name, self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = close_button(self.tr("✕ Close"), pal=p)
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        self._stack.addWidget(self._build_step_dl_manual())  # 0
        self._stack.addWidget(self._build_step_dl_auto())    # 1
        self._stack.addWidget(self._build_step_locate())     # 2
        self._stack.addWidget(self._build_step_extract())    # 3
        self._stack.addWidget(self._build_step_deploy())     # 4
        self._stack.addWidget(self._build_step_proton())     # 5
        self._stack.addWidget(self._build_step_run())        # 6

        # Already extracted → straight to the deploy step (Tk behaviour).
        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
        elif self._auto_dl:
            self._goto_step(_PG_DL_AUTO)
        else:
            self._stack.setCurrentIndex(_PG_DL_MANUAL)

    def _step_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return page, lay

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _accent_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            button_qss("BTN_INFO"))
        return b

    # ---- step 1a: manual download (TexGen / DynDOLOD) ---------------------------
    def _build_step_dl_manual(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download DynDOLOD"))
        note = QLabel(
            self.tr("Click the button below to open the DynDOLOD page on Nexus "
            "Mods.\n\nDownload the archive manually (do NOT use the Mod "
            "Manager download button), then click Next."))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        lay.addSpacing(8)

        open_btn = QPushButton(self.tr("Open Download Page"))
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            button_qss("BTN_WARN"))
        open_btn.clicked.connect(self._open_download_page)
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_LOCATE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_download_page(self):
        from Utils.environment.xdg import open_url
        open_url(_NEXUS_URL)

    # ---- step 1b: auto download (xLODGen) ---------------------------------------
    def _build_step_dl_auto(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download {0}").format(self._name))
        self._dl_status = self._make_status(lay)
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(0)
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)
        lay.addStretch(1)
        return page

    def _start_auto_download(self):
        game, exe_name, app_dir = self._game, self._exe_name, self._app_dir
        name = self._name

        def worker():
            import tempfile
            from Utils.ca_bundle import download_file
            from Utils.wizards.archives import (
                extract_archive, fetch_latest_github_asset,
            )
            from Utils.bethesda.xedit import applications_dir, flatten_subdirs
            try:
                safe_emit(self._dl_status_sig,
                    self.tr("Fetching latest release from GitHub…"), "")
                tag, dl_url = fetch_latest_github_asset(
                    _XLODGEN_GITHUB_API, ["xlodgen"])
                safe_emit(self._dl_status_sig, self.tr("Downloading {0}…").format(tag), "")
                self._log(f"{name} Wizard: downloading {tag} from {dl_url}")

                suffix = Path(dl_url).suffix or ".7z"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                def _reporthook(block_num, block_size, total_size):
                    if total_size > 0:
                        pct = min(100, block_num * block_size * 100 / total_size)
                        safe_emit(self._dl_progress_sig, int(pct))

                download_file(dl_url, tmp_path, reporthook=_reporthook)
                safe_emit(self._dl_progress_sig, 100)
                self._log(f"{name} Wizard: download complete, extracting…")
                safe_emit(self._dl_status_sig, self.tr("Extracting…"), "")

                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)
                paths = extract_archive(tmp_path, dest)
                tmp_path.unlink(missing_ok=True)

                file_count = len([p for p in paths if p.is_file()])
                flatten_subdirs(dest, exe_name)

                if not (dest / exe_name).is_file():
                    raise RuntimeError(self.tr("{0} not found after extraction.").format(exe_name))
                self._exe = dest / exe_name

                self._log(f"{name} Wizard: extracted {file_count} file(s).")
                safe_emit(self._dl_status_sig,
                    self.tr("Downloaded and extracted {0}.").format(tag), ok_text())
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                safe_emit(self._dl_status_sig, self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: download error: {exc}")
                safe_emit(self._dl_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="dyndolod-download").start()

    def _on_dl_progress(self, pct: int):
        self._dl_bar.setValue(pct)

    def _on_dl_done(self, ok: bool):
        if ok:
            QTimer.singleShot(
                500, lambda: None if self._closing
                else self._goto_step(_PG_DEPLOY))

    # ---- step 2: locate ---------------------------------------------------------
    def _build_step_locate(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Locate the Archive"))
        self._locate_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_archive)
        rh.addWidget(browse)
        retry = QPushButton(self.tr("Try Again"))
        retry.setCursor(Qt.PointingHandCursor)
        retry.clicked.connect(self._scan_downloads)
        rh.addWidget(retry)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _scan_downloads(self):
        from Utils.wizards.archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), [self._archive_kw])
        if found:
            self._archive_path = found
            self._set_status(self._locate_status, self.tr("Found: {0}").format(found.name), ok_text())
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))
        else:
            self._archive_path = None
            self._set_status(
                self._locate_status,
                self.tr("DynDOLOD archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually."),
                err_text())

    def _browse_archive(self):
        from Utils.ui.portal import pick_file
        # Portal callback fires on a WORKER thread - marshal via Signal.
        pick_file(self.tr("Select the DynDOLOD archive"), lambda *a: safe_emit(self._picked_sig, *a))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._locate_status,
                             self.tr("Selected: {0}").format(self._archive_path.name), ok_text())
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))

    # ---- step 3: extract --------------------------------------------------------
    def _build_step_extract(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Extract DynDOLOD"))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _start_extract(self):
        archive, game = self._archive_path, self._game
        exe_name, app_dir, name = self._exe_name, self._app_dir, self._name

        def worker():
            from Utils.wizards.archives import extract_archive
            from Utils.bethesda.xedit import applications_dir, flatten_subdirs
            try:
                if archive is None or not archive.is_file():
                    raise RuntimeError(self.tr("Archive not found."))
                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)

                safe_emit(self._extract_status_sig, self.tr("Extracting {0}…").format(archive.name), "")
                self._log(f"{name} Wizard: extracting {archive.name} → {dest}")

                paths = extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"{name} Wizard: extracted {file_count} file(s).")

                flatten_subdirs(dest, exe_name)

                exe = dest / exe_name
                if not exe.is_file():
                    raise RuntimeError(
                        self.tr("{0} not found after extraction.\n"
                        "Check that the archive contains {0}.").format(exe_name))
                self._exe = exe

                safe_emit(self._extract_status_sig,
                    self.tr("Extracted {0} file(s).").format(file_count), ok_text())
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig, self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="dyndolod-extract").start()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- step 4: deploy ---------------------------------------------------------
    def _profile(self) -> str:
        # ctx.profile_name is frozen at wizard open; a profile switch mid-wizard
        # must not deactivate the wrong profile's modlist (the deploy targets
        # the LIVE profile). Read the live name; fall back to the frozen one.
        current = getattr(self._ctx, "current_profile", None)
        if callable(current):
            try:
                live = current()
            except Exception:
                live = None
            if live:
                return live
        return getattr(self._ctx, "profile_name", None) or "default"

    def _output_mod_name(self) -> str:
        from Utils.bethesda.xedit import per_profile_output_mod_name
        return per_profile_output_mod_name(self._output_dir, self._profile())

    def _modlist_path(self) -> Path:
        return (self._game.get_profile_root() / "profiles"
                / self._profile() / "modlist.txt")

    def _ensure_output_mod(self) -> bool:
        """BodySlide pattern: create + enable the per-profile output-capture
        mod BEFORE the deploy so the output has a registered home in the load
        order. Force-enables an existing entry too: a crashed deploy may have
        left the mod deactivated. Returns True when the mod is ENABLED
        afterwards - output installed into a disabled mod silently never
        deploys, so callers must abort on False, not just warn."""
        from Utils.bethesda.bodyslide import ensure_output_mod
        name = self._output_mod_name()
        try:
            ensure_output_mod(self._game, self._profile(), name)
        except Exception as exc:
            self._log(f"{self._name} Wizard: could not create '{name}': {exc}")
            return False
        self._ran = True       # modlist gained a mod - refresh on close
        return self._set_output_mod_enabled(True)

    def _set_output_mod_enabled(self, enabled: bool) -> bool:
        """Set the output mod's enabled flag and report whether the requested
        state is ACTUALLY in effect. False (locked entry, missing entry,
        failed write) means "not enforced" - callers enforcing a state must
        abort instead of trusting the request."""
        from Utils.mods.modlist import read_modlist, set_mod_enabled
        name = self._output_mod_name()
        try:
            in_effect = set_mod_enabled(self._modlist_path(), name, enabled)
        except Exception as exc:
            self._log(f"{self._name} Wizard: could not update '{name}': {exc}")
            return False
        if in_effect:
            verb = "enabled" if enabled else (
                "deactivated for this deploy (previous output kept on disk)")
            self._log(f"{self._name} Wizard: output mod '{name}' {verb}.")
            return True
        reason = "its flag did not change"
        try:
            entries = [entry for entry in read_modlist(self._modlist_path())
                       if entry.name == name]
            if not entries:
                reason = "it has no modlist entry"
            elif entries[0].locked:
                reason = "its modlist entry is locked ('*') and cannot be toggled"
        except Exception:
            pass
        self._log(f"{self._name} Wizard: WARNING: output mod '{name}' is NOT "
                  f"{'enabled' if enabled else 'deactivated'} - {reason}.")
        return False

    def _build_step_deploy(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Deploy Modlist"))
        note = QLabel(
            self.tr("Output is captured by the '{1}' mod. The previous output\n"
                    "stays until a successful run replaces it automatically.\n\n"
                    "For this deploy the output mod is deactivated so {0} does\n"
                    "not scan its own earlier output. (Skip has no such\n"
                    "guarantee. If an older '{2}' output mod from a previous\n"
                    "version is still installed, delete it once by hand.)\n\n"
                    "Click Deploy when you are ready.").format(
                        self._name, self._output_mod_name(), self._output_dir))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip"))
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(self._skip_deploy)
        rh.addWidget(self._skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_deploy)
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _skip_deploy(self):
        self._ensure_output_mod()
        # Honest about what skipping costs: with no fresh deploy the tool
        # reads whatever game view is already published, which may still hold
        # an older output - the "cannot scan its own previous output"
        # guarantee is ABSENT for this run (not just weakened).
        self._log(f"{self._name} Wizard: deploy skipped - no fresh game view "
                  f"is built, so the guarantee that {self._name} cannot scan "
                  "its own previous output is ABSENT for this run.")
        self._goto_step(_PG_PROTON)

    def _tool_lock_setter(self):
        """Return a ``(held: bool) -> None`` callable that holds/releases this
        tool's deploy-restore lock. Call it on the GUI thread.

        Mirrors TextureToolView._tool_lock_setter: it closes over the ctx hook
        and plain strings ONLY - never over ``self`` - because the worker's
        release must still land if the user closed the tab mid-run."""
        hook = getattr(self._ctx, "set_tool_lock", None)
        key, label = f"dyndolod:{self._tool_id}", self._name
        log = self._log
        if hook is None:
            return lambda _held: None

        def _set(held: bool):
            try:
                hook(key, label, held)
            except Exception as exc:
                log(f"{label} Wizard: tool lock failed: {exc}")
        return _set

    def _start_deploy(self):
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(self._deploy_status,
                             self.tr("Deploy is unavailable here."), err_text())
            return
        self._deploy_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        self._set_status(self._deploy_status, self.tr("Deploying…"), "")
        try:
            self._deploy_with_output_mod_disabled(run_deploy)
        except Exception as exc:
            # Non-OSError included: a stuck-disabled Deploy/Skip pair would
            # strand the wizard. Loud (status + log) but recoverable.
            self._set_status(self._deploy_status,
                             self.tr("Error: {0}").format(exc), err_text())
            self._log(f"{self._name} Wizard: error: {exc}")
            self._deploy_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)

    def _deploy_with_output_mod_disabled(self, run_deploy) -> None:
        # BodySlide pattern: materialize the output-capture mod (modlist entry
        # + staging folder) BEFORE the deploy so the output has a registered
        # home in the load order.
        self._ensure_output_mod()
        # DynDOLOD's documented requirement: "Always deactivate or remove
        # older installed output first." Deactivating the output mod for this
        # deploy keeps the previous output out of the rebuilt game view, so
        # the tool cannot scan (and regenerate from) its own earlier files.
        # The files themselves are NOT deleted: a failed run keeps them.
        # If the deactivation does NOT take effect (e.g. a locked entry) the
        # deploy must not run: the old output would silently re-merge into
        # Data/ and every log line about the guarantee would be a lie.
        name = self._output_mod_name()
        if not self._set_output_mod_enabled(False):
            self._set_status(
                self._deploy_status,
                self.tr("Could not deactivate the output mod '{0}' - its "
                        "modlist entry is locked or missing.\n"
                        "Unlock it, or delete its previous output by hand\n"
                        "(the old manual way), then click Deploy again.").format(name),
                err_text())
            self._log(f"{self._name} Wizard: deploy ABORTED: output mod "
                      f"'{name}' stays active, so {self._name} could scan its "
                      "own previous output. Unlock the entry (or delete the "
                      f"previous output from '{name}') and deploy again.")
            self._deploy_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)
            return

        restored = False

        def _restore_output_mod():
            # Exactly once: the async deploy callback owns the restore when
            # the deploy actually started, the sync paths own it otherwise.
            nonlocal restored
            if restored:
                return
            restored = True
            self._set_output_mod_enabled(True)

        def _done(ok: bool):
            # Fired on the UI thread by the app's deploy completion handler.
            # Restore the modlist entry first: the output mod must be enabled
            # for the game's next deploy even when this deploy failed.
            _restore_output_mod()
            self._redeployed = self._redeployed or ok
            if self._closing:
                return
            if ok:
                self._set_status(self._deploy_status, self.tr("Deploy complete."), ok_text())
                self._goto_step(_PG_PROTON)
            else:
                self._set_status(self._deploy_status,
                                 self.tr("Deploy failed - see log."), err_text())
                self._deploy_btn.setEnabled(True)
                self._skip_btn.setEnabled(True)

        deploy_owns_restore = False
        try:
            started = run_deploy(_done)
            deploy_owns_restore = started
        finally:
            # try/finally, not except: a raise anywhere in this dance must
            # leave the output mod re-enabled for the game's normal deploys.
            # When the deploy took ownership (_done fires later on the UI
            # thread) the restore stays with _done.
            if not deploy_owns_restore:
                _restore_output_mod()
        if not started:
            self._set_status(self._deploy_status,
                             self.tr("Could not start deploy - see log."), err_text())
            self._deploy_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)

    # ---- step 5: proton -----------------------------------------------------------
    def _build_step_proton(self) -> QWidget:
        self._proton_holder = QWidget()
        QVBoxLayout(self._proton_holder).setContentsMargins(0, 0, 0, 0)
        return self._proton_holder

    def _enter_proton(self):
        # Built lazily on entry: the exe may only exist after the extract step.
        lay = self._proton_holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._exe is None:
            err = QLabel(
                self.tr('{0} was not found.\nPlease restart the wizard and install {1} first.').format(self._exe_name, self._app_dir))
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        self._proton_step = ProtonStepWidget(
            self._game, self._exe, self._exe_name, self._name,
            on_continue=self._on_proton_chosen,
            log_fn=self._log,
            title=self.tr("Step 5: Choose Proton Version"),
            show_discrete_gpu=self._tool_id in ("texgen", "dyndolod"),
            wizard_id=getattr(self._ctx, "wizard_tool_id", ""),
            wizard_label=getattr(self._ctx, "wizard_tool_label", ""),
            wizard_label_args=getattr(
                self._ctx, "wizard_tool_label_args", ()),
        )
        lay.addWidget(self._proton_step)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._prefer_discrete_gpu = bool(
            self._proton_step is not None
            and self._proton_step.prefer_discrete_gpu()
        )
        self._goto_step(_PG_RUN)

    # ---- step 6: run ----------------------------------------------------------------
    def _build_step_run(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 6: Run {0}").format(self._name))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setEnabled(False)
        self._done_btn.setStyleSheet(
            button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _start_run(self):
        try:
            self._start_run_checked()
        except Exception as exc:
            # Non-OSError included: an unhandled raise here would leave the
            # run step dead with no explanation. Loud (status + log).
            self._set_status(self._run_status,
                             self.tr("Error: {0}").format(exc), err_text())
            self._log(f"{self._name} Wizard: error: {exc}")

    def _start_run_checked(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(self._exe_name), err_text())
            return
        self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name), "")
        name = self._name
        output_mod_name = self._output_mod_name()
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        prefer_discrete_gpu = self._prefer_discrete_gpu
        tool_id = self._tool_id
        game_flag = (XLODGEN_GAME_FLAGS.get(game.game_id)
                     if tool_id == "xlodgen" else "-sse")

        if game_flag is None:
            self._set_status(
                self._run_status,
                self.tr("xLODGen does not support {0}.").format(game.name),
                err_text())
            return

        # The generated output must land in an ENABLED mod or the LOD silently
        # never deploys. Force-enable and VERIFY right before the run - abort
        # loud otherwise (a locked or missing entry is not an excuse).
        if not self._ensure_output_mod():
            self._set_status(
                self._run_status,
                self.tr("The output mod '{0}' is not enabled - {1} output "
                        "would never deploy.\nEnable or unlock the mod, then "
                        "run again.").format(
                            self._output_mod_name(), self._name),
                err_text())
            return

        # Hold the deploy-restore lock for the whole tool run: the tool reads
        # the deployed game view for its whole session, and a user deploy
        # mid-run would re-merge files under the running tool. Released in the
        # worker's finally via release_on_main (texture_tool pattern).
        modlist_path = self._modlist_path()
        set_lock = self._tool_lock_setter()
        set_lock(True)

        def worker():
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            from Utils.wine.paths import to_wine_path
            from Utils.bethesda.xedit import (
                output_mod_build_dir, prepare_xedit_prefix,
                recover_swap_leftovers, swap_output_into_mod,
            )
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            proton_script = compat_data = None
            build_dir: Path | None = None
            keep_build = False
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not resolve the Proton version for the "
                            "game's own prefix - launch the game once, or pick a "
                            "different prefix option."), err_text())
                    else:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not find Proton '{0}' - check that it "
                            "is installed in Steam, Heroic or ProtonPlus.").format(
                                proton_name), err_text())
                    return
                proton_script, compat_data, env = result

                if tool_id in ("texgen", "dyndolod"):
                    # These tools spawn Texconvx64.exe themselves. Child
                    # processes inherit this environment, so filtering the
                    # Vulkan devices here makes the selected discrete GPU DXVK
                    # adapter 0 for texconv's DirectCompute codec as well.
                    from Utils.wizards.textures import apply_discrete_gpu_environment
                    gpu_selection = apply_discrete_gpu_environment(
                        env, prefer_discrete_gpu)
                    _wlog(f"GPU: {gpu_selection}")

                game_path = game.get_game_path()
                if game_path is None:
                    safe_emit(self._run_status_sig, self.tr("Game path not configured."), err_text())
                    return

                staging = game.get_effective_mod_staging_path()
                mod_dir = staging / output_mod_name
                build_dir = output_mod_build_dir(staging, output_mod_name)
                # Crash recovery FIRST: a leftover swap backup may hold the
                # only complete copy of the previous output.
                recover_swap_leftovers(build_dir, mod_dir, log_fn=_wlog)
                # Migrate the pre-namespace scratch location (staging side):
                # recover any legacy backup, then remove the legacy build dir.
                # NEVER touch anything that is a real mod (registered in the
                # modlist or carrying meta.ini): since ".build" names are
                # legitimate mod names now, blind deletion would destroy one.
                legacy_build = staging / f"{output_mod_name}.build"
                legacy_backup = staging / f"{output_mod_name}.build.previous"
                if legacy_backup.exists() or legacy_build.exists():
                    if legacy_backup.exists() and _looks_like_real_mod(
                            legacy_backup, modlist_path):
                        _wlog(f"NOTE: '{legacy_backup.name}' is a registered "
                              "mod (modlist entry or meta.ini) - NOT using it "
                              "as a swap backup.")
                    else:
                        recover_swap_leftovers(legacy_build, mod_dir,
                                               log_fn=_wlog)
                    if legacy_build.is_dir():
                        if _looks_like_real_mod(legacy_build, modlist_path):
                            _wlog(f"NOTE: '{legacy_build.name}' is a "
                                  "registered mod (modlist entry or "
                                  "meta.ini) - NOT touching it; delete it by "
                                  "hand only if it truly is legacy scratch.")
                        else:
                            _wlog(f"NOTE: removing the legacy scratch folder "
                                  f"'{legacy_build.name}' (scratch now lives "
                                  "under '.amethyst-build/').")
                            try:
                                shutil.rmtree(legacy_build)
                            except OSError as exc:
                                _wlog(f"WARN: could not remove "
                                      f"'{legacy_build}': {exc}")
                            if legacy_build.exists():
                                _wlog(f"WARN: '{legacy_build}' still exists "
                                      "after the removal attempt - remove it "
                                      "by hand.")
                # The tool must start from a clean folder: a crashed previous
                # run can leave partial output behind ("If generating from
                # scratch, delete all old files in the output folder first" -
                # DynDOLOD docs). The mod itself keeps its old content until a
                # successful run replaces it. A leftover folder here can also
                # be output KEPT after a failed install - log its removal so
                # "kept for a retry" cannot die in silence.
                if build_dir.exists():
                    _wlog(f"NOTE: discarding the leftover scratch folder "
                          f"'{build_dir}' - a kept output from a failed "
                          "install must be recovered by hand BEFORE running "
                          "this wizard again.")
                    shutil.rmtree(build_dir)
                build_dir.mkdir(parents=True)

                pfx = compat_data / "pfx"
                data_arg = f'-d:{to_wine_path(game_path / "Data", pfx)}'
                output_arg = f'-o:{to_wine_path(build_dir, pfx)}'

                # xEdit-based tools read the game's Installed Path from the
                # registry, the load order from plugins.txt in AppData and
                # the game INIs from My Games - a fresh tool prefix has none
                # of them (no viewsettings/WinXP seeding for these tools).
                prepare_xedit_prefix(
                    game, compat_data, proton_script, env, log_fn=_wlog)

                self._log(f"{name} Wizard: launching {exe} via Proton")
                self._log(f"  args: {data_arg}  {output_arg}  {game_flag}")
                # The "cannot scan its own previous output" guarantee is
                # checked against ground truth - deployed filemap rows AND
                # non-empty <base>* output folders - never a stale wizard
                # flag. The filemap is only the NEXT deploy's plan, so a zero
                # count is a positive claim only when this wizard redeployed.
                own_rows, threats = _previous_output_risk(
                    game, output_mod_name, self._output_dir, modlist_path)
                if own_rows or threats:
                    parts = []
                    advice = []
                    if own_rows:
                        parts.append(f"{own_rows} deployed file(s) of "
                                     f"'{output_mod_name}'")
                        advice.append(
                            f"redeploy with '{output_mod_name}' deactivated "
                            "(this wizard's Deploy button does that)")
                    for threat_name in sorted(threats):
                        threat_rows = threats[threat_name]
                        rows_note = (f" ({threat_rows} deployed file(s))"
                                     if threat_rows else "")
                        if threat_name == self._output_dir:
                            parts.append(f"legacy output '{threat_name}'"
                                         + rows_note)
                            # A legacy <base> output mod is superseded by the
                            # per-profile one - deleting it once is the
                            # documented migration step.
                            advice.append(f"delete the legacy '{threat_name}' "
                                          "mod once by hand")
                        else:
                            parts.append(f"output '{threat_name}' from "
                                         "another profile or version"
                                         + rows_note)
                            # NEVER a bare "delete the previous output" for a
                            # parallel profile's live mod - that would destroy
                            # output the user must keep.
                            advice.append(
                                f"disable '{threat_name}' in this profile's "
                                "modlist (or delete it once by hand if it is "
                                f"a legacy '{self._output_dir}' output)")
                    _wlog("WARNING: the current game view still carries "
                          + ", ".join(parts)
                          + f" - the guarantee that {name} cannot scan its "
                          "own previous output is ABSENT for this run. "
                          + "; ".join(advice) + ".")
                elif own_rows == 0 and self._redeployed:
                    _wlog(f"verified: no deployed copy of '{output_mod_name}' "
                          f"is in the game view - {name} cannot scan its own "
                          "previous output.")
                else:
                    # No hits, but no redeploy in this wizard (or no readable
                    # filemap): the filemap is the next deploy's plan, not the
                    # published view - the guarantee is UNVERIFIED, not given.
                    _wlog(f"WARNING: the guarantee that {name} cannot scan "
                          "its own previous output is UNVERIFIED for this run "
                          "(no redeploy in this wizard and/or no deployed "
                          "filemap to confirm against).")
                # No per-path namespace mask: Bethesda mods MERGE into Data/,
                # so a 'Data/<mod>' mask cannot hide the previous output there
                # (and masking a path that never exists would only leave a
                # stray dir in the view). The enforcement is the temporary
                # output-mod deactivation around the deploy.
                safe_emit(self._run_started_sig)
                rc = run_tool_logged(
                    proton_script, exe, env, log_fn=_wlog,
                    extra_args=[data_arg, output_arg, game_flag], label=name,
                    game=game, owner=self)

                if rc == 0:
                    # Swap the finished output into the output mod only on
                    # success - a crashed/failed run keeps the previous output
                    # intact instead of leaving a broken mix behind. The swap
                    # consumes the build dir (whole-directory rename).
                    if not any(build_dir.iterdir()):
                        _wlog(f"{name} finished successfully (exit code 0) "
                              "but the output is empty - the tool ran fine, "
                              "there is just nothing to install; keeping the "
                              f"previous '{output_mod_name}' content.")
                    else:
                        try:
                            installed = swap_output_into_mod(
                                build_dir, mod_dir, log_fn=_wlog)
                        except Exception as exc:
                            # The LAUNCH succeeded - only the output install
                            # failed. Say so; "launch error" would lie. The
                            # complete new output is kept for a manual retry.
                            keep_build = True
                            self._log(f"{name} Wizard: {exe.name} closed.")
                            self._log(
                                f"{name} Wizard: OUTPUT INSTALL FAILED: {exc} "
                                f"- the previous '{output_mod_name}' content "
                                f"is kept and the complete new output stays "
                                f"in '{build_dir}' - recover it by hand BEFORE "
                                "running this wizard again.")
                            safe_emit(
                                self._run_status_sig,
                                self.tr("Output install failed: {0}").format(exc),
                                err_text())
                            return
                        # The output mod changed on disk; flag the profile so
                        # the next deploy reconciles the catalog.
                        from wizards_qt import notify_wizard_output
                        notify_wizard_output(self._ctx, f"{name} wrote output",
                                             self._log)
                        self._log(f"{name} Wizard: installed {installed} "
                                  f"output item(s) into '{output_mod_name}'.")
                else:
                    partial = (sum(1 for _ in build_dir.iterdir())
                               if build_dir.is_dir() else 0)
                    _wlog(f"{name} exited with code {rc} - keeping the "
                          f"previous '{output_mod_name}' content and "
                          f"discarding the partial output ({partial} item(s) "
                          f"in '{build_dir.name}').")
                self._log(f"{name} Wizard: {exe.name} closed.")
                safe_emit(self._run_status_sig, self.tr("{0} finished.").format(name), ok_text())
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: launch error: {exc}")
            finally:
                # The lock release is the OUTERMOST finally: everything below
                # can raise, and a lock left held disables Deploy/Restore for
                # the rest of the session.
                try:
                    # The scratch build dir must never survive the run: the
                    # next modlist sync would adopt '<name>.build' as a junk
                    # DISABLED entry that flaps on every run (it is skipped as
                    # a scratch name now, but leaving it is still litter). A
                    # successful swap already consumed it (whole-directory
                    # rename); this catches the failed/empty/exceptional
                    # cases. Deliberate decision: a PARTIAL output from a
                    # failed run is DISCARDED (logged above) - it is unusable
                    # half-written data and the previous output stays
                    # authoritative; a COMPLETE output whose install failed is
                    # KEPT (keep_build) for a manual retry.
                    if (build_dir is not None and build_dir.is_dir()
                            and not keep_build):
                        shutil.rmtree(build_dir, ignore_errors=True)
                        if build_dir.exists():
                            _wlog(f"WARN: could not remove the scratch folder "
                                  f"'{build_dir}' - delete it by hand.")
                    # In finally: a tool that crashed is exactly when Proton
                    # sidecars are most likely to be left holding the prefix.
                    if proton_script is not None and compat_data is not None:
                        shutdown_prefix_wineserver(proton_script, compat_data,
                                                   log_fn=_wlog)
                finally:
                    release_on_main(set_lock)

        try:
            threading.Thread(target=worker, daemon=True,
                             name="dyndolod-run").start()
        except BaseException:
            # start() failed: the worker (and its finally) will never run, so
            # release the tool lock here (GUI thread - direct call is safe).
            set_lock(False)
            raise

    def _on_run_started(self):
        self._ran = True
        self._set_status(
            self._run_status,
            self.tr('{0} is running.\nClose it when you are done, then click Done.').format(self._name),
            ok_text())
        self._done_btn.setEnabled(True)

    # ---- shared -------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DL_AUTO:
            self._set_status(self._dl_status,
                             self.tr("Fetching latest release from GitHub…"), "")
            self._start_auto_download()
        elif idx == _PG_LOCATE:
            self._set_status(self._locate_status,
                             self.tr("Searching Downloads folder…"), "")
            self._scan_downloads()
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status, self.tr("Extracting…"), "")
            self._start_extract()
        elif idx == _PG_PROTON:
            self._enter_proton()
        elif idx == _PG_RUN:
            self._start_run()

    def _finish(self):
        # ✕, Done, and the auto-close after the tool exits all land here.
        # Idempotent; in-flight daemon workers finish harmlessly (their late
        # signals are dropped by the _closing guards).
        if self._closing:
            return
        self._closing = True
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            # The tool swapped its output into the output mod (or the modlist
            # gained the output mod) - re-sync the modlist so the changes show
            # up (mirrors Tk's _reload_mod_panel).
            do_refresh()
