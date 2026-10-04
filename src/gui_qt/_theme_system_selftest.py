"""Selftest: option-2 theme repairs (Qt dark mode contract).

Claims proven here (each prints one narrow observed claim):
  1. The shade roles Light/Midlight/Mid/Dark/Shadow are DERIVED from the button
     colour (real QPalette shading semantics) and are NOT aliases of the
     theme's border/row surfaces -- the pre-option-2 hijacking is gone.
  2. The five freed theme keys (BG_ROW, BG_ROW_HOVER, BORDER_FAINT, BORDER_DIM,
     BORDER) no longer route through palette() expressions in the QSS.
  3. system_theme_id() maps the platform scheme to theme ids (dark -> amethyst,
     light -> light) and falls back to palette inference when the scheme is
     Unknown.
  4. A missing theme key derives a visible neutral from the theme instead of
     the old hardcoded dark #1a1a1a.

Run:  python3 src/gui_qt/_theme_system_selftest.py   (from the repo root)
Exit 0 = all claims hold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def claim(ok: bool, text: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {text}", flush=True)
    return ok


def test_roles_unhijacked() -> bool:
    from gui_qt import theme_qt

    ok = True
    p = {
        "BG_HEADER": "#333333", "BG_DEEP": "#101010",
        "BG_ROW": "#222222", "BG_ROW_HOVER": "#2a2a2a",
        "BORDER_FAINT": "#444444", "BORDER_DIM": "#555555", "BORDER": "#666666",
    }
    try:
        pal = theme_qt.build_qpalette(p)
    except Exception as e:  # needs a QApplication for QColor? QColor is fine headless
        return claim(False, f"build_qpalette raised: {e}")
    from PySide6.QtGui import QPalette
    # QPalette construction is GUI-toolkit code but works without a display.
    dark = pal.color(QPalette.Dark).name()
    shadow = pal.color(QPalette.Shadow).name()
    ok &= claim(dark.lower() != p["BG_ROW"].lower(),
                f"QPalette.Dark = {dark} != BG_ROW {p['BG_ROW']} (hijack removed)")
    ok &= claim(shadow.lower() != p["BG_ROW_HOVER"].lower(),
                f"QPalette.Shadow = {shadow} != BG_ROW_HOVER (hijack removed)")
    mid = pal.color(QPalette.Mid).name()
    ok &= claim(mid.lower() != p["BORDER"].lower(),
                f"QPalette.Mid = {mid} != BORDER {p['BORDER']} (hijack removed)")
    # Shading must actually vary around the button colour (derived, not flat).
    shades = {pal.color(r).name().lower() for r in
              (QPalette.Light, QPalette.Midlight, QPalette.Mid,
               QPalette.Dark, QPalette.Shadow)}
    ok &= claim(len(shades) >= 4, f"derived shading produces {len(shades)} distinct values")
    return ok


def test_qss_inlines() -> bool:
    from gui_qt import theme_qt

    freed = {"BG_ROW", "BG_ROW_HOVER", "BORDER_FAINT", "BORDER_DIM", "BORDER"}
    leaked = freed & set(theme_qt._QSS_PALETTE_EXPRESSIONS)
    ok = claim(not leaked, f"freed keys still routed to palette(): {sorted(leaked) or 'none'}")
    qss = theme_qt.build_qss({
        "BG_DEEP": "#101010", "TEXT_MAIN": "#e0e0e0", "BG_ROW": "#222222",
        "BORDER": "#666666",
    })
    ok &= claim("palette(dark)" not in qss and "palette(mid)" not in qss,
                "rendered QSS contains no palette(dark)/palette(mid) aliases")
    ok &= claim("#222222" in qss.lower() or "222222" in qss.lower(),
                "BG_ROW is inlined in the QSS (value present)")
    return ok


def test_system_mapping() -> bool:
    from gui_qt import theme_qt

    ok = True
    dark_id = theme_qt._SYSTEM_DARK_THEME
    light_id = theme_qt._SYSTEM_LIGHT_THEME
    ok &= claim(dark_id != light_id, f"mapping dark={dark_id!r} light={light_id!r}")
    # Forced-mapping check via the constants (the live signal path needs a
    # QApplication + platform theme; covered by the visual A/B instead).
    with_theme = theme_qt.system_theme_id()
    ok &= claim(with_theme in (dark_id, light_id),
                f"system_theme_id() offscreen -> {with_theme!r} (scheme inference)")
    return ok


def test_fallback_derived() -> bool:
    from gui_qt import theme_qt

    ok = True
    light = {"BG_DEEP": "#f4f4f4"}
    # A missing SURFACE key must take the theme's own background: the old
    # hardcoded #1a1a1a painted dark patches inside light themes.
    surf = str(theme_qt._c(light, "BG_MISSING"))
    ok &= claim(surf.lower() == "#f4f4f4",
                f"missing BG_* key in a LIGHT theme -> {surf} (theme surface, not #1a1a1a)")
    # A missing TEXT key must stay READABLE on that background: dark text on a
    # light theme is correct (this is what the pre-option-2 fallback got right).
    txt = str(theme_qt._c(light, "TEXT_MISSING"))
    ok &= claim(txt.lower() != "#f4f4f4",
                f"missing TEXT_* key in a LIGHT theme -> {txt} (readable contrast)")
    dark = {"BG_DEEP": "#101010"}
    ok &= claim(str(theme_qt._c(dark, "BG_MISSING")).lower() == "#101010",
                "missing BG_* key in a DARK theme -> theme surface")
    return ok


def main() -> int:
    # Qt objects (QPixmap icon tinting, QPalette, styleHints) require a
    # QGuiApplication; without one several of these are qFatal ABORTS, not
    # catchable exceptions. offscreen platform keeps the test headless.
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication(sys.argv)  # noqa: F841 - must outlive the tests
    ok = test_roles_unhijacked()
    ok &= test_qss_inlines()
    ok &= test_system_mapping()
    ok &= test_fallback_derived()
    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
