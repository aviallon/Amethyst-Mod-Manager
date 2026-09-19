"""
GUI-neutral core of the native-Linux BodySlide / Outfit Studio wizard.

Unlike the Proton wizard (Utils/bethesda/bodyslide.py) this runs a Linux build
straight on the host, so there is no prefix, no registry seeding and no
Config.xml rewriting: the fork exposes BSOS_* environment variables that win
over the stored configuration on every launch, so the wizard just downloads
the build, deploys, and runs it with the right env.

Fork: https://github.com/ChrisDKN/BodySlide-and-Outfit-Studio-Appimage

We ship the **portable tarball**, not the AppImage. The tarball is a plain
relocatable directory with its own bundled loader and libc, so it needs no
FUSE mount and - unlike an AppImage - runs unchanged inside our own flatpak
sandbox, with no flatpak-spawn --host hop. The tarball also carries a launcher
script per program (``<root>/BodySlide``, ``<root>/OutfitStudio``) that sets up
sharun, PATH and BSOS_BINDIR.

The variables the fork reads (see its GameUtil::ApplyEnvironmentOverrides and
ProjectUtil::GetDataDir):
  BSOS_TARGET_GAME       game name as it appears in GameUtil::TargetGames
                         ("SkyrimSpecialEdition", "Fallout4"; also accepts the
                         raw index). An unknown value is ignored by the tool
                         rather than silently selecting the wrong game.
  BSOS_GAME_DATA_PATH    the deployed Data folder.
  BSOS_OUTPUT_DATA_PATH  where built meshes are written - the output-capture
                         mod in staging, so the build lands in the mod list
                         instead of loose in the game folder.
  BSOS_APPDIR            writable data dir holding Config.xml / *.xml / logs.

Slider data is NOT passed in: with BSOS_APPDIR holding no SliderSets folder,
the fork's GetProjectPath() falls back to <GameData>/CalienteTools/BodySlide,
which is exactly where deployed BodySlide mods land. That is why BSOS_APPDIR
must stay free of a SliderSets directory - its presence would make the tool
treat the data dir as the project dir and list nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Which Linux bundle to install. Anyone can publish their own from a fork of the
# project (the appimage workflow is in-tree), so this is overridable - a fork
# that carries a fix gets it to its users through the ordinary install path
# instead of a hand-patched bundle, which the next update would overwrite.
# Default: the fork that builds the fixed slider-data handling.
_DEFAULT_BODYSLIDE_REPO = "aviallon/BodySlide-and-Outfit-Studio"
_bodyslide_repo = os.environ.get("AMETHYST_BODYSLIDE_REPO", "").strip().strip("/")
if not _bodyslide_repo or "/" not in _bodyslide_repo:
    _bodyslide_repo = _DEFAULT_BODYSLIDE_REPO
GITHUB_API_URL = f"https://api.github.com/repos/{_bodyslide_repo}/releases/latest"
REPO_URL = f"https://github.com/{_bodyslide_repo}"
# tool key → (display name, launcher basename, default output mod name)
TOOLS: dict[str, tuple[str, str, str]] = {
    "bodyslide":    ("BodySlide", "BodySlide", "BodySlide_files"),
    "outfitstudio": ("Outfit Studio", "OutfitStudio", "OutfitStudio_files"),
}

# Seeded into a per-profile BSOS_APPDIR on first use - see seed_data_dir().
_SEED_XML = ("Config.xml", "BodySlide.xml", "OutfitStudio.xml",
             "BuildSelection.xml", "RefTemplates.xml")
_SEED_LINKS = ("res", "lang")


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Install location
# ---------------------------------------------------------------------------
#
# Shared across games rather than per-game Applications/: the tree is a
# self-contained ~170 MB bundle with no per-game state (all of that travels in
# BSOS_APPDIR), so a copy per game would only duplicate downloads and updates.

def tools_dir() -> Path:
    """~/.config/AmethystModManager/Tools/BodySlide-Linux/"""
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "Tools" / "BodySlide-Linux"


def install_root() -> Path:
    """The extracted tarball tree."""
    return tools_dir() / "current"


def version_file() -> Path:
    return tools_dir() / "version.txt"


def launcher_path(program: str) -> Path:
    """The tarball's launcher script for *program* ("BodySlide"/"OutfitStudio")."""
    return install_root() / program


