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

# Outfits per chunk. Both budgets are crude proxies for what BodySlide is
# actually about to allocate, and neither predicts the worst case: measured on
# one load order, 48 outfits / 3497 data entries ran 128s and was OOM-killed at
# 32.7 GB, while a different 48-outfit / 1176-entry chunk finished in 2s. The
# cost is a property of the INDIVIDUAL outfit (which mesh and which slider data
# it pulls in), so chunk size can only reduce how often the worst case is hit -
# the watchdog and the bisect below are what make it survive.
DEFAULT_MAX_OUTFITS_PER_CHUNK = 48

# Ceiling on one chunk process, enforced by polling its process tree (see
# bodyslide_linux.run_logged). Set well under the machine's RAM so the kernel's
# OOM killer never gets to make the choice, which on this workload also takes
# the desktop's responsiveness with it. A healthy chunk stays in the hundreds
# of MB, so this only ever fires on the pathological case.
DEFAULT_MAX_RSS_MB = 8000

# And a wall-clock ceiling, because "very slow" and "about to run out of
# memory" are the same failure from the outside, and a hung chunk would
# otherwise stall a launch forever.
DEFAULT_CHUNK_TIMEOUT_S = 300

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


@dataclass
class Alternative:
    """Groups that cover substantially the same outfits - pick one.

    A body shape or a physics variant ships the same outfits again under a
    different group (CBBE vs 3BA vs BHUNP vs HIMBO, with and without physics),
    so building all of them duplicates work and multiplies memory for meshes the
    user will never see. Detected from member overlap, not from names: names are
    a hint for the UI, but two groups that list the same outfits ARE
    alternatives whatever they are called.
    """
    key: str                       # stable id, used to store the choice
    groups: list[str] = field(default_factory=list)
    members: int = 0               # outfits in the largest candidate
    hint: str = ""                 # e.g. "body shape" / "physics", for the UI
    certain: bool = False          # near-identical membership: safe to skip
    #                               by default. Weak candidates (a shared name
    #                               prefix) are only OFFERED - skipping on a name
    #                               guess would silently drop wanted outfits.


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

def _split_budget(names: list[str], cost: dict[str, int], budget: int,
                  prefix: str, max_outfits: int) -> list[Chunk]:
    """Greedy split of *names* into chunks within BOTH budgets."""
    chunks: list[Chunk] = []
    bucket: list[str] = []
    used = 0
    part = 0
    for name in names:
        too_big = (bucket and (used + cost.get(name, 0) > budget
                               or len(bucket) >= max_outfits))
        if too_big:
            part += 1
            chunks.append(Chunk(group=f"{prefix}_{part}", outfits=bucket,
                                data=used, temporary=True))
            bucket, used = [], 0
        bucket.append(name)
        used += cost.get(name, 0)
    if bucket:
        part += 1
        chunks.append(Chunk(group=f"{prefix}_{part}", outfits=bucket,
                            data=used, temporary=True))
    return chunks


def plan_chunks(groups: dict[str, list[str]], outfits: list[Outfit], *,
                max_data_per_chunk: int = DEFAULT_MAX_DATA_PER_CHUNK,
                max_outfits_per_chunk: int = DEFAULT_MAX_OUTFITS_PER_CHUNK,
                skip_groups: "set[str] | None" = None,
                ) -> list[Chunk]:
    """Turn groups into an ordered list of process invocations.

    A group within both budgets becomes one chunk. A heavier group is split into
    synthetic sub-groups. Outfits in NO group are split by the same budgets
    rather than collected into one chunk: several packs ship no slider groups at
    all, so a single "everything else" chunk would recreate the unbounded batch
    build this module exists to avoid.

    *skip_groups* holds groups the user did not choose (see choose_alternatives);
    their outfits are left out entirely rather than built and discarded.
    """
    cost = {o.name: o.data for o in outfits}
    skip = skip_groups or set()
    grouped: set[str] = set()
    chunks: list[Chunk] = []

    for name, members in groups.items():
        known = [m for m in members if m in cost]
        if name in skip:
            grouped.update(known)      # counted as covered, deliberately not built
            continue
        grouped.update(known)
        if not known:
            continue
        total = sum(cost[m] for m in known)
        if total <= max_data_per_chunk and len(known) <= max_outfits_per_chunk:
            chunks.append(Chunk(group=name, outfits=known, data=total))
        else:
            chunks.extend(_split_budget(known, cost, max_data_per_chunk,
                                        f"{CHUNK_PREFIX}{name}",
                                        max_outfits_per_chunk))

    loose = [o.name for o in outfits if o.name not in grouped]
    if loose:
        total = sum(cost[n] for n in loose)
        if total <= max_data_per_chunk and len(loose) <= max_outfits_per_chunk:
            chunks.append(Chunk(group=f"{CHUNK_PREFIX}ungrouped", outfits=loose,
                                data=total, temporary=True))
        else:
            chunks.extend(_split_budget(loose, cost, max_data_per_chunk,
                                        f"{CHUNK_PREFIX}ungrouped",
                                        max_outfits_per_chunk))
    return chunks


