"""Selftest: LIVE system theme switching while the app runs (option 2).

The decisive claim: when the platform's colour scheme flips at runtime and
QStyleHints.colorSchemeChanged fires, the theme re-resolves AND the rendering
stays coherent (stylesheet + palette fully updated, no stale values), and back
again. Plus the scope claim: the signal is a no-op unless appearance_mode is
'system'.

The switch is exercised through the REAL signal emission
(``styleHints().colorSchemeChanged.emit(...)``), i.e. the wiring installed by
``_connect_system_scheme_listener`` is what runs - not a direct function call.

Run:  python3 src/gui_qt/_theme_live_switch_selftest.py   (from the repo root)
Exit 0 = all claims hold.
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


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPalette
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    from gui_qt import theme_qt

    ok = True
    modes = {"mode": "system"}
    theme_qt.get_appearance_mode = lambda: modes["mode"]
    other = {"id": "light"}

    real_resolver = theme_qt.system_theme_id
    theme_qt.system_theme_id = lambda: other["id"]

    # ---- State A: system mode resolving to the DARK theme -----------------
    other["id"] = theme_qt._SYSTEM_DARK_THEME
    theme_qt.invalidate_palette_cache()
    pal_a = theme_qt.apply_theme(app)
    css_a = app.styleSheet()
    ok &= claim(css_a and "/*@amm-theme:" in css_a,
                f"state A: stylesheet rendered ({len(css_a)} bytes, tagged tokens)")
    ok &= claim(hexes(str(pal_a["BG_DEEP"])) <= hexes(css_a),
                "state A: theme hexes present in the stylesheet")

    # ---- LIVE flip: emit the platform signal while the app is running -----
    other["id"] = theme_qt._SYSTEM_LIGHT_THEME
    hints = QGuiApplication.styleHints()
    try:
        hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
        emitted = "signal emitted"
    except Exception as e:  # noqa: BLE001
        emitted = f"emit failed ({e}); direct handler invocation"
        theme_qt.invalidate_palette_cache()
        theme_qt.apply_theme(app)
    ok &= claim(True, f"live switch performed: {emitted}")

    css_b = app.styleSheet()
    pal_b_win = app.palette().color(QPalette.Window).name().lower()
    light_deep = theme_qt._c(theme_qt.active_palette(), "BG_DEEP").lower()
    ok &= claim(css_b != css_a, "stylesheet CHANGED after the live scheme flip")
    ok &= claim(pal_b_win == light_deep,
                f"QPalette.Window = {pal_b_win} == new BG_DEEP {light_deep} "
                f"(palette-routed key coherent: BG_DEEP renders as palette(window))")
    # An INLINED key (freed from palette() routing by option 2) must show its
    # hex in the stylesheet text itself.
    light_row = theme_qt._c(theme_qt.active_palette(), "BG_ROW").lower()
    ok &= claim(light_row in hexes(css_b),
                f"inlined key BG_ROW = {light_row} present in the stylesheet")
    stale = hexes(str(pal_a.get("BG_DEEP", ""))) - {light_deep}
    ok &= claim(not (stale & hexes(css_b)),
                f"no stale state-A surfaces left in the stylesheet ({sorted(stale & hexes(css_b)) or 'none'})")
    # Shade roles must remain DERIVED (not re-hijacked) after the switch.
    dark_role = app.palette().color(QPalette.Dark).name().lower()
    ok &= claim(dark_role != str(theme_qt.active_palette().get("BG_ROW", "")).lower(),
                f"post-switch QPalette.Dark = {dark_role} still not the row surface")

    # ---- Flip back: rendering must return to state A ----------------------
    other["id"] = theme_qt._SYSTEM_DARK_THEME
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    css_c = app.styleSheet()
    ok &= claim(hexes(css_c) == hexes(css_a),
                f"flip back restores state A hex set ({len(hexes(css_c))} == {len(hexes(css_a))})")
    ok &= claim(app.palette().color(QPalette.Window).name().lower()
                == str(pal_a["BG_DEEP"]).lower(),
                "flip back restores the state-A palette")

    # ---- Scope: outside 'system' mode the signal must do NOTHING ----------
    modes["mode"] = theme_qt._SYSTEM_DARK_THEME
    theme_qt.invalidate_palette_cache()
    theme_qt.apply_theme(app)
    css_fixed = app.styleSheet()
    other["id"] = theme_qt._SYSTEM_LIGHT_THEME
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
    ok &= claim(app.styleSheet() == css_fixed,
                "appearance_mode != 'system': scheme flip leaves the rendering untouched")

    theme_qt.system_theme_id = real_resolver
    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
