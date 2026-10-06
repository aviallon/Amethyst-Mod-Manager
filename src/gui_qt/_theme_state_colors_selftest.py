"""Selftest: STATE SURFACES follow the KDE second-background rule.

The bug class (reported 2026-10-06 with a screenshot): state rows/bands paint
a SECOND contrasting background (conflict/requirement tints, separator bands,
plugin file states) and the text on top went unreadable - a light tint met the
fixed selection white in a dark theme. 35 such state tokens were consumed by
the delegates but MISSING from the derived palette, so they fell back to
arbitrary colours.

The KDE rule being encoded here (KColorScheme): the second contrasting
background is AlternateBackground (QPalette::AlternateBase) - semantic tints
are blended ON TOP of it so they keep their meaning AND the theme's contrast
family - and every background pairs with a COMPUTED readable foreground.

Claims:
  1. COVERAGE: every token any gui_qt consumer reads exists in the derived
     palette (for both a dark and a light platform palette) - nobody falls
     back to _FALLBACK.
  2. FAMILY: every state-band token stays within the AlternateBase contrast
     family (lightness close to AlternateBase) - no light patches on a dark
     theme and vice versa.
  3. PAIRING: the computed foreground of every band reaches WCAG >= 4.5.

Run:  python3 src/gui_qt/_theme_state_colors_selftest.py   (from the repo root)
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def claim(ok: bool, text: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {text}", flush=True)
    return ok


DARK = {"window": "#202024", "windowText": "#e6e6e6", "base": "#16161a",
        "alt": "#26262c", "button": "#2a2a30", "highlight": "#7aa2f7",
        "highlightedText": "#ffffff", "mid": "#3a3a42", "light": "#4a4a52",
        "midlight": "#303038"}
LIGHT = {"window": "#eff0f1", "windowText": "#232629", "base": "#ffffff",
         "alt": "#f2f2f4", "button": "#e4e4e6", "highlight": "#3daee9",
         "highlightedText": "#ffffff", "mid": "#b8bcc0", "light": "#d6d8da",
         "midlight": "#c8cacd"}


def set_platform_palette(app, m: dict) -> None:
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


def consumed_keys() -> dict[str, set[str]]:
    pat = re.compile(r"(?:qc|qc_contrast|_c|c|ct|cf)\(p,\s*['\"]([A-Z_]+)['\"]")
    out: dict[str, set[str]] = {}
    for f in (Path(__file__).resolve().parents[1] / "gui_qt").rglob("*.py"):
        if "_selftest" in f.name:
            continue
        for m in pat.finditer(f.read_text()):
            out.setdefault(m.group(1), set()).add(f.name)
    return out


# The second-contrast-background surfaces (the KDE AlternateBackground family).
BAND_KEYS = (
    "CONFLICT_HL_WIN", "CONFLICT_HL_LOSE", "CONFLICT_HL_ANCHOR",
    "REQ_HL_REQUIRES", "REQ_HL_REQUIRED_BY",
    "FILE_WIN", "FILE_LOSE", "FILE_ANCHOR", "FILE_DIM",
    "BG_GREEN_ROW", "BG_HOVER", "BG_SEP",
    "OVERWRITE_SEP_BG", "ROOT_SEP_BG",
    "PLUGIN_CYCLE_ANCHOR", "PLUGIN_CYCLE_OK_BG",
    "PLUGIN_CYCLE_WARN_BG", "PLUGIN_CYCLE_ERR_BG",
)


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor
    app = QApplication(sys.argv)
    from gui_qt import theme_qt
    from gui_qt.modlist_delegate import _contrasting_text_color, _contrast_ratio

    theme_qt.get_appearance_mode = lambda: "system"
    ok = True

    used = consumed_keys()
    for name, plat in (("dark", DARK), ("light", LIGHT)):
        set_platform_palette(app, plat)
        theme_qt.invalidate_palette_cache()
        tok = theme_qt.derive_platform_tokens()

        # 1. coverage - no consumer may hit a fallback
        missing = sorted(k for k in used
                         if k not in tok and k not in theme_qt._SYSTEM_QSS_EXPRESSIONS)
        ok &= claim(not missing,
                    f"[{name}] all {len(used)} consumed tokens are derived "
                    f"(missing: {missing or 'none'})")

        # 2. family - band lightness stays near AlternateBase's
        alt_l = QColor(plat["alt"]).lightness()
        far = {k: (QColor(tok[k]).lightness(), alt_l)
               for k in BAND_KEYS
               if k in tok and abs(QColor(tok[k]).lightness() - alt_l) > 30}
        ok &= claim(not far,
                    f"[{name}] all {len(BAND_KEYS)} state bands stay in the "
                    f"AlternateBase family (|dl| <= 30; far: {far or 'none'})")

        # 3. pairing - computed foreground reaches WCAG >= 4.5 on each band
        bad = {}
        for k in BAND_KEYS + ("TEXT_SEP",):
            bg = tok.get(k)
            if not bg or k == "TEXT_SEP":
                continue
            fg = _contrasting_text_color(bg)
            ratio = _contrast_ratio(QColor(fg), QColor(bg))
            if ratio < 4.5:
                bad[k] = round(ratio, 2)
        ok &= claim(not bad,
                    f"[{name}] every band's computed text reaches WCAG >= 4.5 "
                    f"(below: {bad or 'none'})")

    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
