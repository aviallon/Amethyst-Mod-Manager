"""Per-profile "staging changed outside Filegraph" marker.

Filegraph's deploy fast path calls :meth:`LibrarySession.ensure_ready`, which
only rebuilds a catalog that was never built. A *ready* catalog is assumed to
still describe the staging tree. Manager-owned mutations that write files
directly into a profile's staging (wizard tool output: Pandora, BodySlide,
DynDOLOD/TexGen/xLODGen, Synthesis, PGPatcher, xEdit QuickAutoClean, …) break
that assumption: the catalog stays ready but stale, so the next deploy reuses
the old winner generation and the freshly generated files are silently left
out. The user-visible symptom is a game launched with the previous output
(e.g. Pandora's 255 regenerated files missing while ``Data/`` still holds
symlinks from the earlier deploy).

Profile Groups make it worse: the group's ``mods/<entry>`` is a symlink into a
member's real folder, and ``materialize_group``'s cheap change detector
(:func:`Utils.profiles.groups._stale_group_entries`) compares the group
catalog's manifest fingerprint against the *member* catalog's. A wizard
writing through the group link updates neither catalog, so the two stale
fingerprints still match and the entry is never refreshed.

The fix is deliberately conservative: a manager-owned writer marks the profile
dirty; the next deploy (or Refresh) consumes the marker and rebuilds the
catalog from disk before planning. No marker means the fast path is unchanged,
so ordinary deploys pay nothing.
"""

from __future__ import annotations

from pathlib import Path

from Utils.atomic_write import write_atomic_text

_MARKER_NAME = "staging_dirty"


def marker_path(profile_dir) -> Path:
    """Return the marker path for *profile_dir* (may not exist)."""
    return Path(profile_dir) / _MARKER_NAME


def mark_staging_dirty(profile_dir, reason: str = "", *, log_fn=None) -> None:
    """Record that *profile_dir*'s staging changed outside the catalog.

    Idempotent and safe to call from a worker thread; overwrites the previous
    reason so the newest writer is what the deploy log names. Never raises on
    an unwritable profile dir (a missed marker is a performance problem, not a
    correctness one - the next Refresh rebuilds regardless).
    """
    if profile_dir is None:
        return
    try:
        write_atomic_text(marker_path(profile_dir), (reason or "").strip() + "\n")
    except OSError as exc:
        if log_fn is not None:
            log_fn(f"Could not mark '{Path(profile_dir).name}' staging dirty: {exc}")


def staging_dirty_reason(profile_dir) -> "str | None":
    """Return the pending reason, or None when the profile is not marked."""
    if profile_dir is None:
        return None
    try:
        return marker_path(profile_dir).read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def consume_staging_dirty(profile_dir) -> "str | None":
    """Read and clear the marker. Returns the reason, or None if unmarked.

    Call *after* the catalog has been reconciled, not before - an aborted
    deploy must leave the marker in place so the next attempt still refreshes.
    """
    reason = staging_dirty_reason(profile_dir)
    if reason is None:
        return None
    try:
        marker_path(profile_dir).unlink()
    except OSError:
        pass
    return reason


__all__ = [
    "mark_staging_dirty", "staging_dirty_reason", "consume_staging_dirty",
    "marker_path",
]