# ---------------------------------------------------------------------------
# Alternatives - one body shape / physics variant, not all of them
# ---------------------------------------------------------------------------

# Tokens that say what an alternative is ABOUT. Used only to label the choice in
# the UI; the grouping itself is decided by member overlap, because two groups
# that list the same outfits are alternatives whatever they are called.
_HINT_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("body shape", ("3ba", "bhunp", "uunp", "cbbe", "himbo", "tng", "sos",
                    "males", "females")),
    ("physics", ("physics", "hdt", "smp")),
)


def isolate_failures(run_fn: Callable[[str, list[str]], int],
                     members: list[str], *,
                     log_fn: Callable[[str], None] = _noop,
                     tag: str = "retry") -> list[str]:
    """Run *members* as one group; on failure, halve and retry.

    Returns the outfits that failed even when built alone. *run_fn* takes
    ``(group_name, member_names)`` and returns the exit code.

    Why bisect rather than just report the chunk: a chunk that dies tells you
    nothing about which of its 48 outfits did it, and the failure is a property
    of one outfit, so the halves quickly name it. A single outfit that fails on
    its own is then skipped by name instead of costing the other 47 their build.
    """
    def attempt(names: list[str]) -> int:
        group = f"{CHUNK_PREFIX}{tag}_{len(names)}"
        return run_fn(group, names)

    rc = attempt(members)
    if rc == 0:
        return []
    if len(members) == 1:
        log_fn(f"BodySlide: '{members[0]}' failed on its own (code {rc}) - "
               f"skipping it; every other outfit in that chunk still built.")
        return list(members)

    mid = len(members) // 2
    log_fn(f"BodySlide: {len(members)} outfits failed together (code {rc}) - "
           f"splitting into {mid} and {len(members) - mid} to find the culprit.")
    failed = isolate_failures(run_fn, members[:mid], log_fn=log_fn,
                              tag=f"{tag}a")
    failed += isolate_failures(run_fn, members[mid:], log_fn=log_fn,
                               tag=f"{tag}b")
    return failed


def _hint_for(names: list[str]) -> str:
    low = " ".join(names).lower()
    hits = [label for label, tokens in _HINT_TOKENS
            if any(t in low for t in tokens)]
    return ", ".join(hits)


