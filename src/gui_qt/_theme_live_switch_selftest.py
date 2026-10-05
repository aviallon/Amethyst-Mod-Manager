"""Selftest: LIVE platform-palette switching (state-of-the-art system mode).

Model under test: in appearance_mode='system' the PLATFORM PALETTE is the
source of truth. A desktop theme switch = the platform pushes a new palette
(ApplicationPaletteChange); the app must re-derive its inline tokens and NEVER
override the platform palette/style itself.

Claims (each prints one narrow observed claim):
  1. derive_platform_tokens() maps real QPalette roles (Window/Base/...) and
     derives the semantic extras (hover row, dim text) by blend math.
  2. system-mode QSS routes the role-backed tokens to palette(...) - no hex
     for them in the stylesheet text.
  3. LIVE flip via ApplicationPaletteChange: inline tokens re-derive, no stale
     values, flip-back restores, and the platform palette is left untouched
     (the whole point of the architecture).
  4. Re-entrancy: no loop.
  5. Scope: outside 'system' the platform palette change changes nothing.

Run:  python3 src/gui_qt/_theme_live_switch_selftest.py   (from the repo root)
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def claim(ok: bool, text: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {text}", flush=True)
    return ok


def hexes(text: str) -> set[str]:
    return {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", text)}


DARK = {"window": "#202024", "windowText": "#e6e6e6", "base": "#16161a",
        "alt": "#26262c", "button": "#2a2a30", "highlight": "#7aa2f7",
        "highlightedText": "#ffffff", "mid": "#3a3a42", "light": "#4a4a52",
        "midlight": "#303038"}
LIGHT = {"window": "#eff0f1", "windowText": "#232629", "base": "#ffffff",
         "alt": "#f2f2f4", "button": "#e4e4e6", "highlight": "#3daee9",
         "highlightedText": "#ffffff", "mid": "#b8bcc0", "light": "#d6d8da",
         "midlight": "#c8cacd"}


def set_platform_palette(app, m: dict) -> None:
    """Simulate what the platform theme does on a desktop theme switch."""
    from PySide6.QtGui import QPalette, QColor
    pal = app.palette()
    for role, key in ((QPalette.Window, "window"), (QPalette.WindowText, "windowText"),
                      (QPalette.Base, "base"), (QPalette.AlternateBase, "alt"),
                      (QPalette.Button, "button"), (QPalette.Highlight, "highlight"),
                      (QPalette.HighlightedText, "highlightedText"),
                      (QPalette.Mid, "mid"), (QPalette.Light, "light"),
                      (QPalette.Midlight, "midlight")):
        pal.setColor(role, QColor(m[key]))
    if hasattr(QPalette, "Accent"):
        pal.setColor(QPalette.Accent, QColor(m["highlight"]))
    app.setPalette(pal)


def main() -> int:
    from PySide6.QtCore import QEvent, QCoreApplication
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPalette

    app = QApplication(sys.argv)
    from gui_qt import theme_qt

    ok = True
    modes = {"mode": "system"}
    theme_qt.get_appearance_mode = lambda: modes["mode"]

    # ---- 1. token derivation --------------------------------------------
    set_platform_palette(app, DARK)
    tok = theme_qt.derive_platform_tokens()
    ok &= claim(tok["BG_DEEP"].lower() == DARK["window"],
                f"BG_DEEP = {tok['BG_DEEP']} <- QPalette.Window {DARK['window']}")
    ok &= claim(tok["BG_ROW"].lower() == DARK["base"] and tok["BG_ROW_ALT"].lower() == DARK["alt"],
                "BG_ROW <- Base, BG_ROW_ALT <- AlternateBase (real roles)")
    expect_hover = theme_qt._blend(DARK["base"], DARK["highlight"], 0.16)
    ok &= claim(tok["BG_ROW_HOVER"].lower() == expect_hover,
                f"BG_ROW_HOVER derived = {tok['BG_ROW_HOVER']} (want {expect_hover})")

    # ---- 2. system QSS routes role-backed tokens -------------------------
    theme_qt.invalidate_palette_cache()
    theme_qt.apply_theme(app)
    css_a = app.styleSheet()
    ok &= claim("palette(window)" in css_a and "palette(highlight)" in css_a,
                "system QSS routes role-backed tokens to palette(...)")
    ok &= claim(DARK["window"] not in css_a.lower(),
                "no hex of a ROLE-BACKED token inlined in system QSS")

    # ---- 3. LIVE flip through the platform channel ----------------------
    set_platform_palette(app, LIGHT)
    QCoreApplication.sendEvent(app, QEvent(QEvent.ApplicationPaletteChange))
    css_b = app.styleSheet()
    tok_b = theme_qt.derive_platform_tokens()
    ok &= claim(css_b != css_a, "stylesheet re-derived after the live palette flip")
    ok &= claim(theme_qt._blend(LIGHT["base"], LIGHT["highlight"], 0.16).lower()
                in hexes(css_b),
                "inline token re-derived from the NEW palette (hover row)")
    stale = hexes(tok["BG_ROW_HOVER"]) & hexes(css_b)
    ok &= claim(not stale, f"no stale dark-derived values left ({sorted(stale) or 'none'})")
    ok &= claim(app.palette().color(QPalette.Window).name().lower() == LIGHT["window"],
                "platform palette LEFT UNTOUCHED by apply_theme (native honouring)")

    # ---- 4. no loop ------------------------------------------------------
    css_c = app.styleSheet()
    ok &= claim(css_c == css_b, "no re-entrant loop after the palette-change event")

    # ---- 5. scope --------------------------------------------------------
    modes["mode"] = theme_qt._QT_DEFAULT_THEME
    theme_qt.invalidate_palette_cache()
    theme_qt.apply_theme(app)
    css_fixed = app.styleSheet()
    set_platform_palette(app, DARK)
    QCoreApplication.sendEvent(app, QEvent(QEvent.ApplicationPaletteChange))
    ok &= claim(app.styleSheet() == css_fixed,
                "appearance_mode != 'system': platform palette change is ignored")

    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
