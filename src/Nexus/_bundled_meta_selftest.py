"""Selftest: the mod's bundled root meta.ini is honoured ONLY for modl:// installs.

Claims proven here (each prints one narrow observed claim):
  1. bundled_meta_from_archive parses the root meta.ini of a zip (and a
     tar.gz) into the NexusModMeta fields (name/version/author/...).
  2. An archive without a root meta.ini yields None (thin metadata fallback).
  3. A garbage/empty meta.ini yields None.
  4. SCOPE A/B: _write_install_meta WITH prebuilt_meta (what the modl handler
     passes) writes the rich fields; WITHOUT it (every other install source)
     the same archive yields only the thin local stamps. Only
     gui_qt/app.py::_on_modl_download_done passes bundled metadata.

Run:  python3 src/Nexus/_bundled_meta_selftest.py   (from the repo root)
Exit 0 = all claims hold.
"""

import io
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Nexus.nexus_meta import (  # noqa: E402
    bundled_meta_from_archive, meta_from_ini_text)

RICH_INI = """[General]
gamename = skyrimspecialedition
version = 9.9.9
author = Test Author
nexusname = Test Mod
nexusurl = https://example.invalid/mods/1
description = A test mod.
filecategory = MAIN
nexusrequirements = 30379:SKSE64
fomod = true
"""


def claim(ok: bool, text: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {text}")
    return ok


def make_zip(path: Path, meta: str | None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SKSE/Plugins/dummy.dll", b"MZ")
        if meta is not None:
            z.writestr("meta.ini", meta)
    return path


def test_parse() -> bool:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        z = make_zip(Path(td) / "m.zip", RICH_INI)
        m = bundled_meta_from_archive(z)
        ok &= claim(m is not None, "zip with meta.ini -> parsed")
        ok &= claim(m is not None and m.nexus_name == "Test Mod",
                    f"nexus_name = {getattr(m, 'nexus_name', None)!r} (want 'Test Mod')")
        ok &= claim(m is not None and m.version == "9.9.9",
                    f"version = {getattr(m, 'version', None)!r} (want '9.9.9')")
        ok &= claim(m is not None and m.author == "Test Author"
                    and m.nexus_requirements == "30379:SKSE64",
                    "author + nexusRequirements carried")

        tgz = Path(td) / "m.tar.gz"
        with tarfile.open(tgz, "w:gz") as t:
            data = RICH_INI.encode()
            info = tarfile.TarInfo("meta.ini")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        m2 = bundled_meta_from_archive(tgz)
        ok &= claim(m2 is not None and m2.version == "9.9.9",
                    f"tar.gz with meta.ini -> version = {getattr(m2, 'version', None)!r}")

        none_zip = make_zip(Path(td) / "bare.zip", None)
        ok &= claim(bundled_meta_from_archive(none_zip) is None,
                    "zip without meta.ini -> None (thin fallback)")

        bad_zip = make_zip(Path(td) / "bad.zip", "[General]\nrandom = 1\n")
        ok &= claim(bundled_meta_from_archive(bad_zip) is None,
                    "meta.ini with no useful field -> None (not a source)")
    return ok


def test_scope_ab() -> bool:
    """The decisive claim: rich metadata appears ONLY when the caller passes
    the bundled meta as prebuilt_meta (the modl handler does; nobody else)."""
    from Utils.mods.install import _write_install_meta

    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        archive = make_zip(root / "TestMod-9.9.9.zip", RICH_INI)
        log = lambda *a, **k: None  # noqa: E731

        # A) modl-style: bundled meta handed over as prebuilt_meta.
        dest_a = root / "mod_a"
        dest_a.mkdir()
        bundled = bundled_meta_from_archive(archive)
        _write_install_meta(dest_a, archive, None, log, prebuilt_meta=bundled)
        text_a = (dest_a / "meta.ini").read_text()
        m_a = meta_from_ini_text(text_a)
        ok &= claim(m_a.nexus_name == "Test Mod" and m_a.version == "9.9.9",
                    f"WITH bundled meta -> name={m_a.nexus_name!r} version={m_a.version!r}")
        ok &= claim(m_a.installation_file == archive.name,
                    f"local stamps still applied: installationfile={m_a.installation_file!r}")

        # B) every other source: no prebuilt_meta -> thin stamps only.
        dest_b = root / "mod_b"
        dest_b.mkdir()
        _write_install_meta(dest_b, archive, None, log, prebuilt_meta=None)
        text_b = (dest_b / "meta.ini").read_text()
        m_b = meta_from_ini_text(text_b)
        ok &= claim(not m_b.nexus_name and not m_b.version,
                    f"WITHOUT bundled meta -> name={m_b.nexus_name!r} version={m_b.version!r} "
                    f"(bundled file NOT consulted: scope respected)")
    return ok


def main() -> int:
    ok = test_parse()
    ok &= test_scope_ab()
    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
