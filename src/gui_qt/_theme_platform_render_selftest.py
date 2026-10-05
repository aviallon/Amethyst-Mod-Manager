"""Selftest: RENDERING proof of the platform-driven theme (pixels, not eyes).

The claim this exists for: "rendering stays CORRECT across a live platform
palette switch" - measured on real rendered pixels via QWidget.grab(), a
channel this repo controls end to end (no screenshot harness, no stale
frames). A small widget gallery is themed by the QSS in system mode, the
platform palette is switched dark -> light at runtime (exactly what the
platform theme does), and each rendering is measured:

  * background pixel matches the platform palette's Window colour,
  * label text pixels CONTRAST with that background (readable),
  * the two renderings differ (the live switch actually repainted),
  * after the flip-back the rendering matches the first one again.

Run:  python3 src/gui_qt/_theme_platform_render_selftest.py   (from the repo root)
"""

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


def render_gallery():
    """A representative mini-UI: window label + list row + accent button."""
    from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QPushButton, QListWidget
    w = QWidget()
    w.setObjectName("GalleryRoot")
    lay = QVBoxLayout(w)
    title = QLabel("Welcome to Amethyst Mod Manager")
    title.setObjectName("GalleryTitle")
    lay.addWidget(title)
    lw = QListWidget()
    lw.setObjectName("GalleryList")
    lw.addItem("first row")
    lw.addItem("second row")
    lay.addWidget(lw)
    btn = QPushButton("Next")
    btn.setObjectName("GalleryAccent")
    lay.addWidget(btn)
    w.resize(360, 240)
    w.show()
    return w, title, lw, btn


def lightness(img, x0, y0, x1, y1) -> float:
    vals = [img.pixelColor(x, y).lightness()
            for x in range(x0, x1, 2) for y in range(y0, y1, 2)]
    return sum(vals) / max(len(vals), 1)


def spread(img, x0, y0, x1, y1) -> float:
    """max-min lightness in a region = the legibility contrast actually
    painted (coverage-insensitive, unlike the mean of a text band)."""
    vals = [img.pixelColor(x, y).lightness()
            for x in range(x0, x1) for y in range(y0, y1)]
    return (max(vals) - min(vals)) if vals else 0.0


def main() -> int:
    from PySide6.QtCore import QEvent, QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    from gui_qt import theme_qt

    modes = {"mode": "system"}
    theme_qt.get_appearance_mode = lambda: modes["mode"]

    ok = True
    set_platform_palette(app, DARK)
    theme_qt.invalidate_palette_cache()
    theme_qt.apply_theme(app)
    w, title, lw, btn = render_gallery()
    w.show()
    app.processEvents()

    def grab():
        return w.grab().toImage()

    def band(widget) -> tuple[int, int, int, int]:
        """Exact pixel rect of a widget inside the grab (geometry, not guesses)."""
        top = widget.mapTo(w, widget.rect().topLeft())
        return top.x(), top.y(), top.x() + widget.width(), top.y() + widget.height()

    # ---- Rendering A (dark platform) ------------------------------------
    img_a = grab()
    bg_a = lightness(img_a, 5, 5, 355, 20)          # window surface band
    tx0, ty0, tx1, ty1 = band(title)
    txt_a = lightness(img_a, tx0, ty0, tx1, ty1)   # the title label EXACTLY
    ok &= claim(abs(bg_a - 34) < 25,
                f"rendering A background follows the DARK platform (bg {bg_a:.1f} ~ #202024)")
    ok &= claim(spread(img_a, tx0, ty0, tx1, ty1) > 40,
                f"rendering A readable: title-band contrast = "
                f"{spread(img_a, tx0, ty0, tx1, ty1):.1f} lightness (>40)")

    # ---- LIVE flip to light platform ------------------------------------
    set_platform_palette(app, LIGHT)
    QCoreApplication.sendEvent(app, QEvent(QEvent.ApplicationPaletteChange))
    img_b = grab()
    bg_b = lightness(img_b, 5, 5, 355, 20)
    txt_b = lightness(img_b, tx0, ty0, tx1, ty1)
    ok &= claim(abs(bg_b - 240) < 25,
                f"rendering B background follows the LIGHT platform (bg {bg_b:.1f})")
    ok &= claim(spread(img_b, tx0, ty0, tx1, ty1) > 40,
                f"rendering B readable: title-band contrast = "
                f"{spread(img_b, tx0, ty0, tx1, ty1):.1f} lightness (>40)")
    ok &= claim(abs(bg_b - bg_a) > 80 and abs(txt_b - txt_a) > 80,
                "the live switch actually repainted (bg AND text moved)")

    # ---- Flip back: rendering matches A again ---------------------------
    set_platform_palette(app, DARK)
    QCoreApplication.sendEvent(app, QEvent(QEvent.ApplicationPaletteChange))
    img_c = grab()
    bg_c = lightness(img_c, 5, 5, 355, 20)
    ok &= claim(abs(bg_c - bg_a) < 4,
                f"flip back restores rendering A (bg {bg_c:.1f} vs {bg_a:.1f})")

    print(f"== {'ALL CLAIMS HOLD' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