def is_installed() -> bool:
    return all(os.access(launcher_path(p), os.X_OK)
               for _n, p, _o in TOOLS.values())


def installed_version() -> str | None:
    """Release tag of the installed build, or None when not installed."""
    if not is_installed():
        return None
    try:
        tag = version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tag or None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_latest_release() -> tuple[str, str]:
    """Return (tag, download_url) for the newest x86_64 portable tarball.

    Deliberately not Utils.wizards.archives.fetch_latest_github_asset: that one
    only accepts ARCHIVE_EXTS (.zip/.7z/…), and would also have to be taught to
    skip the AppImage and .zsync assets published alongside the tarball.
    """
    import json
    import urllib.request

    from Utils.ca_bundle import get_ssl_context

    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15,
                                context=get_ssl_context()) as resp:
        data = json.loads(resp.read().decode())

    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name = asset.get("name", "").lower()
        if name.endswith(".tar.zst") and "x86_64" in name:
            return tag, asset["browser_download_url"]
    raise RuntimeError(
        f"No x86_64 .tar.zst asset in the latest release ({tag}).")


def install_release(url: str, tag: str, *, reporthook=None,
                    log_fn: Callable[[str], None] = _noop) -> Path:
    """Download and extract *url*, replacing any existing install.

    Extraction goes to a staging directory that only replaces the live tree
    once it is complete, so a failed download or extraction never leaves a
    half-populated bundle behind. The old tree is removed rather than merged:
    a new release renames libraries, and leftovers from the previous version
    would sit in lib/ shadowing nothing but wasting space at best.
    """
    import tempfile

    from Utils.ca_bundle import download_file
    from Utils.wizards.archives import extract_to_dir

    root = install_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.with_name(root.name + ".new")
    old = root.with_name(root.name + ".old")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)

    with tempfile.TemporaryDirectory(dir=str(root.parent)) as tmpdir:
        archive = Path(tmpdir) / "bodyslide.tar.zst"
        download_file(url, archive, reporthook=reporthook)
        log_fn(f"extracting {archive.name}…")
        unpacked = Path(tmpdir) / "x"
        unpacked.mkdir()
        extract_to_dir(archive, unpacked)

        # The tarball wraps everything in one versioned directory; move that
        # up so the launcher always lives at a stable path.
        entries = [e for e in unpacked.iterdir() if e.name != "__MACOSX"]
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked
        src.rename(staging)

    missing = [p for _n, p, _o in TOOLS.values()
               if not os.access(staging / p, os.X_OK)]
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(
            "Launcher(s) missing from the archive: " + ", ".join(missing))

    if root.exists():
        root.rename(old)
    staging.rename(root)
    shutil.rmtree(old, ignore_errors=True)

    try:
        version_file().write_text(tag + "\n", encoding="utf-8")
    except OSError as exc:
        log_fn(f"could not record version ({exc})")
    log_fn(f"installed {tag} → {root}")
    return root


# ---------------------------------------------------------------------------
# Per-game / per-profile environment
# ---------------------------------------------------------------------------

def safe_name(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw or "")


def target_game(game: "BaseGame") -> str | None:
    """The GameUtil::TargetGames name for *game*, or None when unsupported.

    Reuses the Proton wizard's mapping table - its tag strings are exactly the
    names the fork matches BSOS_TARGET_GAME against.
    """
    from Utils.bethesda.bodyslide import bodyslide_game
    mapping = bodyslide_game(game)
    return None if mapping is None else mapping[0]


def data_dir(game: "BaseGame", profile: str) -> Path:
    """Writable BSOS_APPDIR for this game+profile.

    Per profile because BuildSelection.xml (which outfits are ticked) belongs
    to a load order, not to the machine. Lives under the profile root's
    Applications/ folder, which the filemap never scans.
    """
    from Utils.bethesda.xedit import applications_dir
    return applications_dir(game, "BodySlide-Linux") / f"data_{safe_name(profile)}"