def detect_alternatives(groups: dict[str, list[str]], outfits: list[Outfit],
                        *, threshold: float = 0.85,
                        min_members: int = 2) -> list[Alternative]:
    """Group sets that are candidates for "pick one".

    Two signals, and they are NOT equally trustworthy:

    * OVERLAP (certain): two groups share at least *threshold* of the LARGER
      one, so they are the same outfit list under two names - a physics or body
      variant. Building both duplicates work and multiplies memory.
      Normalising by the larger set matters: normalising by the smaller one
      merged a 5-outfit 'CBBE' group with the 32-outfit 'CBBE Vanilla Outfits',
      which is a SUBSET, not an alternative, and would have dropped 32 outfits.
    * NAME PREFIX (candidate only): groups whose names share a prefix often are
      variants whose members are named differently (a body installed for SOS vs
      for vanilla), so overlap cannot see it. Offered, never skipped on its own -
      a name guess is not good enough to drop someone's outfits.
    """
    known = {o.name for o in outfits}
    members = {name: {m for m in ms if m in known}
               for name, ms in groups.items()}
    names = sorted(n for n, ms in members.items() if len(ms) >= min_members)

    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    certain_pairs: set[frozenset[str]] = set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            larger = max(len(members[a]), len(members[b]))
            if not larger:
                continue
            if len(members[a] & members[b]) / larger >= threshold:
                union(a, b)
                certain_pairs.add(frozenset((a, b)))

    sets: dict[str, list[str]] = {}
    for n in names:
        sets.setdefault(find(n), []).append(n)

    out: list[Alternative] = []
    seen: set[str] = set()
    for root, group_names in sets.items():
        if len(group_names) < 2:
            continue
        group_names.sort()
        biggest = max(len(members[n]) for n in group_names)
        certain = all(frozenset(p) in certain_pairs
                      for p in zip(group_names, group_names[1:]))
        out.append(Alternative(key=root, groups=group_names, members=biggest,
                               hint=_hint_for(group_names), certain=certain))
        seen.update(group_names)

    # Weak candidates: same leading word(s), left for the user to decide.
    for prefix_len in (2, 1):
        buckets: dict[str, list[str]] = {}
        for n in names:
            if n in seen:
                continue
            toks = n.split()
            if len(toks) >= prefix_len:
                buckets.setdefault(" ".join(toks[:prefix_len]), []).append(n)
        for prefix, group_names in buckets.items():
            if len(group_names) < 2 or not _hint_for(group_names):
                continue
            group_names.sort()
            biggest = max(len(members[n]) for n in group_names)
            out.append(Alternative(key=f"prefix:{prefix}", groups=group_names,
                                   members=biggest,
                                   hint=_hint_for(group_names), certain=False))
            seen.update(group_names)
    return sorted(out, key=lambda a: a.key)


def skipped_groups(alternatives: list[Alternative],
                   choices: dict[str, str]) -> set[str]:
    """Groups to leave out because the user chose a sibling alternative instead.

    A stored choice always wins. Without one, only a CERTAIN set is reduced to
    its first group; a weak candidate is left alone, because building an
    unwanted variant costs time while skipping a wanted one loses meshes - and
    the log says plainly which groups were built and which were skipped.
    """
    skip: set[str] = set()
    for alt in alternatives:
        chosen = choices.get(alt.key)
        if chosen not in alt.groups:
            if not alt.certain and chosen is None:
                continue
            chosen = alt.groups[0]
        skip.update(g for g in alt.groups if g != chosen)
    return skip


def choices_path(game: "BaseGame", profile: str) -> Path:
    from Utils.bethesda.bodyslide_linux import data_dir
    return data_dir(game, profile) / "BodySlide_auto_choices.json"


