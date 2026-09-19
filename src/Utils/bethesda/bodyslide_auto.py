"""
Automatic, chunked BodySlide builds.

Why this exists: a single BodySlide batch build of a large load order grows to
tens of GB of anonymous memory inside ONE process, and with 1000+ outfits the
Linux OOM killer ends it at the very end of the build (observed: 32.2 GB
anon-rss, `Out of memory: Killed process … (BodySlide)`). The memory is
cumulative within a process, so the fix that needs no cooperation from the tool
is to stop giving it one enormous batch.

The native BodySlide CLI is **group-only** - it accepts

    -gbuild, --groupbuild=<str>   builds the specified group on launch
    -t, --targetdir=<str>         build target directory
    -p, --preset=<str>            preset used for the build
    -tri, --trimorphs             enables tri morph output
    -preview, --preview=<str>     open the specified nif files in preview mode

and nothing else - there is no per-outfit flag and no filter, so the unit of
chunking has to be a *slider group*. Each group runs in its own process, which
bounds the peak instead of accumulating it. Groups too large to be safe on their
own are split further by synthesising temporary group files in the deployed
SliderGroups folder and removing them again afterwards; that is the only case
that writes into the game's Data tree, and it is cleaned up even on failure.

Gui-neutral on purpose: no Qt import. The wizard/launch integration only calls
into this module.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Marker so temporary chunk groups can be told apart from a mod's own groups.
CHUNK_PREFIX = "ZZAutoChunk_"

# Slider-data entries per chunk. The observed failure was ~194k entries in one
# process; ~8k per process keeps every chunk well inside what previously worked
# (CBBE's whole 492-outfit set is ~39k and builds fine on its own).
DEFAULT_MAX_DATA_PER_CHUNK = 8000

_STAMP_NAME = "BodySlide_auto_stamp.json"


def _noop(_msg: str) -> None:
    pass


@dataclass
class Outfit:
    """One <SliderSet> - the thing a build produces."""
    name: str
    data: int          # number of <Data> entries: the cost driver
    source: str = ""


@dataclass
class Chunk:
    """One BodySlide process invocation."""
    group: str                       # value for --groupbuild
    outfits: list[str] = field(default_factory=list)
    data: int = 0
    temporary: bool = False          # synthetic group file, to be removed after

    @property
    def label(self) -> str:
        return f"{self.group} ({len(self.outfits)} outfits, {self.data} data)"


# ---------------------------------------------------------------------------
# Discovery - reads the DEPLOYED tree, which is what the tool itself sees
# ---------------------------------------------------------------------------

def bodyslide_dir(game: "BaseGame") -> Path | None:
    """<deployed Data>/CalienteTools/BodySlide, or None when not deployed."""
    from Utils.vfs import effective_tool_data_root
    try:
        root = effective_tool_data_root(game)
    except RuntimeError:
        return None
    if root is None:
        return None
    path = Path(root) / "CalienteTools" / "BodySlide"
    return path if path.is_dir() else None


def _parse_slider_sets(path: Path) -> list[Outfit]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for m in re.finditer(r'<SliderSet\b[^>]*name="([^"]+)"(.*?)</SliderSet>',
                         text, re.S):
        out.append(Outfit(name=m.group(1),
                          data=len(re.findall(r"<Data\b", m.group(2))),
                          source=path.name))
    return out


def discover_outfits(game: "BaseGame") -> list[Outfit]:
    """Every outfit BodySlide would build, with its slider-data cost.

    Deduplicated by outfit name: a mod shipping the same outfit twice (or two
    mods sharing one) must not be counted twice, or chunk sizes lie.
    """
    root = bodyslide_dir(game)
    if root is None:
        return []
    seen: dict[str, Outfit] = {}
    for folder in ("SliderSets", "ConversionSets"):
        for path in sorted(glob.glob(str(root / folder / "*.osp"))) + \
                    sorted(glob.glob(str(root / folder / "*.xml"))):
            if os.path.basename(path).startswith(CHUNK_PREFIX):
                continue
            for outfit in _parse_slider_sets(Path(path)):
                seen.setdefault(outfit.name, outfit)
    return sorted(seen.values(), key=lambda o: o.name)


def discover_groups(game: "BaseGame") -> dict[str, list[str]]:
    """Slider groups from the deployed tree: name -> member outfit names.

    Our own synthetic chunk groups are excluded so a crashed previous run's
    leftovers cannot become input to this one.
    """
    root = bodyslide_dir(game)
    if root is None:
        return {}
    groups: dict[str, list[str]] = {}
    for path in sorted(glob.glob(str(root / "SliderGroups" / "*.xml"))):
        if os.path.basename(path).startswith(CHUNK_PREFIX):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'<Group\b[^>]*name="([^"]+)"(.*?)</Group>',
                             text, re.S):
            members = re.findall(r'<Member\b[^>]*name="([^"]+)"', m.group(2))
            # first definition wins, matching the tool's own precedence
            groups.setdefault(m.group(1), members)
    return groups


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_chunks(groups: dict[str, list[str]], outfits: list[Outfit], *,
                max_data_per_chunk: int = DEFAULT_MAX_DATA_PER_CHUNK,
                ) -> list[Chunk]:
    """Turn groups into an ordered list of process invocations.

    A group at or under the budget becomes one chunk. A heavier group is split
    into synthetic sub-groups (greedy, in outfit order) so no single process
    exceeds the budget. Outfits in no group are collected into one final chunk
    so nothing is silently skipped.
    """
    cost = {o.name: o.data for o in outfits}
    grouped: set[str] = set()
    chunks: list[Chunk] = []

    for name, members in groups.items():
        known = [m for m in members if m in cost]
        grouped.update(known)
        if not known:
            continue
        total = sum(cost[m] for m in known)
        if total <= max_data_per_chunk:
            chunks.append(Chunk(group=name, outfits=known, data=total))
            continue
        # greedy split preserving the group's own ordering
        bucket: list[str] = []
        used = 0
        part = 0
        for member in known:
            if bucket and used + cost[member] > max_data_per_chunk:
                part += 1
                chunks.append(Chunk(group=f"{CHUNK_PREFIX}{name}_{part}",
                                    outfits=bucket, data=used, temporary=True))
                bucket, used = [], 0
            bucket.append(member)
            used += cost[member]
        if bucket:
            part += 1
            chunks.append(Chunk(group=f"{CHUNK_PREFIX}{name}_{part}",
                                outfits=bucket, data=used, temporary=True))

    loose = [o.name for o in outfits if o.name not in grouped]
    if loose:
        chunks.append(Chunk(group=f"{CHUNK_PREFIX}ungrouped", outfits=loose,
                            data=sum(cost[n] for n in loose), temporary=True))
    return chunks


# ---------------------------------------------------------------------------
# Temporary chunk groups
# ---------------------------------------------------------------------------

def chunk_group_dirs(game: "BaseGame") -> list[Path]:
    """Where a synthetic group file can live so the tool will read it."""
    root = bodyslide_dir(game)
    return [] if root is None else [root / "SliderGroups"]


def write_chunk_groups(game: "BaseGame", chunks: list[Chunk],
                       log_fn: Callable[[str], None] = _noop) -> list[Path]:
    """Write synthetic group files; returns the paths written.

    Only our own files are ever written, and they are removed by
    clear_chunk_groups() whether the build succeeded or not.
    """
    written: list[Path] = []
    for folder in chunk_group_dirs(game):
        if not folder.is_dir():
            continue
        for chunk in chunks:
            if not chunk.temporary:
                continue
            body = "\n".join(f'      <Member name="{n}"/>'
                             for n in chunk.outfits)
            xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<SliderGroups>\n'
                   f'  <Group name="{chunk.group}">\n{body}\n'
                   '  </Group>\n'
                   '</SliderGroups>\n')
            path = folder / f"{chunk.group}.xml"
            try:
                path.write_text(xml, encoding="utf-8")
                written.append(path)
            except OSError as exc:
                log_fn(f"could not write chunk group {path.name}: {exc}")
    return written


def clear_chunk_groups(game: "BaseGame",
                       log_fn: Callable[[str], None] = _noop) -> int:
    """Remove every synthetic group file, including a crashed run's leftovers."""
    removed = 0
    for folder in chunk_group_dirs(game):
        for path in glob.glob(str(folder / f"{CHUNK_PREFIX}*.xml")):
            try:
                os.unlink(path)
                removed += 1
            except OSError as exc:
                log_fn(f"could not remove {os.path.basename(path)}: {exc}")
    return removed


# ---------------------------------------------------------------------------
# Staleness - "every time it might be needed"
# ---------------------------------------------------------------------------

def fingerprint(game: "BaseGame", profile: str, groups: dict[str, list[str]],
                outfits: list[Outfit], *, extra: str = "") -> str:
    """A cheap hash of everything that changes what a build must produce.

    Deliberately excludes the OUTPUT (a build does not invalidate itself):
      * every outfit name and its slider-data volume
      * the group layout, since that is what the chunks are made of
      * the source files' sizes+mtime, so edited slider data invalidates
      * the tool version and the TRI setting, which change the output format
    """
    h = hashlib.sha256()
    for o in sorted(outfits, key=lambda x: x.name):
        h.update(f"{o.name}\x1f{o.data}\x1e".encode())
    for name in sorted(groups):
        h.update(f"{name}\x1f{','.join(groups[name])}\x1e".encode())
    root = bodyslide_dir(game)
    if root is not None:
        for path in sorted(glob.glob(str(root / "*" / "*"))):
            try:
                st = os.stat(path)
            except OSError:
                continue
            h.update(f"{os.path.relpath(path, root)}\x1f{st.st_size}\x1f{st.st_mtime_ns}\x1e".encode())
    try:
        from Utils.bethesda.bodyslide_linux import installed_version
        h.update(f"tool\x1f{installed_version()}\x1e".encode())
    except Exception:  # noqa: BLE001 - a missing tool must not break the hash
        pass
    h.update(f"extra\x1f{extra}\x1e".encode())
    return h.hexdigest()


def stamp_path(game: "BaseGame", profile: str) -> Path:
    from Utils.bethesda.bodyslide_linux import data_dir
    return data_dir(game, profile) / _STAMP_NAME


def is_stale(game: "BaseGame", profile: str) -> tuple[bool, str]:
    """(needs_build, reason). A missing or unreadable stamp means stale."""
    try:
        groups = discover_groups(game)
        outfits = discover_outfits(game)
    except Exception as exc:  # noqa: BLE001
        return True, f"could not read BodySlide data ({exc})"
    if not outfits:
        return False, "no BodySlide outfits deployed"
    if not groups:
        return True, "no slider groups deployed to chunk by"
    want = fingerprint(game, profile, groups, outfits)
    try:
        data = json.loads(stamp_path(game, profile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, "no previous automatic build recorded"
    if data.get("fingerprint") != want:
        return True, "BodySlide inputs changed since the last build"
    return False, "up to date"


def write_stamp(game: "BaseGame", profile: str, fingerprint_value: str,
                *, chunks: int, built: int) -> None:
    path = stamp_path(game, profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fingerprint": fingerprint_value,
            "chunks": chunks,
            "outfits_built": built,
        }, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run_chunked(game: "BaseGame", profile: str, *, output_dir: Path,
                log_fn: Callable[[str], None] = _noop,
                max_data_per_chunk: int = DEFAULT_MAX_DATA_PER_CHUNK,
                trimorphs: bool = True) -> int:
    """Build every group, one process per chunk. Returns the number of failures.

    Never raises for a single chunk failing: a build that dies on chunk 7 of 12
    must still report 1..6 as done, and the caller can retry cheaply because
    the stamp is only written when everything succeeded.

    *trimorphs* defaults to True because a command-line build does NOT write
    morph output unless asked, and without it every built body would silently
    lose its BodyMorph sliders - a worse outcome than the memory it costs, now
    that each process is bounded.
    """
    from Utils.bethesda.bodyslide_linux import build_env, run_logged

    groups = discover_groups(game)
    outfits = discover_outfits(game)
    chunks = plan_chunks(groups, outfits, max_data_per_chunk=max_data_per_chunk)
    if not chunks:
        log_fn("BodySlide: nothing to build (no outfits or groups).")
        return 0

    cleared = clear_chunk_groups(game, log_fn=log_fn)
    if cleared:
        log_fn(f"BodySlide: removed {cleared} leftover chunk group(s).")
    written = write_chunk_groups(game, chunks, log_fn=log_fn)

    failures = 0
    total_data = sum(c.data for c in chunks)
    env = build_env(game, profile, output_dir, log_fn=log_fn)
    log_fn(f"BodySlide: {len(chunks)} chunk(s), {len(outfits)} outfits, "
           f"{total_data} slider-data entries, limit {max_data_per_chunk}/chunk.")
    try:
        for index, chunk in enumerate(chunks, 1):
            args = [f"--groupbuild={chunk.group}", f"--targetdir={output_dir}"]
            if trimorphs:
                args.append("--trimorphs")
            log_fn(f"BodySlide: chunk {index}/{len(chunks)} - {chunk.label}")
            rc = run_logged("BodySlide", env, args=args, log_fn=log_fn,
                            label=f"BodySlide[{index}/{len(chunks)}]")
            if rc != 0:
                failures += 1
                log_fn(f"BodySlide: chunk {index} failed with code {rc}.")
    finally:
        # always, including on KeyboardInterrupt: a leftover chunk group would
        # otherwise be read back as a real group on the next run
        for path in written:
            try:
                os.unlink(path)
            except OSError as exc:
                log_fn(f"BodySlide: could not remove {path.name}: {exc}")
    return failures


# ---------------------------------------------------------------------------
# Launch integration - one call, so the Qt side needs no logic of its own
# ---------------------------------------------------------------------------

# Key for the Launch-settings checkbox declared by the Skyrim handler. Stored
# per game by Utils/executables/launch.py; absent means enabled, matching the
# "it should just work unless I turn it off" intent.
TOGGLE_KEY = "bodyslide_auto"


def is_enabled(game: "BaseGame") -> bool:
    """Whether automatic builds are on for *game*.

    Off unless the user turned it off is the wrong way round for a toggle that
    defaults to on, so any failure to read the setting leaves it enabled only
    when the tool is actually installed.
    """
    try:
        from Utils.bethesda.bodyslide_linux import is_installed
        if not is_installed():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        from Utils.executables import launch as exe_launch
        return bool(exe_launch.load_launch_toggle(game, TOGGLE_KEY, True))
    except Exception:  # noqa: BLE001 - an unreadable setting must not block play
        return True


def ensure_output_dir(game: "BaseGame", profile: str) -> Path | None:
    """The output-capture mod a build lands in, created if needed.

    Same destination the BodySlide wizard uses, so an automatic build and a
    manual one write to the same mod instead of producing two.
    """
    try:
        from Utils.bethesda.bodyslide import ensure_output_mod, sanitize_output_name
        from Utils.bethesda.bodyslide_linux import TOOLS
        name = sanitize_output_name(TOOLS["bodyslide"][2])
        ensure_output_mod(game, profile, name)
        return game.get_effective_mod_staging_path() / name
    except Exception:  # noqa: BLE001
        return None


def run_automatic(game: "BaseGame", profile: str,
                  log_fn: Callable[[str], None] = _noop) -> None:
    """The whole transparent step: decide, build, record. Never raises.

    Called from the launch path immediately before the game starts. It must not
    be able to stop someone playing, so every failure is logged and swallowed -
    the worst case is a stale body, not a game that will not start.
    """
    try:
        if not is_enabled(game):
            return
        stale, why = is_stale(game, profile)
        if not stale:
            log_fn(f"BodySlide: automatic build skipped - {why}.")
            return
        log_fn(f"BodySlide: automatic build needed - {why}.")

        groups = discover_groups(game)
        outfits = discover_outfits(game)
        output_dir = ensure_output_dir(game, profile)
        if output_dir is None:
            log_fn("BodySlide: could not determine the output mod; skipping.")
            return
        output_dir.mkdir(parents=True, exist_ok=True)

        want = fingerprint(game, profile, groups, outfits)
        failures = run_chunked(game, profile, output_dir=output_dir,
                               log_fn=log_fn)
        if failures:
            log_fn(f"BodySlide: {failures} chunk(s) failed; not recording the "
                   "build so the next launch retries.")
            return
        write_stamp(game, profile, want,
                    chunks=len(plan_chunks(groups, outfits)),
                    built=len(outfits))
        log_fn(f"BodySlide: automatic build complete ({len(outfits)} outfits).")
    except Exception as exc:  # noqa: BLE001 - never block the launch
        log_fn(f"BodySlide: automatic build failed - {exc!r}")