def seed_data_dir(app_dir: Path, root: Path,
                  log_fn: Callable[[str], None] = _noop) -> None:
    """Make *app_dir* usable as BSOS_APPDIR for the install at *root*.

    The tarball's own launcher only defaults BSOS_APPDIR to the tarball root,
    which is already populated; it does nothing when a caller points the
    variable elsewhere. But the programs resolve res/ and lang/ RELATIVE TO
    the data dir - wx loads res/xrc/BodySlide.xrc from there - so an
    un-seeded data dir fails at startup with "Cannot open resources file".
    The AppImage's AppRun does this seeding; for the tarball it is ours to do.

    res/ and lang/ are symlinked (never copied) so an update to *root* is
    picked up immediately; the XML defaults are copied once and then owned by
    the program, which rewrites them on exit.
    """
    app_dir.mkdir(parents=True, exist_ok=True)

    for name in _SEED_LINKS:
        link, target = app_dir / name, root / name
        if not target.exists():
            log_fn(f"WARNING: {target} missing from the install.")
            continue
        # Refresh dangling/stale links (the install path can change across
        # updates); a real directory in their place is assumed deliberate.
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
            link.unlink()
        elif link.exists():
            continue
        link.symlink_to(target)

    for name in _SEED_XML:
        dst, src = app_dir / name, root / name
        if dst.exists() or not src.is_file():
            continue
        try:
            shutil.copy2(src, dst)
            dst.chmod(dst.stat().st_mode | 0o200)
        except OSError as exc:
            log_fn(f"could not seed {name} ({exc})")


def build_env(game: "BaseGame", profile: str, output_dir: Path, *,
              base: "dict | None" = None,
              log_fn: Callable[[str], None] = _noop) -> dict:
    """Environment for a native launch: host env + the BSOS_* overrides.

    Starts from Utils.environment.xdg.host_env() so a launch from inside our own AppImage
    doesn't hand the child our bundled loader/GTK paths (see
    Utils/environment/appimage.py).
    """
    from Utils.environment.xdg import host_env

    env = dict(base) if base is not None else host_env()

    app_dir = data_dir(game, profile)
    seed_data_dir(app_dir, install_root(), log_fn=log_fn)
    # An empty SliderSets here would make GetProjectPath() return this folder
    # and stop looking, so the tool would list no outfits at all. It should
    # never exist, but a stray one is cheap to catch and impossible to debug
    # from the UI, so say so in the log rather than silently listing nothing.
    if (app_dir / "SliderSets").is_dir():
        log_fn(f"WARNING: {app_dir}/SliderSets exists - outfit discovery will "
               "use that folder instead of the deployed Data folder.")
    env["BSOS_APPDIR"] = str(app_dir)

    name = target_game(game)
    if name:
        env["BSOS_TARGET_GAME"] = name
    else:
        log_fn(f"no BodySlide target game for {game.name}; "
               "the tool will keep its configured game.")

    from Utils.vfs import effective_tool_data_root
    try:
        data_path = effective_tool_data_root(game)
    except RuntimeError:
        data_path = None
    if data_path is not None:
        env["BSOS_GAME_DATA_PATH"] = str(data_path)

    env["BSOS_OUTPUT_DATA_PATH"] = str(output_dir)
    return env


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

# GTK chatter the tool emits by the dozen per window resize ("Negative content
# width …", host desktop modules the bundled GTK can't load). It says nothing
# about BodySlide and would bury the lines that matter in the app log.
_GTK_NOISE = re.compile(r"\b(Gtk|Gdk|GLib|GLib-GObject)-(WARNING|Message|CRITICAL)\b")


# Exit codes invented by the watchdog below, so a caller can tell "we stopped it"
# from "it crashed". Both are negative and outside the range a real process
# reports, and distinct from -9 (the OOM killer) and -15 (someone's SIGTERM).
EXIT_MEMORY_GUARD = -100
EXIT_TIMEOUT = -101


def _page_size_kb() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") // 1024
    except (ValueError, OSError, AttributeError):
        return 4


def _rss_mb(pid: int, scale: float) -> float:
    """Resident memory of one process in MiB, or 0.0 if it is gone.

    /proc/<pid>/statm is "size resident shared text lib data dt" in pages. It
    is used instead of /proc/<pid>/stat because stat's comm field is
    parenthesised and may contain spaces, which makes its field offsets a
    classic source of silently wrong numbers.
    """
    try:
        with open(f"/proc/{pid}/statm", "rb") as handle:
            return int(handle.read().split()[1]) * scale
    except (OSError, IndexError, ValueError):
        return 0.0


def _parent_pid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"PPid:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return 0