def load_choices(game: "BaseGame", profile: str) -> dict[str, str]:
    try:
        data = json.loads(choices_path(game, profile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def save_choices(game: "BaseGame", profile: str, choices: dict[str, str]) -> None:
    path = choices_path(game, profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(choices, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not save alternative choices: {exc}") from exc


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


def _fingerprint_inputs(game: "BaseGame", profile: str,
                       groups: dict[str, list[str]], outfits: list[Outfit]) -> str:
    """One fingerprint used by BOTH is_stale() and run_automatic().

    They must agree exactly, or a fresh build would look stale forever. The
    alternative choices belong in here: picking a different body shape changes
    which outfits are built.
    """
    choices = load_choices(game, profile)
    return fingerprint(game, profile, groups, outfits,
                       extra=json.dumps(choices, sort_keys=True))


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
    want = _fingerprint_inputs(game, profile, groups, outfits)
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
                max_outfits_per_chunk: int = DEFAULT_MAX_OUTFITS_PER_CHUNK,
                max_rss_mb: float = DEFAULT_MAX_RSS_MB,
                timeout_s: float = DEFAULT_CHUNK_TIMEOUT_S,
                trimorphs: bool = True) -> int:
    """Build every chosen group, one process per chunk. Returns unbuildable outfits.

    Never raises for a single chunk failing: a build that dies on chunk 7 of 12
    must still report 1..6 as done. The return value is the number of OUTFITS
    that could not be built even after being isolated - 0 means everything
    built, and anything else means a specific, named outfit needs attention.

    *max_rss_mb* / *timeout_s* are ceilings on ONE chunk process, passed to
    run_logged. They exist because chunk size cannot bound the worst case: an
    outfit can ask for tens of GB on its own, and letting the kernel's OOM
    killer decide takes the whole desktop down with it.

    *trimorphs* defaults to True because a command-line build does NOT write
    morph output unless asked, and without it every built body would silently
    lose its BodyMorph sliders - a worse outcome than the memory it costs, now
    that each process is bounded.
    """
    from Utils.bethesda.bodyslide_linux import (EXIT_MEMORY_GUARD, EXIT_TIMEOUT,
                                               build_env, run_logged)

    groups = discover_groups(game)
    outfits = discover_outfits(game)
    alternatives = detect_alternatives(groups, outfits)
    choices = load_choices(game, profile)
    skip = skipped_groups(alternatives, choices)
    if alternatives:
        for alt in alternatives:
            chosen = choices.get(alt.key) or alt.groups[0]
            if chosen not in alt.groups:
                chosen = alt.groups[0]
            others = [g for g in alt.groups if g != chosen]
            label = f" ({alt.hint})" if alt.hint else ""
            log_fn(f"BodySlide: alternative{label} - building '{chosen}', "
                   f"skipping {', '.join(repr(o) for o in others)}"
                   f" [change in the wizard's alternatives list]")

    chunks = plan_chunks(groups, outfits,
                         max_data_per_chunk=max_data_per_chunk,
                         max_outfits_per_chunk=max_outfits_per_chunk,
                         skip_groups=skip)
    if not chunks:
        log_fn("BodySlide: nothing to build (no outfits or groups).")
        return 0

    cleared = clear_chunk_groups(game, log_fn=log_fn)
    if cleared:
        log_fn(f"BodySlide: removed {cleared} leftover chunk group(s).")
    written = write_chunk_groups(game, chunks, log_fn=log_fn)

    failed_names: list[str] = []
    total_data = sum(c.data for c in chunks)
    total_outfits = sum(len(c.outfits) for c in chunks)
    env = build_env(game, profile, output_dir, log_fn=log_fn)
    log_fn(f"BodySlide: {len(chunks)} chunk(s), {total_outfits} outfits, "
           f"{total_data} slider-data entries "
           f"(caps {max_outfits_per_chunk} outfits / {max_data_per_chunk} data).")
    try:
        # Groups that already have a file on disk from write_chunk_groups above;
        # anything else is a retry group that has to be written as we go.
        prepared = {c.group for c in chunks}

        for index, chunk in enumerate(chunks, 1):
            def run_chunk(group: str, members: list[str]) -> int:
                args = [f"--groupbuild={group}", f"--targetdir={output_dir}"]
                if trimorphs:
                    args.append("--trimorphs")
                wrote: Path | None = None
                if group not in prepared:
                    made = write_chunk_groups(
                        game,
                        [Chunk(group=group, outfits=list(members), data=0,
                               temporary=True)],
                        log_fn=_noop)
                    wrote = made[0] if made else None
                    if wrote is None:
                        log_fn(f"BodySlide: could not create a group for "
                               f"{len(members)} outfit(s) - skipping.")
                        return 1
                try:
                    return run_logged("BodySlide", env, args=args, log_fn=log_fn,
                                      label=f"BodySlide[{index}/{len(chunks)}]",
                                      max_rss_mb=max_rss_mb, timeout_s=timeout_s)
                finally:
                    if wrote is not None:
                        try:
                            os.unlink(wrote)
                        except OSError as exc:
                            log_fn(f"BodySlide: could not remove {wrote.name}: {exc}")

            log_fn(f"BodySlide: chunk {index}/{len(chunks)} - {chunk.label}")
            try:
                rc = run_chunk(chunk.group, list(chunk.outfits))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one chunk must not end the run
                log_fn(f"BodySlide: chunk {index} could not run - {exc}")
                failed_names.extend(chunk.outfits)
                continue
            if rc == 0:
                continue
            if rc in (-9, EXIT_MEMORY_GUARD):
                log_fn(f"BodySlide: chunk {index} ran out of memory (code {rc}).")
            elif rc == EXIT_TIMEOUT:
                log_fn(f"BodySlide: chunk {index} hit the {timeout_s:.0f}s limit.")
            else:
                log_fn(f"BodySlide: chunk {index} failed with code {rc}.")
            failed = isolate_failures(run_chunk, list(chunk.outfits),
                                      log_fn=log_fn, tag=f"retry{index}")
            if failed:
                failed_names.extend(failed)
                log_fn(f"BodySlide: gave up on {len(failed)} outfit(s) after "
                       f"isolating them: {', '.join(sorted(failed))}")
    finally:
        # always, including on KeyboardInterrupt: a leftover chunk group would
        # otherwise be read back as a real group on the next run
        for path in written:
            try:
                os.unlink(path)
            except OSError as exc:
                log_fn(f"BodySlide: could not remove {path.name}: {exc}")
    unbuildable = sorted(set(failed_names))
    if unbuildable:
        log_fn(f"BodySlide: {len(unbuildable)} outfit(s) could not be built - "
               f"everything else did: {', '.join(unbuildable)}")
    # Counts OUTFITS, not chunks: after a bisect a chunk failure is no longer a
    # unit, and the caller needs to know whether anything was left unbuilt
    # rather than how many processes it took to find out.
    return len(unbuildable)


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


def ensure_output_dir(game: "BaseGame", profile: str) -> tuple[Path | None, str]:
    """(path, reason) for the output-capture mod a build lands in.

    Same destination the BodySlide wizard uses, so an automatic build and a
    manual one write to the same mod instead of producing two. Returns a reason
    on failure rather than None alone: an earlier version swallowed a
    TypeError from a wrong-arity call and reported only "could not determine the
    output mod", which said nothing about what to fix.
    """
    try:
        from Utils.bethesda.bodyslide import ensure_output_mod, sanitize_output_name
        from Utils.bethesda.bodyslide_linux import TOOLS
        default = TOOLS["bodyslide"][2]
        name = sanitize_output_name(default, default)
        ensure_output_mod(game, profile, name)
        return game.get_effective_mod_staging_path() / name, ""
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, f"{exc!r}"


def run_automatic(game: "BaseGame", profile: str,
                  log_fn: Callable[[str], None] = _noop,
                  status_fn: "Callable[[str], None] | None" = None) -> None:
    """The whole transparent step: decide, build, record. Never raises.

    Called from the launch path immediately before the game starts. It must not
    be able to stop someone playing, so every failure is logged and swallowed -
    the worst case is a stale body, not a game that will not start.

    *status_fn* receives short user-facing lines; the launch path wires it to the
    play toast, because a step that can take minutes must not be invisible.
    """
    status = status_fn or (lambda _msg: None)
    try:
        if not is_enabled(game):
            log_fn("BodySlide: automatic build disabled in Launch settings.")
            return
        stale, why = is_stale(game, profile)
        if not stale:
            log_fn(f"BodySlide: automatic build skipped - {why}.")
            return
        log_fn(f"BodySlide: automatic build needed - {why}.")

        output_dir, reason = ensure_output_dir(game, profile)
        if output_dir is None:
            log_fn(f"BodySlide: could not prepare the output mod - {reason}")
            status("BodySlide: could not prepare its output mod; skipping")
            return

        groups = discover_groups(game)
        outfits = discover_outfits(game)
        alternatives = detect_alternatives(groups, outfits)
        skip = skipped_groups(alternatives, load_choices(game, profile))
        chunks = plan_chunks(groups, outfits, skip_groups=skip)
        log_fn(f"BodySlide: building {sum(len(c.outfits) for c in chunks)} "
               f"outfits in {len(chunks)} chunk(s)…")
        status(f"Building {sum(len(c.outfits) for c in chunks)} BodySlide "
               f"outfits ({len(chunks)} chunks)…")

        output_dir.mkdir(parents=True, exist_ok=True)
        want = _fingerprint_inputs(game, profile, groups, outfits)
        total = sum(len(c.outfits) for c in chunks)
        unbuildable = run_chunked(game, profile, output_dir=output_dir,
                                  log_fn=log_fn)
        if unbuildable and unbuildable >= total:
            log_fn(f"BodySlide: nothing built ({unbuildable} outfit(s) failed); "
                   "not recording the build so the next launch retries.")
            status("BodySlide: the build failed - see the log")
            return
        if unbuildable:
            # Recorded anyway: retrying the whole build forever because one
            # outfit cannot be built on this machine would mean rebuilding
            # everything the user's machine CAN build on every single launch.
            log_fn(f"BodySlide: recording the build, but {unbuildable} "
                   "outfit(s) could not be built - named above.")
            status(f"BodySlide: built, {unbuildable} outfit(s) failed")
        write_stamp(game, profile, want,
                    chunks=len(chunks),
                    built=total - unbuildable)
        log_fn("BodySlide: automatic build complete.")
    except Exception as exc:  # noqa: BLE001 - never block the launch
        log_fn(f"BodySlide: automatic build failed - {exc!r}")
        status("BodySlide: automatic build failed - see the log")