def process_tree_rss_mb(root: int) -> float:
    """Resident memory of *root* and every descendant, in MiB.

    The launcher is a shell script that execs Wine which execs the real
    BodySlide.exe, so the interesting memory is never in *root* itself. Nothing
    here is Wine-specific: it walks /proc, summing resident memory over the
    process tree.

    Returns 0.0 when /proc cannot be read (non-Linux, or the process has gone).
    """
    scale = _page_size_kb() / 1024.0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0.0
    children: dict[int, list[int]] = {}
    rss: dict[int, float] = {}
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        rss[pid] = _rss_mb(pid, scale)
        parent = _parent_pid(pid)
        if parent:
            children.setdefault(parent, []).append(pid)
    total = 0.0
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0.0)
        stack.extend(children.get(pid, ()))
    return total


def run_logged(program: str, env: dict, *,
               log_fn: Callable[[str], None] = _noop,
               label: str = "BodySlide",
               args: "list[str] | None" = None,
               max_rss_mb: "float | None" = None,
               timeout_s: "float | None" = None,
               poll_s: float = 0.5) -> int:
    """Run the tarball launcher for *program*, streaming output to *log_fn*.

    *args* are extra command-line arguments; with none the program starts
    normally (its GUI). The tool's CLI supports only --groupbuild/--targetdir/
    --preset/--trimorphs/--preview, so --groupbuild is how an unattended build
    is requested - see Utils/bethesda/bodyslide_auto.py.

    *max_rss_mb* and *timeout_s*, when given, put a ceiling on one run: the
    whole process tree is polled, and on breach it is killed and
    EXIT_MEMORY_GUARD / EXIT_TIMEOUT is returned. BodySlide asks for memory as
    a function of the outfit it is on, not of the batch, so a cap on the batch
    cannot bound one chunk - a watchdog is the only thing that keeps the kernel
    from reaching for the OOM killer, which takes the whole machine's
    responsiveness with it.

    Blocks until the tool exits - call from a worker thread. No flatpak-spawn
    hop: the bundle carries its own loader and libc, so it runs inside our
    sandbox as-is.
    """
    import threading
    import time

    launcher = launcher_path(program)
    home = os.path.expanduser("~")
    cwd = home if os.path.isdir(home) else "/"
    argv = [str(launcher), *(args or [])]

    log_fn(f"{label}: launching {' '.join(argv)}")
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            # own session, so killing the run kills Wine and the real exe too
            start_new_session=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch - {exc}")
        raise

    assert proc.stdout is not None
    suppressed = 0
    state = {"lines": 0}

    def drain() -> None:
        nonlocal suppressed
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.rstrip("\n")
                if not line:
                    continue
                if _GTK_NOISE.search(line):
                    suppressed += 1
                else:
                    state["lines"] += 1
                    log_fn(f"{label}: {line}")
        except (ValueError, OSError):
            pass  # pipe closed under us when the process was killed

    reader = threading.Thread(target=drain, name=f"{label}-log", daemon=True)
    reader.start()

    killed: int | None = None
    started = time.monotonic()
    peak = 0.0
    while True:
        try:
            proc.wait(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            pass
        if max_rss_mb is not None and killed is None:
            used = process_tree_rss_mb(proc.pid)
            peak = max(peak, used)
            if used > max_rss_mb:
                log_fn(f"{label}: using {used:.0f} MB after "
                       f"{time.monotonic() - started:.0f}s, over the "
                       f"{max_rss_mb:.0f} MB limit - stopping it here rather "
                       f"than letting the OOM killer take the machine.")
                killed = EXIT_MEMORY_GUARD
        if (timeout_s is not None and killed is None
                and time.monotonic() - started > timeout_s):
            log_fn(f"{label}: still running after {timeout_s:.0f}s - stopping it.")
            killed = EXIT_TIMEOUT
        if killed is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            break

    rc = proc.wait()
    reader.join(timeout=2.0)
    if killed is not None:
        rc = killed
    if suppressed:
        log_fn(f"{label}: suppressed {suppressed} GTK warning line(s).")
    if killed is not None:
        log_fn(f"{label}: killed by the watchdog (peak {peak:.0f} MB, "
               f"{time.monotonic() - started:.0f}s, {state['lines']} log line(s))")
    elif rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc
