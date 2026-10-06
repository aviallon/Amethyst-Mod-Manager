"""Qt theming - builds a QSS stylesheet from the existing theme palettes.

The palette data in ``themes/*.py`` is plain ``{KEY: "#hex"}`` dicts
(toolkit-neutral), so the Qt app reuses it directly rather than duplicating
colours. Per-theme overrides flow through the same ``THEME_DEFAULTS_OVERRIDE``
mechanism the Tk app uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gui_qt.theme_keys import ThemeKey

from pathlib import Path
import re
import weakref
from collections.abc import Iterable
from typing import TYPE_CHECKING, Callable

from themes import load_palettes
from Utils.ui.config import get_appearance_mode

if TYPE_CHECKING:
    from PySide6.QtGui import QColor, QPalette


# Fallback used if a palette is missing a key, so QSS never renders with an
# empty colour string.
_FALLBACK = "#1a1a1a"

_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


def _icon_url(name: str) -> str:
    """Forward-slash absolute path to icons/<name> for QSS `image: url(...)`
    (QSS wants POSIX separators even on the path form)."""
    return _ICONS_DIR.joinpath(name).as_posix()


def _tinted_icon_url(name: str, color: str) -> str:
    """Return a POSIX path to a recoloured copy of icons/<name> for QSS
    `image: url(...)`.

    QSS `url()` can't tint a PNG, so we bake a tinted copy once per (name,
    color) into a temp cache dir and hand back its path. The alpha shape of the
    source glyph is preserved; opaque pixels are filled with *color*.
    Falls back to the untinted icon if the source is missing or Qt can't paint.
    """
    src = _ICONS_DIR / name
    if not src.is_file():
        return _icon_url(name)
    import tempfile
    cache_dir = Path(tempfile.gettempdir()) / "amethyst_tinted_icons"
    safe_color = color.lstrip("#").lower() or "none"
    out = cache_dir / f"{Path(name).stem}_{safe_color}.png"
    if not out.is_file():
        from PySide6.QtGui import QPixmap, QPainter, QColor
        from PySide6.QtCore import Qt
        pm = QPixmap(str(src))
        if pm.isNull():
            return _icon_url(name)
        tinted = QPixmap(pm.size())
        tinted.fill(Qt.transparent)
        p = QPainter(tinted)
        p.drawPixmap(0, 0, pm)          # original - for its alpha shape
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(tinted.rect(), QColor(color))
        p.end()
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not tinted.save(str(out), "PNG"):
            return _icon_url(name)
    return out.as_posix()


# The Qt app defaults to the signature "amethyst" palette (violet accent over
# cool near-black zinc). The original near-black "dark" palette with the blue
# #0078d4 accent stays available as appearance_mode = "dark", as does Breeze
# Dark as "breeze". Existing installs keep "dark" - Utils.ui.config pins it.
_QT_DEFAULT_THEME = "amethyst"


# Memoised active_palette() result. Model data() methods call it per cell per
# repaint, and an uncached call rescans the theme packages + parses config each
# time. Invalidated by apply_theme() and wherever the theme/appearance is
# changed (settings / theme editor save paths).
_active_palette_cache: dict | None = None

# Palette values embedded in a widget-local stylesheet carry a tiny CSS comment
# describing the palette role that produced them.  Qt preserves comments in
# QWidget.styleSheet(), which lets apply_theme() regenerate stylesheets that
# were assembled by a view at construction time without rebuilding that view.
# This is the scalable half of live theming; delegates/models/icons that cache
# non-CSS objects use bind_theme() below.
_THEME_TOKEN_RE = re.compile(
    r"(?:#[0-9a-fA-F]{3,8}|[A-Za-z][A-Za-z0-9_-]*)"
    r"/\*@amm-theme:(?P<key>[A-Z0-9_]+):"
    r"(?P<transform>direct|contrast|lighten)\*/")


class _ThemeValue(str):
    """A normal colour string that retains its palette role in f-strings.

    QColor, comparisons and ordinary string operations see the plain ``#hex``
    value.  Formatting it into QSS additionally emits a valid CSS comment, so
    the value can be replaced accurately even when several roles shared the
    same old colour and diverge in the new theme.
    """

    theme_key: str
    theme_transform: str
    def __new__(cls, value: str, key: str, transform: str = "direct"):
        obj = super().__new__(cls, value)
        obj.theme_key = key
        obj.theme_transform = transform
        return obj

    def __format__(self, spec: str) -> str:
        value = super().__format__(spec)
        return (f"{value}/*@amm-theme:{self.theme_key}:"
                f"{self.theme_transform}*/")


# id(owner) -> (weak owner, [(updater kind, updater, roles), ...]). Updaters are
# weak when they are bound methods; unbound callbacks receive (owner, palette),
# so they need not close over the owner and accidentally keep it alive.  A
# binding may declare the semantic roles it consumes, allowing single-colour
# editor changes to avoid unrelated model resets, icon work and rich-text
# rendering.
_theme_bindings: dict[
    int,
    tuple[weakref.ReferenceType,
          list[tuple[str, object, frozenset[str] | None]]],
] = {}
_applied_base_style_name: str | None = None

# Changing any of these roles changes a QPalette role.  Most of the editor's
# specialised colours (conflict bands, status pills, etc.) do not, so avoid a
# costly PaletteChange cascade through every widget for those edits.
_QPALETTE_ROLES = frozenset({
    "BG_DEEP", "TEXT_MAIN", "BG_LIST", "BG_ROW_ALT", "BG_HEADER",
    "BG_SELECT", "TEXT_ON_ACCENT", "BG_PANEL", "TEXT_FAINT",
    "LINK_BLUE", "BORDER_FAINT", "BORDER_DIM", "BORDER",
    "BG_ROW", "BG_ROW_HOVER", "TEXT_DIM", "ACCENT", "ACCENT_HOV",
})

# Application QSS can refer to QPalette roles instead of embedding a literal.
# Palette-only updates are substantially cheaper than resetting the whole app
# stylesheet, so give the most widely used semantic colours stable Qt roles.
# LinkVisited and BrightText are intentionally used for accent hover/contrast;
# the app does not expose visited-link styling and these meanings match their
# normal visual purpose closely.
_QSS_PALETTE_EXPRESSIONS = {
    "BG_DEEP": "palette(window)",
    "TEXT_MAIN": "palette(window-text)",
    "BG_LIST": "palette(base)",
    "BG_ROW_ALT": "palette(alternate-base)",
    "BG_HEADER": "palette(button)",
    # BG_ROW / BG_ROW_HOVER removed from this map on purpose (option 2,
    # 2026-10-02): they aliased QPalette.Dark/Shadow and required hijacking
    # Fusion's shading roles. See the note below the map.
    "BG_SELECT": "palette(highlight)",
    "TEXT_ON_ACCENT": "palette(highlighted-text)",
    # Qt's stylesheet role is `tooltip-base` (unlike the QPalette enum name
    # ToolTipBase). `tool-tip-base` is silently treated as Window, which made
    # panel-backed floating cards appear to have no background of their own.
    "BG_PANEL": "palette(tooltip-base)",
    "TEXT_FAINT": "palette(placeholder-text)",
    "LINK_BLUE": "palette(link)",
    "ACCENT": "palette(accent)",
    "ACCENT_HOV": "palette(link-visited)",
    # BORDER_FAINT / BORDER_DIM / BORDER removed for the same reason:
    # Light/Midlight/Mid must keep their Qt shading meaning.
}

# NOTE (option 2, 2026-10-02): five keys - BG_ROW, BG_ROW_HOVER, BORDER_FAINT,
# BORDER_DIM, BORDER - were deliberately removed from _QSS_PALETTE_EXPRESSIONS.
# They used to route through QPalette.Dark/Shadow/Light/Midlight/Mid, which
# forced build_qpalette() to hijack those roles and broke every consumer using
# their real meaning (Fusion bevels/frames, scrollbar shading, dialogs). With
# no entry here, the renderer falls back to the tagged inline value (_c),
# which _render_theme_tokens keeps current exactly like every other theme key.

# ---------------------------------------------------------------------------
# SYSTEM MODE (state of the art, 2026-10-05): follow the platform palette.
#
# On KDE the platform palette IS KColorScheme's output (Breeze parity,
# third-party schemes, high-contrast), so deriving everything from it is the
# correct KDE support - no extra dependency, and app.setPalette() is NEVER
# called in this mode (native dialogs and widgets stay exactly the desktop).
# Every token that has a real Qt role routes to it; the few semantic extras
# (hover row, dim text) are derived from the palette at render time; status
# colours (ok/warn/error) stay semantic constants by design - they must not
# repaint with the theme.
# ---------------------------------------------------------------------------

_SYSTEM_QSS_EXPRESSIONS = {
    "BG_DEEP": "palette(window)",
    "BG_MAIN": "palette(window)",
    "TEXT_MAIN": "palette(window-text)",
    "BG_LIST": "palette(base)",
    "BG_ROW": "palette(base)",
    "BG_ROW_ALT": "palette(alternate-base)",
    "BG_HEADER": "palette(button)",
    "BG_SELECT": "palette(highlight)",
    "TEXT_ON_ACCENT": "palette(highlighted-text)",
    # Raised surfaces (menus, panels, bottom bar, selected tab) map to the
    # BUTTON role. The earlier tooltip-base mapping gave tooltip colours to
    # menus and the selected tab - visibly incoherent on Plasma (reported
    # 2026-10-05).
    "BG_PANEL": "palette(button)",
    "CHECK_FILL": "palette(accent)",   # checkboxes follow the accent
    "SCROLL_TROUGH": "palette(window)",
    "SCROLL_BG": "palette(mid)",
    "SCROLL_ACTIVE": "palette(accent)",
    "TEXT_FAINT": "palette(placeholder-text)",
    "LINK_BLUE": "palette(link)",
    "ACCENT": "palette(accent)",
    "BORDER": "palette(mid)",
    "BORDER_FAINT": "palette(light)",
    "BORDER_DIM": "palette(midlight)",
}

_PLATFORM_DERIVED = "_PLATFORM_DERIVED"


def _blend(a: str, b: str, t: float) -> str:
    """Linear blend of two #rrggbb colours (t toward b)."""
    def _rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    try:
        ra, rb = _rgb(a), _rgb(b)
    except Exception:
        return a
    return "#%02x%02x%02x" % tuple(
        round(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))


def _state_tint(alt_hex: str, hue_hex: str, t: float) -> str:
    """KDE-style SECOND contrasting background (KColorScheme rule, 2026-10-06):
    the semantic hue is re-projected to the AlternateBase lightness before the
    blend, so the band keeps its meaning AND lands exactly in the theme's
    contrast family - no light patches in a dark theme, none dark in a light
    one. Its paired foreground is contrast_text() of the result."""
    from PySide6.QtGui import QColor
    a, h = QColor(alt_hex), QColor(hue_hex)
    h.setHsv(h.hue(), h.saturation(), a.lightness())
    return _blend(alt_hex, h.name(), t)


def _hue_text(hue_hex: str, bg_hex: str) -> str:
    """Colored text that is GUARANTEED readable: the semantic hue kept, its
    lightness re-projected to the contrast pole of its background (what the
    explicit themes do by hand - dark green text on pastel green in light,
    bright green on deep green in dark)."""
    from PySide6.QtGui import QColor
    pole = QColor(contrast_text(bg_hex))
    h = QColor(hue_hex)
    h.setHsv(h.hue(), max(h.saturation(), 120), pole.lightness())
    return h.name()


def derive_platform_tokens() -> dict:
    """Semantic token dict derived from the LIVE platform palette.

    Only real QPalette roles are read; the derived keys are computed from
    them (hover rows, dim text, accent variants). Marked _PLATFORM_DERIVED so
    build_qss knows to use _SYSTEM_QSS_EXPRESSIONS and apply_theme knows not
    to override the application palette.
    """
    from PySide6.QtGui import QGuiApplication, QPalette
    pal = QGuiApplication.palette()
    col = lambda role: pal.color(role).name()
    win, base = col(QPalette.Window), col(QPalette.Base)
    alt = col(QPalette.AlternateBase)
    accent = col(QPalette.Accent) if hasattr(QPalette, "Accent") else col(QPalette.Highlight)
    p = {
        _PLATFORM_DERIVED: "1",
        "BG_DEEP": win,
        "BG_MAIN": win,
        "BG_PANEL": col(QPalette.Button),
        "CHECK_FILL": accent,   # checkboxes follow the ACCENT, not a fixed green
        "TEXT_MAIN": col(QPalette.WindowText),
        "BG_LIST": base,
        "BG_ROW": base,
        "BG_ROW_ALT": col(QPalette.AlternateBase),
        "BG_ROW_HOVER": _blend(base, col(QPalette.Highlight), 0.16),
        "BG_HEADER": col(QPalette.Button),
        "BG_SELECT": col(QPalette.Highlight),
        "TEXT_ON_ACCENT": col(QPalette.HighlightedText),
        "TEXT_DIM": _blend(col(QPalette.WindowText), win, 0.45),
        "TEXT_FAINT": col(QPalette.PlaceholderText),
        "LINK_BLUE": col(QPalette.Link),
        "ACCENT": accent,
        "ACCENT_HOV": _blend(accent, col(QPalette.WindowText), 0.25),
        "BORDER": col(QPalette.Mid),
        "BORDER_FAINT": col(QPalette.Light),
        "BORDER_DIM": col(QPalette.Midlight),
        "SCROLL_TROUGH": win,
        "SCROLL_BG": _blend(win, col(QPalette.WindowText), 0.22),
        "SCROLL_ACTIVE": accent,
        # ---- STATE SURFACES: the KDE way for a second contrasting bg -----
        # KColorScheme's designated second background is AlternateBackground
        # (QPalette.AlternateBase): built to stay readable with the same
        # foreground as the base. Every state tint is AlternateBase blended
        # toward its semantic hue - it keeps its meaning AND the theme's
        # contrast family (no light patches in a dark theme, reported
        # 2026-10-06). Paired foregrounds are computed from the actual band
        # so the text is always readable.
        "BG_HOVER": _blend(alt, col(QPalette.WindowText), 0.08),
        # Modlist highlight rows (user recipe, 2026-10-06): the WINNER row is
        # the accent colour (like the selected row), the LOSER row is that same
        # accent mixed 80% / 20% with the normal row background. Subtle 22%
        # tints were "not contrasted enough"; the semantic hues stay but at
        # the same strong 80% mix.
        "BG_GREEN_ROW": _blend(base, "#7ee787", 0.8),
        "CONFLICT_HL_WIN": accent,
        "CONFLICT_HL_LOSE": _blend(base, accent, 0.8),
        "CONFLICT_HL_ANCHOR": _blend(base, "#e3b341", 0.8),
        "REQ_HL_REQUIRES": _blend(base, "#a855f7", 0.8),
        "REQ_HL_REQUIRED_BY": _blend(base, "#58a6ff", 0.8),
        "FILE_WIN": _blend(base, "#7ee787", 0.8),
        "FILE_LOSE": _blend(base, "#ff7b72", 0.8),
        "FILE_ANCHOR": _blend(base, "#e3b341", 0.8),
        "FILE_DIM": _blend(alt, col(QPalette.WindowText), 0.12),
        "PLUGIN_CYCLE_ANCHOR": _state_tint(alt, "#e3b341", 0.22),
        "PLUGIN_CYCLE_OK_BG": _state_tint(alt, "#7ee787", 0.22),
        "PLUGIN_CYCLE_WARN_BG": _state_tint(alt, "#e3b341", 0.22),
        "PLUGIN_CYCLE_ERR_BG": _state_tint(alt, "#ff7b72", 0.22),
        "HIGHLIGHT_DRAG": _blend(accent, win, 0.35),
        # Separator / boundary bands (the [Overwrite] (N) bars)
        "BG_SEP": _blend(win, alt, 0.5),
        "OVERWRITE_SEP_BG": _state_tint(alt, "#e3b341", 0.16),
        "ROOT_SEP_BG": _state_tint(alt, "#58a6ff", 0.16),
        # ---- paired foregrounds: computed from the band they sit on ------
        "TEXT_SEP": contrast_text(_blend(win, alt, 0.5)),
        "OVERWRITE_SEP_FG": contrast_text(_state_tint(alt, "#e3b341", 0.16)),
        "ROOT_SEP_FG": contrast_text(_state_tint(alt, "#58a6ff", 0.16)),
        "PLUGIN_CYCLE_OK_FG": contrast_text(_state_tint(alt, "#7ee787", 0.22)),
        "PLUGIN_CYCLE_WARN_FG": contrast_text(_state_tint(alt, "#e3b341", 0.22)),
        "PLUGIN_CYCLE_ERR_FG": contrast_text(_state_tint(alt, "#ff7b72", 0.22)),
        # ---- Framework-status banner (the conditional line above the lists,
        # SKSE/BepInEx/... states) + tag chips + card texts: theme-editor keys
        # that fell back to white-on-white (reported 2026-10-06).
        "FRAMEWORK_INSTALLED_BG": _state_tint(alt, "#7ee787", 0.18),
        "FRAMEWORK_STAGED_BG": _state_tint(alt, "#e3b341", 0.18),
        "FRAMEWORK_DISABLED_BG": _state_tint(alt, "#58a6ff", 0.18),
        "FRAMEWORK_MISSING_BG": _state_tint(alt, "#ff7b72", 0.18),
        "FRAMEWORK_INSTALLED_FG": _hue_text("#7ee787", _state_tint(alt, "#7ee787", 0.18)),
        "FRAMEWORK_STAGED_FG": _hue_text("#e3b341", _state_tint(alt, "#e3b341", 0.18)),
        "FRAMEWORK_DISABLED_FG": _hue_text("#58a6ff", _state_tint(alt, "#58a6ff", 0.18)),
        "FRAMEWORK_MISSING_FG": _hue_text("#ff7b72", _state_tint(alt, "#ff7b72", 0.18)),
        "TAG_BUNDLED_BG": _state_tint(alt, "#1a5c8a", 0.15),
        "TAG_INSTALLED_BG": _state_tint(alt, "#7ee787", 0.15),
        "TAG_BUNDLED_FG": _hue_text("#1a5c8a", _state_tint(alt, "#1a5c8a", 0.15)),
        "TAG_UNORDERED_FG": _hue_text("#888888", base),
        "TAG_FOLDER": _hue_text("#1e7a8a", base),
        "TAG_BSA": _hue_text("#8a6a00", base),
        "TAG_BSA_ALT": _hue_text("#1e7a8a", base),
        "TAG_INI_PROFILE": _hue_text("#006a80", base),
        "TEXT_CARD": col(QPalette.WindowText),
        "TEXT_CARD_MED": _blend(col(QPalette.WindowText), win, 0.35),
        "TEXT_CARD_DIM": _blend(col(QPalette.WindowText), win, 0.6),
        "TEXT_MUTED": _blend(col(QPalette.WindowText), win, 0.6),
        "TEXT_TREE_FG": col(QPalette.WindowText),
        "TONE_BLUE": _hue_text("#1e5a8a", base),
        "TONE_FLAG": _hue_text("#a06a00", base),
        # ---- cards / entries / overlays / colored panels (theme-editor keys) -
        "BG_CARD": base,
        "BG_CARD_ALT": alt,
        "BG_ENTRY": base,
        "BG_SELECT_BAR": _state_tint(alt, "#58a6ff", 0.22),
        "BG_HOVER_ROW": _blend(base, col(QPalette.WindowText), 0.08),
        "BG_MOD_OPT": _state_tint(alt, "#58a6ff", 0.14),
        "BG_MOD_REQ": _state_tint(alt, "#7ee787", 0.3),
        "BG_OVERLAY_DEEP": _blend(win, col(QPalette.WindowText), 0.10),
        "BG_OVERLAY_ERR": _blend(win, "#ff7b72", 0.22),
        "BG_GREEN_DEEP": _state_tint(alt, "#7ee787", 0.25),
        "BG_RED_DEEP": _state_tint(alt, "#ff7b72", 0.25),
        "BG_ORANGE_DEEP": _state_tint(alt, "#e3b341", 0.25),
        "BG_BLUE_DEEP": _state_tint(alt, "#58a6ff", 0.25),
        "BG_DARK_BLUE": _blend(base, "#58a6ff", 0.7),
        "BG_DARK_GREEN": _blend(base, "#7ee787", 0.7),
        "BG_GREEN_TEXT": _hue_text("#7ee787", _state_tint(alt, "#7ee787", 0.25)),
        "BG_RED_TEXT": _hue_text("#ff7b72", _state_tint(alt, "#ff7b72", 0.25)),
        "BG_ORANGE_TEXT": _hue_text("#e3b341", _state_tint(alt, "#e3b341", 0.25)),
        "BG_BLUE_TEXT": _hue_text("#58a6ff", _state_tint(alt, "#58a6ff", 0.25)),
    }
    # Semantic status colours: deliberately theme-independent (a success
    # green must not repaint with the desktop) but must stay legible on the
    # current surfaces - derived contrast picks the label colour.
    for key, value in _SEMANTIC_CONSTANTS.items():
        # fill ONLY keys no palette role can provide - derived values win
        # (CHECK_FILL follows the accent in platform mode, 2026-10-05)
        p.setdefault(key, value)
    return p


# Status/action colours shared by custom themes AND system mode.
_SEMANTIC_CONSTANTS = {
    "TEXT_OK_BRIGHT": "#7ee787",
    "TEXT_WARN_BRIGHT": "#e3b341",
    "STATUS_ERR_BRIGHT": "#ff7b72",
    "BTN_SUCCESS": "#2ea043",
    "BTN_SUCCESS_HOV": "#3fb950",
    "RED_BTN": "#d73a49",
    "RED_HOV": "#f85149",
    "CHECK_FILL": "#2ea043",
    "DROPDOWN_ARROW": "#8b949e",
    # Semantic foregrounds + action fills: stable by design (status colours
    # must not repaint with the desktop; consumers pair them with a computed
    # contrasting background or use them on the theme's surfaces).
    "TEXT_OK": "#7ee787",
    "TEXT_WARN": "#e3b341",
    "TEXT_ERR": "#ff7b72",
    "TEXT_ERR_BRIGHT": "#ff7b72",
    "TEXT_WHITE": "#ffffff",
    "TONE_GREEN": "#7ee787",
    "TONE_RED": "#ff7b72",
    "TONE_CYAN": "#56b6c2",
    "TONE_BLUE_SOFT": "#58a6ff",
    "PLUGIN_CYCLE_LINK": "#58a6ff",
    "STATUS_BADGE_RED": "#d73a49",
    "STATUS_BADGE_GREEN": "#2ea043",
    "STATUS_SUCCESS_SOLID": "#2ea74d",
    "STATUS_QUEUED": "#c37800",
    "STATUS_DL_GREEN": "#2e8e40",
    "TEXT_BLACK": "#111111",
    "BTN_DANGER": "#d73a49",
    "BTN_DANGER_HOV": "#f85149",
    "BTN_INFO": "#1f6feb",
    "BTN_INFO_HOV": "#388bfd",
    "BTN_WARN": "#9e6a03",
    "BTN_WARN_HOV": "#b8860b",
    # Action-fill family (buttons): semantic, stable by design.
    "BG_BTN_SAVE": "#5a5a9a",
    "BTN_CANCEL": "#c0392b",
    "BTN_CANCEL_HOV": "#a93226",
    "BTN_DANGER_ALT": "#a83232",
    "BTN_DANGER_ALT_HOV": "#8b1a1a",
    "BTN_DANGER_DEEP": "#8b1a1a",
    "BTN_DANGER_DEEP_HOV": "#7a1a1a",
    "BTN_SUCCESS_ALT": "#3a8a3d",
    "BTN_SUCCESS_ALT_HOV": "#2e6b30",
    "BTN_SUCCESS_DEEP": "#369150",
    "BTN_SUCCESS_DEEP_HOV": "#2a6e3f",
    "BTN_WARN_DEEP": "#a07800",
    "BTN_WARN_DEEP_HOV": "#7a5a00",
    "BTN_WARN_BROWN": "#7a5200",
    "BTN_WARN_BROWN_HOV": "#5a3a00",
    "BTN_WARN_ORANGE": "#d97000",
    "BTN_WARN_ORANGE_HOV": "#b35a00",
    "BTN_INFO_DEEP": "#2070a8",
    "BTN_INFO_DEEP_HOV": "#1a5a8a",
    "BTN_PURPLE": "#8957e5",
    "BTN_PURPLE_HOV": "#7c3aed",
    "BTN_GREY": "#6b7280",
    "BTN_GREY_HOV": "#555c66",
    "BTN_GREY_ALT": "#7a8290",
    "BTN_GREY_ALT_HOV": "#646b76",
    "BTN_NEUTRAL": "#4a5568",
    "BTN_NEUTRAL_HOV": "#3a4454",
}

# These values are baked into paths to pre-tinted PNGs and therefore are not
# represented by the semantic comments in the QSS text itself.  Rebuild the
# application QSS when one changes; all other roles can be updated in-place.
_QSS_IMAGE_ROLES = frozenset({
    "CHECK_FILL", "DROPDOWN_ARROW", "TEXT_DIM", "TEXT_MAIN",
})


def bind_theme(owner, updater: Callable | None = None, *,
               roles: Iterable[str] | None = None) -> None:
    """Refresh *owner* now and whenever the runtime theme changes.

    With no *updater*, ``owner.refresh_theme(palette)`` is used.  A bound method
    may be supplied directly, or an unbound callback accepting ``(owner,
    palette)``.  Ownership is weak: closing a tab/overlay removes its binding
    without requiring explicit teardown.  ``roles`` optionally identifies the
    palette roles consumed by this updater; it is still invoked immediately,
    but later single-role changes skip it when none of those roles changed.
    """
    oid = id(owner)

    def _gone(_ref, key=oid):
        _theme_bindings.pop(key, None)

    owner_ref = weakref.ref(owner, _gone)
    kind: str
    stored: object
    if updater is None:
        kind, stored = "name", "refresh_theme"
    elif getattr(updater, "__self__", None) is owner:
        kind, stored = "weakmethod", weakref.WeakMethod(updater)
    else:
        kind, stored = "callback", updater
    entry = _theme_bindings.get(oid)
    if entry is None or entry[0]() is not owner:
        entry = (owner_ref, [])
        _theme_bindings[oid] = entry
    watched = frozenset(roles) if roles is not None else None
    entry[1].append((kind, stored, watched))
    _invoke_theme_binding(owner, kind, stored, active_palette())


def bind_theme_icon(owner, name: str, size: int, key: "ThemeKey", *,
                    degrees: int | None = None) -> None:
    """Keep a button/action icon tinted from a semantic palette role."""
    def _update(target, palette, icon_name=name, px=size, role=key,
                rotation=degrees):
        from gui_qt.icons import icon, icon_rotated
        colour = _c(palette, role)
        themed = (icon_rotated(icon_name, rotation, px, colour)
                  if rotation is not None else icon(icon_name, px, colour))
        target.setIcon(themed)

    bind_theme(owner, _update, roles={key})


def _invoke_theme_binding(owner, kind: str, stored, palette: dict) -> None:
    try:
        if kind == "name":
            callback = getattr(owner, stored, None)
            if callable(callback):
                callback(palette)
        elif kind == "weakmethod":
            callback = stored()
            if callback is not None:
                callback(palette)
        else:
            stored(owner, palette)
    except (RuntimeError, ReferenceError):
        # The C++ QObject may already have been deleted while its Python wrapper
        # is waiting for GC.  Its weak entry will disappear shortly.
        pass
    except Exception as exc:
        # A single optional view must never prevent the rest of the application
        # from adopting the new palette.
        print(f"[theme] live refresh failed for {type(owner).__name__}: {exc}",
              flush=True)


def invalidate_palette_cache() -> None:
    """Drop the memoised active palette (call after the theme changes)."""
    global _active_palette_cache
    _active_palette_cache = None


def active_palette() -> dict:
    """Return the {KEY: hex} palette for the Qt app. Defaults to the amethyst palette;
    an explicit saved appearance_mode theme wins when present. The special mode
    ``system`` resolves through :func:`system_theme_id` (follow the platform's
    light/dark scheme). (Values may be str or (light,dark) tuples; _c()
    normalises them.) Memoised - see invalidate_palette_cache()."""
    global _active_palette_cache
    if _active_palette_cache is not None:
        return _active_palette_cache
    palettes = load_palettes()
    mode = get_appearance_mode()
    if mode == "system":
        # State of the art: the platform palette is the source of truth (on
        # KDE it already is KColorScheme's output). No app-level palette or
        # style override happens in this mode.
        try:
            _active_palette_cache = derive_platform_tokens()
            return _active_palette_cache
        except Exception:
            mode = _QT_DEFAULT_THEME
    if mode == "system":
        mode = system_theme_id()
    if mode and mode in palettes:
        pal = palettes[mode]
    else:
        pal = (palettes.get(_QT_DEFAULT_THEME)
               or palettes.get("dark")
               or next(iter(palettes.values()), {}))
    _active_palette_cache = pal
    return pal


# "system" appearance mode: which theme id each platform scheme maps to.
_SYSTEM_DARK_THEME = "amethyst"
_SYSTEM_LIGHT_THEME = "light"


def system_theme_id() -> str:
    """Map the platform's colour scheme to one of our theme ids.

    Qt 6.5+ exposes the scheme via QStyleHints.colorScheme(); when it reports
    Unknown (or no GUI exists yet) we infer it from the palette the platform
    theme actually set (Window lightness). KDE's platform theme does not
    implement requestColorScheme() (programmatic forcing is a no-op there), so
    FOLLOWING the platform is the only reliable direction - hence this
    resolver rather than Qt::ColorScheme overrides.
    """
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt
        # styleHints() is a qFatal (abort, not a catchable Python exception)
        # when no QGuiApplication exists yet - guard first.
        if QGuiApplication.instance() is not None:
            scheme = QGuiApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return _SYSTEM_DARK_THEME
            if scheme == Qt.ColorScheme.Light:
                return _SYSTEM_LIGHT_THEME
    except Exception:
        pass
    try:
        from PySide6.QtGui import QGuiApplication, QPalette
        window = QGuiApplication.palette().color(QPalette.Window)
        return _SYSTEM_DARK_THEME if window.lightness() < 128 else _SYSTEM_LIGHT_THEME
    except Exception:
        return _SYSTEM_DARK_THEME


_system_listener_connected = False
_applied_system_theme_id: str | None = None
_in_retheme = False
_retheme_pending = False


def _maybe_retheme_for_system() -> None:
    """Re-theme on ANY platform palette/scheme change while mode is 'system'.

    Called from BOTH delivery channels: QStyleHints.colorSchemeChanged AND the
    application-level ApplicationPaletteChange/ThemeChange events (what Plasma
    sends on a desktop theme switch - the native palette propagates even when
    colorSchemeChanged never fires).

    Deliberately NO 'scheme id unchanged' shortcut: Plasma also pushes
    ACCENT-ONLY palette changes (same dark/light scheme), and the old guard
    silently swallowed them - the accent only caught up on app restart
    (reported 2026-10-05). Loop safety: a re-entrancy flag, plus the fact
    that apply_theme never calls setPalette/setStyle in system mode and so
    cannot re-emit ApplicationPaletteChange itself.
    """
    global _applied_system_theme_id, _in_retheme, _retheme_pending
    if get_appearance_mode() != "system":
        return
    # COALESCE bursts: Plasma's accent picker can push a palette event per
    # hover/selection, and a full QSS rebuild + repolish per event freezes the
    # UI ("not responding", reported 2026-10-06). One deferred retheme per
    # burst; and when the derived tokens are unchanged the work is skipped
    # entirely, which also breaks any event ping-pong.
    from PySide6.QtCore import QTimer
    if _retheme_pending:
        return
    _retheme_pending = True

    def _run() -> None:
        global _applied_system_theme_id, _in_retheme, _retheme_pending
        _retheme_pending = False
        if get_appearance_mode() != "system" or _in_retheme:
            return
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is None:
            return
        try:
            fresh = derive_platform_tokens()
        except Exception:
            fresh = None
        if fresh is not None and fresh == _active_palette_cache:
            return
        _in_retheme = True
        try:
            invalidate_palette_cache()
            apply_theme(app)
            _applied_system_theme_id = system_theme_id()
        finally:
            _in_retheme = False

    QTimer.singleShot(50, _run)


class _SystemSchemeWatcher:
    """Application event filter bridging Qt's palette-change channel."""

    def eventFilter(self, obj, ev):  # noqa: ANN001
        try:
            from PySide6.QtCore import QEvent
            if ev.type() in (QEvent.ApplicationPaletteChange, QEvent.ThemeChange):
                _maybe_retheme_for_system()
        except Exception:
            pass
        return False


_system_watcher_holder = [None]


def _make_watcher():
    """A real QObject subclass (NOT a QObject+mixin - PySide refuses or
    silently breaks those): eventFilter must be a virtual override."""
    from PySide6.QtCore import QObject, QEvent

    class _Watcher(QObject):
        def eventFilter(self, obj, ev):  # noqa: ANN001
            try:
                if ev.type() in (QEvent.ApplicationPaletteChange, QEvent.ThemeChange):
                    _maybe_retheme_for_system()
            except Exception:
                pass
            return False

    return _Watcher()


def _connect_system_scheme_listener() -> None:
    """Follow live platform scheme changes while appearance_mode is 'system'.

    Idempotent; called from apply_theme(). Covers both delivery channels:
    colorSchemeChanged (explicit scheme API) and ApplicationPaletteChange
    (the palette propagation Plasma performs on a desktop theme switch).
    """
    global _system_listener_connected
    if _system_listener_connected:
        return
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is None:
            return
        hints = QGuiApplication.styleHints()
    except Exception:
        return

    try:
        hints.colorSchemeChanged.connect(lambda _s=None: _maybe_retheme_for_system())
    except Exception:
        pass
    try:
        if _system_watcher_holder[0] is None:
            _system_watcher_holder[0] = _make_watcher()
        QGuiApplication.instance().installEventFilter(_system_watcher_holder[0])
    except Exception:
        pass
    _system_listener_connected = True


def _c(pal: dict, key: "ThemeKey") -> str:
    val = pal.get(key)
    if val is None:
        # Missing key: derive from the THEME (the old hardcoded dark #1a1a1a
        # left dark patches in light themes). Surfaces fall back to the theme's
        # own deepest background; text-ish keys to a readable contrast on it.
        bg = str(pal.get("BG_DEEP", _FALLBACK))
        if key.upper().startswith(("BG_", "BORDER", "ROW")):
            val = bg
        else:
            val = contrast_text(bg, dark=_FALLBACK, light="#f2f2f2")
    # Palette values may be (light, dark) tuples in some themes; take a string.
    if isinstance(val, (tuple, list)):
        val = val[-1]
    return _ThemeValue(str(val), key)


def _render_theme_tokens(stylesheet: str, pal: dict, *,
                         roles: frozenset[str] | None = None) -> str:
    """Replace tagged palette values in a previously-built stylesheet."""
    if not stylesheet or "/*@amm-theme:" not in stylesheet:
        return stylesheet
    if roles is not None and not any(
            f"@amm-theme:{key}:" in stylesheet for key in roles):
        return stylesheet

    def _replace(match: re.Match) -> str:
        key = match.group("key")
        if roles is not None and key not in roles:
            return match.group(0)
        transform = match.group("transform")
        value = _theme_value(pal, key, transform)
        # Keep the token for the next live change. str() deliberately strips
        # _ThemeValue.__format__ so the marker is emitted exactly once.
        return (f"{str(value)}/*@amm-theme:{key}:{transform}*/")

    return _THEME_TOKEN_RE.sub(_replace, stylesheet)


def _theme_value(pal: dict, key: str, transform: str = "direct") -> str:
    from typing import cast
    # Dynamic-key escape hatch: the render machinery rebuilds values from
    # parsed QSS tags (runtime strings). Static call sites must use _c()
    # directly so mypy checks their ThemeKey literals.
    value = _c(pal, cast("ThemeKey", key))
    if transform == "contrast":
        return contrast_text(value)
    if transform == "lighten":
        return _lighten(value)
    return value


def _replace_cached_palette_refs(owner, old: dict | None, new: dict, *,
                                 roles: frozenset[str] | None = None) -> None:
    """Point widget ``_pal``/``_p``-style snapshots at the runtime palette.

    Identity matching makes this safe without knowing attribute names: only
    references to the exact former active palette are replaced. Working theme
    dictionaries and unrelated application dictionaries are left untouched.
    """
    if old is None:
        return
    try:
        attrs = vars(owner)
    except (TypeError, RuntimeError):
        return
    for name, value in list(attrs.items()):
        replacement: object = None
        if value is old:
            replacement = new
        elif (isinstance(value, _ThemeValue)
              and (roles is None or value.theme_key in roles)):
            replacement = _theme_value(
                new, value.theme_key, value.theme_transform)
        if replacement is not None:
            try:
                setattr(owner, name, replacement)
            except (AttributeError, RuntimeError):
                pass


def _refresh_widget_styles(app, old: dict | None, new: dict,
                           changed_roles: frozenset[str]) -> list:
    """Refresh all currently-open widget-local QSS without rebuilding UI."""
    restyled = []
    try:
        widgets = list(app.allWidgets())
    except Exception:
        widgets = []
    for widget in widgets:
        try:
            _replace_cached_palette_refs(
                widget, old, new, roles=changed_roles)
            sheet = widget.styleSheet()
            rendered = _render_theme_tokens(
                sheet, new, roles=changed_roles)
            if rendered != sheet:
                widget.setStyleSheet(rendered)
                restyled.append(widget)
        except (RuntimeError, ReferenceError):
            pass
    return restyled


def _repolish_palette_qss(app, already_restyled: list) -> None:
    """Resolve ``palette(...)`` QSS values without resetting application QSS.

    Qt caches palette expressions when a widget is polished; a PaletteChange
    alone does not update those cached brushes. Unpolishing/polishing individual
    widgets is considerably cheaper than QApplication.setStyleSheet. Subtrees
    whose local stylesheet was just reset have already been repolished by Qt.
    """
    roots = {id(widget) for widget in already_restyled}
    try:
        widgets = list(app.allWidgets())
    except Exception:
        widgets = []
    for widget in widgets:
        try:
            current = widget
            covered = False
            while current is not None:
                if id(current) in roots:
                    covered = True
                    break
                current = current.parentWidget()
            if covered:
                continue
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
        except (RuntimeError, ReferenceError):
            pass


def _notify_theme_bindings(old: dict | None, new: dict,
                           changed_roles: frozenset[str]) -> None:
    for oid, entry in list(_theme_bindings.items()):
        owner = entry[0]()
        if owner is None:
            _theme_bindings.pop(oid, None)
            continue
        _replace_cached_palette_refs(owner, old, new, roles=changed_roles)
        for kind, stored, roles in list(entry[1]):
            if roles is not None and roles.isdisjoint(changed_roles):
                continue
            _invoke_theme_binding(owner, kind, stored, new)


def _resolved_palette_value(pal: dict, key: str) -> str:
    """Comparable runtime value for a palette role (without theme tokens)."""
    value = pal.get(key, _FALLBACK)
    if isinstance(value, (tuple, list)):
        value = value[-1]
    return str(value)


def _changed_palette_roles(old: dict | None, new: dict) -> frozenset[str]:
    """Semantic roles whose resolved values differ between two palettes."""
    if old is None:
        return frozenset(new)
    return frozenset(
        key for key in old.keys() | new.keys()
        if _resolved_palette_value(old, key) != _resolved_palette_value(new, key)
    )


def _refresh_application_stylesheet(app, old: dict | None, new: dict,
                                    changed_roles: frozenset[str]) -> bool:
    """Update application QSS with the least expensive safe operation.

    Re-rendering semantic comments avoids rebuilding the large stylesheet and,
    crucially, avoids calling QApplication.setStyleSheet when the edited role
    is not used by global QSS.  Image tint roles and palette-schema changes need
    a full rebuild because their generated URLs cannot carry CSS comments.
    """
    current = app.styleSheet()
    needs_rebuild = (
        not current
        or "/*@amm-theme:" not in current
        or old is None
        or old.keys() != new.keys()
        or not _QSS_IMAGE_ROLES.isdisjoint(changed_roles)
    )
    rendered = build_qss(new) if needs_rebuild else _render_theme_tokens(
        current, new, roles=changed_roles)
    if rendered != current:
        app.setStyleSheet(rendered)
        return True
    return False


def build_qss(pal: dict | None = None) -> str:
    """Build the application QSS from a palette (default: active palette)."""
    p = pal or active_palette()
    exprs = _SYSTEM_QSS_EXPRESSIONS if p.get(_PLATFORM_DERIVED) else _QSS_PALETTE_EXPRESSIONS
    c = lambda k: exprs.get(k, _c(p, k))
    # Auto-contrast text for a coloured fill: label visibility beats palette
    # choice, so button text is never editable - it's derived from the fill.
    ct = lambda k: ("palette(bright-text)" if k == "ACCENT"
                    else contrast_text(_c(p, k)))
    cf = lambda k, fallback: c(k) if k in p else c(fallback)
    return f"""
    QWidget {{
        color: {c('TEXT_MAIN')};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {c('BG_DEEP')}; }}
    /* Transparent by default so labels/checkboxes don't paint near-black boxes
       over their container - containers set their own background explicitly. */
    QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}

    /* Toggle indicators - blue when checked, consistent size everywhere. */
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 3px;
        background: {c('BG_DEEP')};
    }}
    QCheckBox::indicator:hover {{ border: 1px solid {c('CHECK_FILL')}; }}
    QCheckBox::indicator:checked {{
        background: {c('CHECK_FILL')};
        border: 1px solid {c('CHECK_FILL')};
        image: url({_tinted_icon_url('check_white.png', ct('CHECK_FILL'))});
    }}
    /* Radio: same look as the dropdown-menu exclusive indicator - hollow ring
       unchecked, a fully accent-filled circle when checked (border-radius = half
       the box). 14px to match QMenu::indicator. */
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 7px;
        background: {c('BG_DEEP')};
    }}
    QRadioButton::indicator:hover {{ border: 1px solid {c('ACCENT')}; }}
    QRadioButton::indicator:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 7px;
        background: {c('ACCENT')};
    }}

    /* Toolbar */
    QToolBar {{
        background: {c('BG_HEADER')};
        border: none;
        spacing: 4px;
        padding: 4px 6px;
    }}
    QToolButton {{
        background: transparent;
        color: {c('TEXT_MAIN')};
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QToolButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    QToolButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    QToolButton::menu-button {{ width: 16px; border-left: 1px solid {c('BORDER')}; }}
    QToolButton::menu-arrow {{ width: 8px; height: 8px; }}

    QToolTip {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('ACCENT')};
        border-radius: 4px;
        padding: 5px 8px;
        font-size: 13px;
    }}

    QMenu {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        padding: 5px;
    }}
    QMenu::item {{
        padding: 7px 28px 7px 12px;
        border-radius: 4px;
        margin: 1px 2px;
    }}
    QMenu::item:selected {{ background: {c('BG_SELECT')}; color: {c('TEXT_ON_ACCENT')}; }}
    /* Menu indicators. Exclusive (selector) menus = a blue-filled dot when
       checked, a hollow ring otherwise. Non-exclusive (checkable) items use the
       same blue rounded-square box as the modlist / QCheckBox indicators. */
    QMenu::indicator {{
        width: 16px; height: 16px;
        margin-left: 4px;
    }}
    QMenu::indicator:exclusive:unchecked {{
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 8px;
        background: {c('BG_DEEP')};
    }}
    QMenu::indicator:exclusive:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 8px;
        background: {c('ACCENT')};
    }}
    QMenu::indicator:non-exclusive:unchecked {{
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 3px;
        background: {c('BG_DEEP')};
    }}
    QMenu::indicator:non-exclusive:checked {{
        border: 1px solid {c('CHECK_FILL')};
        border-radius: 3px;
        background: {c('CHECK_FILL')};
        image: url({_tinted_icon_url('check_white.png', ct('CHECK_FILL'))});
    }}
    /* Submenu indicator - our own right-pointing arrow (matches the collapsed
       row indicator) in place of Qt's default triangle. */
    QMenu::right-arrow {{
        width: 12px; height: 12px;
        margin-right: 6px;
        image: url({_tinted_icon_url('right.png', c('DROPDOWN_ARROW'))});
    }}
    QMenu::separator {{ height: 1px; background: {c('BORDER')}; margin: 5px 8px; }}

    /* List / tree */
    QTreeView, QListView {{
        background: {c('BG_LIST')};
        alternate-background-color: {c('BG_ROW_ALT')};
        border: none;
        outline: none;
    }}
    QTreeView::item:selected, QListView::item:selected {{
        background: {c('BG_SELECT')};
        color: {c('TEXT_ON_ACCENT')};
    }}
    QHeaderView::section {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_MAIN')};
        padding: 5px 8px;
        border: none;
        border-right: 1px solid {c('BORDER')};
        border-bottom: 1px solid {c('BORDER')};
    }}

    /* Detachable tabs (overlay replacement) - modern flat look: rounded top,
       no boxy borders, an accent underline on the selected tab. */
    QTabWidget::pane {{ border: none; top: 0; }}
    QTabBar {{ background: {c('BG_HEADER')}; }}
    QTabBar::tab {{
        background: transparent;
        color: {c('TEXT_DIM')};
        /* extra right padding gives the close button its own square area */
        padding: 8px 10px 8px 16px;
        margin: 3px 1px 0 1px;
        border: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        /* reserve space for the selected underline so text doesn't shift */
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{
        background: {c('BG_ROW_HOVER')};
        color: {c('TEXT_MAIN')};
    }}
    QTabBar::tab:selected {{
        background: {c('BG_DEEP')};   /* merge with the pane below (folder look) */
        color: {c('TEXT_MAIN')};
        border-bottom: 2px solid {c('ACCENT')};
    }}
    /* Close button - a clear square on the right of the tab with a larger hit
       area and the same neutral hover treatment as other dismissal controls. */
    QTabBar::close-button {{
        image: url({_tinted_icon_url('close_white.png', _c(p, 'TEXT_DIM'))});
        subcontrol-position: right;
        margin: 3px 6px 3px 4px;
        border-radius: 4px;
    }}
    QTabBar::close-button:hover {{
        background: {c('BG_ROW_HOVER')};
        image: url({_tinted_icon_url('close_white.png', _c(p, 'TEXT_MAIN'))});
    }}

    /* Slim modern scrollbars - applied globally (modlist, plugins, log, …) */
    QScrollBar:vertical {{
        background: {cf('SCROLL_TROUGH', 'BG_MAIN')};
        width: 14px;
        margin: 0;
    }}
    QScrollBar:horizontal {{
        background: {cf('SCROLL_TROUGH', 'BG_MAIN')};
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {cf('SCROLL_BG', 'BORDER_FAINT')};
        border-radius: 5px;
        min-height: 28px;
        min-width: 28px;
        margin: 2px;
    }}
    QScrollBar::handle:hover {{ background: {cf('SCROLL_ACTIVE', 'TEXT_DIM')}; }}
    QScrollBar::handle:pressed {{ background: {cf('SCROLL_ACTIVE', 'TEXT_DIM')}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0; height: 0; background: none; border: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Status bar + bottom bar */
    QStatusBar {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_DIM')};
        border-top: 1px solid {c('BORDER')};
    }}
    QStatusBar::item {{ border: none; }}
    #BottomBar {{
        background: {c('BG_PANEL')};
        border-top: 1px solid {c('BORDER')};
    }}

    /* Generic buttons / inputs */
    QPushButton {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border: none;
        padding: 6px 12px;
        border-radius: 4px;
    }}
    QPushButton:hover {{ background: {c('ACCENT_HOV')}; }}
    QComboBox, QLineEdit {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    /* Replace Qt's default triangle drop-down indicator with arrow.png. */
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 20px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url({_tinted_icon_url('arrow.png', c('DROPDOWN_ARROW'))});
        width: 12px;
        height: 12px;
    }}
    QSplitter::handle {{ background: {c('BORDER')}; }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical {{ height: 6px; }}
    /* Highlight the grip on hover/drag so a fully-collapsed panel's handle is
       easy to find and grab. */
    QSplitter::handle:hover {{ background: {c('ACCENT')}; }}
    QSplitter::handle:pressed {{ background: {c('ACCENT')}; }}
    /* FOMOD wizard divider - a visible, grabbable handle. */
    #FomodSplit::handle {{ background: {c('BORDER')}; }}
    #FomodSplit::handle:horizontal {{ width: 6px; }}
    #FomodSplit::handle:hover {{ background: {c('ACCENT')}; }}
    /* FOMOD option groups - larger text + indicators for readability. */
    #FomodGroup {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #FomodGroupTitle {{ font-size: 15px; font-weight: 600; }}
    #FomodGroup QRadioButton, #FomodGroup QCheckBox {{
        font-size: 14px;
        padding: 4px 0;
        spacing: 10px;
    }}
    #FomodGroup QRadioButton::indicator,
    #FomodGroup QCheckBox::indicator {{ width: 20px; height: 20px; }}
    #FomodGroup QRadioButton::indicator,
    #FomodGroup QRadioButton::indicator:checked {{ border-radius: 10px; }}

    #StatusChip {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border-radius: 3px;
        padding: 3px 8px;
    }}
    #PlaceholderPane {{
        background: {c('BG_PANEL')};
        color: {c('TEXT_FAINT')};
    }}

    /* Add-Game picker cards */
    #GameCard {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #GameCard:hover {{ border: 1px solid {c('ACCENT')}; }}
    #GameCardName {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 12px; }}
    #GameSelectBtn {{
        background: {c('BTN_SUCCESS')}; color: {ct('BTN_SUCCESS')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameSelectBtn:hover {{ background: {c('BTN_SUCCESS_HOV')}; }}
    #GameAddBtn {{
        background: {c('ACCENT')}; color: {ct('ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameAddBtn:hover {{ background: {c('ACCENT_HOV')}; }}

    /* Header bars (left two-tier header + right play bar) */
    #HeaderBar {{
        background: {c('BG_HEADER')};
        border-bottom: 1px solid {c('BORDER')};
    }}
    /* Side-bar mode: the rule dividing bar from body runs down the inner edge,
       not along the bottom. Both sides are drawn - the outer edge is flush
       against the window, so only the inner one is ever visible. */
    #HeaderBar[vertical="true"] {{
        border-bottom: none;
        border-left: 1px solid {c('BORDER')};
        border-right: 1px solid {c('BORDER')};
    }}
    /* Bottom mode: the bar sits under the body, so the dividing rule moves to
       its top edge - a bottom border there would only hug the status bar. */
    #HeaderBar[position="bottom"] {{
        border-bottom: none;
        border-top: 1px solid {c('BORDER')};
    }}
    /* Dim caption above a tab's column header (Mod Files / Data / …). Palette-
       driven so it stays legible in light themes (was a hardcoded #aaa). */
    #HeaderCaption {{ color: {c('TEXT_DIM')}; }}
    #GroupSep {{ background: {c('BORDER')}; border: none; }}
    #ActionButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 6px 12px;
        font-size: 14px;
    }}
    /* Split buttons (with a dropdown arrow) need extra right padding so the
       label never runs under the 22px arrow section. */
    #ActionButton[split="true"] {{ padding: 6px 28px 6px 12px; }}
    /* Icon-only mode (narrow top bar, see MainWindow._sync_header_compact):
       drop the label padding so the button is a near-square glyph. Split
       buttons still reserve the arrow section on the right. */
    #ActionButton[compact="true"] {{ padding: 6px; }}
    #ActionButton[compact="true"][split="true"] {{ padding: 6px 24px 6px 6px; }}
    /* Deployed profile: only the TEXT goes green when the current selection is
       the deployed one (button chrome stays normal). */
    #ActionButton[deployed="true"] {{ color: {c('TEXT_OK_BRIGHT')}; }}
    #ActionButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* Menu open (menuOpen property) OR pressed → whole button + arrow go blue. */
    #ActionButton:pressed, #ActionButton[menuOpen="true"] {{
        background: {c('ACCENT')}; color: {ct('ACCENT')};
    }}
    /* Split-button arrow section (right of the divider), like the mockup. */
    #ActionButton::menu-button {{
        background: transparent;
        border-left: 1px solid {c('BORDER')};
        width: 22px;
        border-top-right-radius: 5px;
        border-bottom-right-radius: 5px;
    }}
    #ActionButton::menu-button:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* When the menu is open the arrow section matches the highlighted button. */
    #ActionButton[menuOpen="true"]::menu-button {{
        background: {c('ACCENT')};
        border-left: 1px solid {c('ACCENT_HOV')};
    }}
    /* Split-button dropdown arrow (QToolButton = ::menu-indicator,
       QPushButton = ::menu-arrow): use arrow.png instead of Qt's triangle. */
    #ActionButton::menu-indicator, #ActionButton::menu-arrow {{
        image: url({_tinted_icon_url('arrow.png', c('DROPDOWN_ARROW'))});
        width: 10px; height: 10px;
        subcontrol-origin: padding;
        subcontrol-position: center right;
        right: 6px;
    }}
    /* Square icon-only toolbar button (Settings). */
    #IconButton {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
    }}
    #IconButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #IconButton:pressed {{ background: {c('ACCENT')}; }}
    /* Configure-Game form body + monospace path fields + buttons. */
    #FormBody {{ background: {c('BG_DEEP')}; }}
    #FormScroll {{ background: {c('BG_DEEP')}; }}
    #FormScroll > QWidget > QWidget {{ background: {c('BG_DEEP')}; }}
    /* The four bordered card panels in the Configure-Game view. */
    #ConfigPanel {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #ConfigPanel QLabel {{ background: transparent; }}
    /* State pills on the location rows and the identity strip. The border and
       text colour are set per-widget from the status tone; only the shared
       ground lives here. */
    #StatusChip {{ background: {c('BG_ROW')}; }}
    /* Launcher picker under the game-install row - an inset strip so it reads
       as part of that row rather than as a section of its own. */
    #LauncherStrip {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
    }}
    #PathEdit {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 8px 10px;
    }}
    /* Icon tiles for choosing between installs found through multiple
       launchers. The active install follows the current theme accent. */
    #InstallChoiceButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 6px;
        padding: 6px;
    }}
    #InstallChoiceButton:hover {{
        background: {c('BG_ROW_HOVER')};
        border: 1px solid {c('ACCENT')};
    }}
    #InstallChoiceButton:pressed {{
        background: {c('BG_ROW_HOVER')};
        color: {c('TEXT_MAIN')};
    }}
    #InstallChoiceButton:checked {{
        background: {c('BG_ROW')};
        border: 2px solid {c('ACCENT')};
        padding: 5px;
    }}
    #InstallChoiceButton:checked:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* Consistent form button (Browse/Open/Scan/Reset, Cancel) - same height as
       the primary Save/Danger buttons so rows line up. */
    #FormButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 0 14px;
        min-height: 30px;
        font-size: 13px;
    }}
    #FormButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FormButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    #PrimaryButton {{
        background: {c('ACCENT')}; color: {ct('ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 18px;
        min-height: 30px; font-size: 13px;
    }}
    #PrimaryButton:hover {{ background: {c('ACCENT_HOV')}; }}
    #PrimaryButton:disabled {{ background: {c('BG_ROW')}; color: {c('TEXT_DIM')}; }}
    #DangerButton {{
        background: {c('RED_BTN')}; color: {ct('RED_BTN')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 16px;
        min-height: 30px; font-size: 13px;
    }}
    #DangerButton:hover {{ background: {c('RED_HOV')}; }}
    #FooterButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 12px;
        font-size: 12px;
    }}
    #FooterButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FooterButton:pressed {{ background: {c('ACCENT')}; color: {ct('ACCENT')}; }}
    #FooterButton:disabled {{
        background: {c('BG_ROW')};
        color: {c('TEXT_DIM')};
        border: 1px solid {c('BORDER_FAINT')};
    }}
    /* Generic "this footer button is latched on" state. */
    #FooterButton[active="true"] {{
        background: {c('ACCENT')};
        color: {ct('ACCENT')};
        border: 1px solid {c('ACCENT')};
    }}
    #FooterButton[active="true"]:hover {{ background: {c('ACCENT_HOV')}; }}
    #PlayButton {{
        background: {c('BTN_SUCCESS')};
        color: {ct('BTN_SUCCESS')};
        font-weight: 600;
        font-size: 14px;
        padding: 6px 18px;
        border: none;
        border-radius: 5px;
    }}
    #PlayButton:hover {{ background: {c('BTN_SUCCESS')}; }}
    #PlayButton[running="true"] {{
        background: {c('RED_BTN')};
        color: {ct('RED_BTN')};
    }}
    #PlayButton[running="true"]:hover {{ background: {c('RED_HOV')}; }}

    /* Bottom log panel */
    #LogBar {{
        background: {c('BG_HEADER')};
        border-top: 1px solid {c('BORDER')};
    }}
    #LogView {{
        background: {c('BG_DEEP')};
        color: {c('TEXT_MAIN')};
        border: none;
        border-top: 1px solid {c('BORDER')};
        font-family: monospace;
        font-size: 12px;
    }}

    /* Deploy/restore progress popup + notification toasts. */
    #ProgressPopup {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    QProgressBar {{
        background: {c('BG_DEEP')};
        border: none;
        border-radius: 4px;
    }}
    QProgressBar::chunk {{
        background: {c('ACCENT')};
        border-radius: 4px;
    }}
    #Toast {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #ToastDot[state="info"] {{ color: {c('ACCENT')}; }}
    #ToastDot[state="success"] {{ color: {c('TEXT_OK_BRIGHT')}; }}
    #ToastDot[state="warning"] {{ color: {c('TEXT_WARN_BRIGHT')}; }}
    #ToastDot[state="error"] {{ color: {c('STATUS_ERR_BRIGHT')}; }}
    """


def build_qpalette(p: dict) -> "QPalette":
    """Role-based QPalette for palette *p* so stock widgets (menus, combos,
    tooltips, disabled states) read the theme colours even where QSS doesn't
    reach. Applied app-wide at startup; also set on the theme editor's preview
    subtree so its stock-widget bits track the working palette."""
    from PySide6.QtGui import QPalette, QColor

    c = lambda k: QColor(_c(p, k))
    pal = QPalette()
    pal.setColor(QPalette.Window, c("BG_DEEP"))
    pal.setColor(QPalette.WindowText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Base, c("BG_LIST"))
    pal.setColor(QPalette.AlternateBase, c("BG_ROW_ALT"))
    pal.setColor(QPalette.Text, c("TEXT_MAIN"))
    pal.setColor(QPalette.Button, c("BG_HEADER"))
    pal.setColor(QPalette.ButtonText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    pal.setColor(QPalette.ToolTipBase, c("BG_PANEL"))
    pal.setColor(QPalette.ToolTipText, c("TEXT_MAIN"))
    pal.setColor(QPalette.PlaceholderText, c("TEXT_FAINT"))
    pal.setColor(QPalette.Link, c("LINK_BLUE"))
    pal.setColor(QPalette.LinkVisited, c("ACCENT_HOV"))
    pal.setColor(QPalette.Accent, c("ACCENT"))
    # Used by global QSS as the auto-contrasted label on accent fills.
    pal.setColor(QPalette.BrightText, qc_contrast(p, "ACCENT"))
    # Fusion draws bevels/frames from the Light/Midlight/Mid/Dark/Shadow shade
    # roles (their documented meaning, QPalette: Light/Dark/Mid/Midlight/Shadow
    # form the 3D shading set). They are DERIVED here from the button colour,
    # never repurposed: hijacking them as row/border surfaces (the pre-option-2
    # behaviour) broke Fusion chrome, scrollbars and native dialogs.
    btn = QColor(_c(p, "BG_HEADER"))
    pal.setColor(QPalette.Light, btn.lighter(118))
    pal.setColor(QPalette.Midlight, btn.lighter(107))
    pal.setColor(QPalette.Mid, btn.darker(112))
    pal.setColor(QPalette.Dark, btn.darker(138))
    pal.setColor(QPalette.Shadow, btn.darker(190))
    # Keep selection vivid even when the window/widget isn't focused (otherwise
    # Fusion greys the Inactive-group highlight, which looks broken in lists).
    pal.setColor(QPalette.Inactive, QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.Inactive, QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    # Disabled states (greyed text) across all relevant roles.
    dim = c("TEXT_DIM")
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.ToolTipText):
        pal.setColor(QPalette.Disabled, role, dim)
    return pal


def _apply_qpalette(app, p: dict) -> None:
    app.setPalette(build_qpalette(p))


def _make_proxy_style(base):
    """Wrap *base* QStyle in a ProxyStyle that enlarges the tab close indicator
    so the close button fills the tab height (QSS width/height alone is clamped
    by the style's PM_TabCloseIndicator metric), and shortens the tooltip
    wake-up delay app-wide (the default ~700ms feels sluggish)."""
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class _TabProxyStyle(QProxyStyle):
        def pixelMetric(self, metric, option=None, widget=None):
            if metric in (QStyle.PM_TabCloseIndicatorWidth,
                          QStyle.PM_TabCloseIndicatorHeight):
                return 22
            return super().pixelMetric(metric, option, widget)

        def styleHint(self, hint, option=None, widget=None, returnData=None):
            # Show tooltips faster: the default hover-to-show delay is ~700ms.
            if hint == QStyle.SH_ToolTip_WakeUpDelay:
                return 250
            return super().styleHint(hint, option, widget, returnData)

    proxy = _TabProxyStyle(base) if base is not None else _TabProxyStyle()
    return proxy


def _resolve_base_style_name(p: dict) -> str | None:
    """Resolve the installed QStyle name requested by *p*."""
    from PySide6.QtWidgets import QStyleFactory
    keys = {k.lower(): k for k in QStyleFactory.keys()}
    wanted = str(p.get("BASE_QSTYLE", "") or "").lower()
    # Default to Fusion (what the QSS was authored against) rather than any
    # system style. On the flatpak/Steam Deck a Breeze plugin is present and
    # would otherwise win, but Breeze draws its own QTabBar baseline/shape that
    # the QSS can't fully suppress (the stray white underline under the tabs).
    # Only honour Breeze when a theme opts in explicitly via BASE_QSTYLE.
    pick = (keys.get(wanted)
            or keys.get("fusion")
            or keys.get("breeze")
            or (QStyleFactory.keys()[0] if QStyleFactory.keys() else None))
    return pick


def _resolve_base_style(p: dict):
    """Create the installed QStyle selected for *p*."""
    from PySide6.QtWidgets import QStyleFactory
    pick = _resolve_base_style_name(p)
    return QStyleFactory.create(pick) if pick else None


def _lighten(hex_color: str, factor: float = 0.18) -> str:
    """Return *hex_color* blended toward white by *factor* (0..1) - used for the
    hover state of danger buttons so it lifts consistently regardless of theme."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    value = f"#{r:02x}{g:02x}{b:02x}"
    if isinstance(hex_color, _ThemeValue) and abs(factor - 0.18) < 0.0001:
        return _ThemeValue(value, hex_color.theme_key, "lighten")
    return value


def contrast_text(bg: str, dark: str = "#101010", light: str = "#ffffff") -> str:
    """Return whichever of *dark* / *light* reads best on the *bg* fill.

    Uses the WCAG relative-luminance threshold so a button label is always
    visible regardless of how light or dark its background is (e.g. a yellow
    or cyan fill gets dark text; a deep red fill gets light text). Falls back
    to *light* when *bg* can't be parsed."""
    h = bg.lstrip("#")
    if len(h) != 6:
        return light
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return light
    # Linearise then weight per Rec. 709 for perceived luminance.
    def _lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    value = dark if lum > 0.4 else light
    if (isinstance(bg, _ThemeValue)
            and dark == "#101010" and light == "#ffffff"):
        return _ThemeValue(value, bg.theme_key, "contrast")
    return value


def qc(pal: dict, key: "ThemeKey") -> "QColor":
    """QColor for palette *key* - shorthand for ``QColor(_c(pal, key))``,
    the incantation every delegate __init__ repeats per colour."""
    from PySide6.QtGui import QColor
    return QColor(_c(pal, key))


def _contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two "#rrggbb" colours (1.0 = identical)."""
    def _rel_lum(h):
        h = h.lstrip("#")
        if len(h) != 6:
            return 0.0
        try:
            ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        except ValueError:
            return 0.0
        def _lin(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (_lin(c) for c in ch)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def link_on(pal: dict, bg_key: "ThemeKey", text_key: "ThemeKey" = "TEXT_MAIN",
            min_ratio: float = 2.2, min_vs_text: float = 1.8) -> "QColor":
    """QColor for "this text is clickable" on the *bg_key* fill.

    Has to satisfy two constraints at once, and themes routinely break both:

    * legible against the fill - many themes derive BG_SELECT from the same
      accent as LINK_BLUE (Breeze/Adwaita/Pip-Boy use the identical value), so
      a plain accent tint disappears on a selected row; and
    * distinct from the text it replaces - monochrome themes build everything
      from one hue, so Cyberpunk's #00e5ff link on #00ffff text (and Pip-Boy's
      green on green) is invisible *as a hover cue* even though it is perfectly
      readable. Hue cannot separate those, because the theme only has one hue.

    Prefer the theme's own LINK_BLUE when it clears both. Otherwise separate it
    on the brightness axis instead - stepping toward whichever end of the range
    the fill can still carry - so single-hue themes keep their colour and still
    visibly change on hover."""
    from PySide6.QtGui import QColor
    link, bg = _c(pal, "LINK_BLUE"), _c(pal, bg_key)
    text = _c(pal, text_key)

    def _ok(c):
        return (_contrast_ratio(c, bg) >= min_ratio
                and _contrast_ratio(c, text) >= min_vs_text)

    if _ok(link):
        return QColor(link)

    # Walk the accent toward white and toward black in steps, taking the first
    # that clears the fill *and* separates from the body text. Ordering by step
    # size keeps the result as close to the theme's accent as the palette allows.
    for step in (0.25, 0.4, 0.55, 0.7, 0.85):
        for target in ("#ffffff", "#000000"):
            cand = _mix(link, target, step)
            if _ok(cand):
                return QColor(cand)

    # Nothing derived from the accent worked; fall back to the plain legible
    # text for the fill, which is at worst a readable non-cue.
    return QColor(contrast_text(bg))


def _mix(a: str, b: str, factor: float) -> str:
    """Blend *a* toward *b* by *factor* (0..1)."""
    ha, hb = a.lstrip("#"), b.lstrip("#")
    if len(ha) != 6 or len(hb) != 6:
        return a
    try:
        ca = [int(ha[i:i + 2], 16) for i in (0, 2, 4)]
        cb = [int(hb[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return a
    out = [int(x + (y - x) * factor) for x, y in zip(ca, cb)]
    return "#%02x%02x%02x" % tuple(out)


def qc_contrast(pal: dict, key: "ThemeKey") -> "QColor":
    """Auto-contrasted text QColor for the fill at palette *key* (shorthand
    for ``QColor(contrast_text(_c(pal, key)))``)."""
    from PySide6.QtGui import QColor
    return QColor(contrast_text(_c(pal, key)))


# One fixed size for every labelled in-view close button.
CLOSE_BTN_SIZE = (90, 30)


def button_qss(key: "ThemeKey", *, hover_key: "ThemeKey | None" = None,
               text_key: "ThemeKey | None" = None,
               disabled_bg_key: "ThemeKey" = "BTN_GREY",
               disabled_fg_key: "ThemeKey" = "TEXT_DIM",
               pal: dict | None = None,
               padding: str = "8px 24px") -> str:
    """Return a palette-driven ``QPushButton`` stylesheet string.

    Central builder so the many tab/wizard views that used to hardcode
    ``background:#2d6a9e``-style hex (blue "Select", green "Done", orange)
    all pull their colours from the active theme instead - which
    is what lets a monotone / high-contrast theme actually take effect.

    *key* is the palette key for the base fill; the hover is *hover_key* when
    given, otherwise the base blended toward white via :func:`_lighten`. The
    label colour is **auto-contrasted** off the fill (:func:`contrast_text`) so
    it stays visible on any theme - button text is deliberately not editable,
    since visibility matters more than the exact colour. Pass *text_key* only
    to force a specific palette key. Disabled fill/text are palette-driven."""
    if pal is None:
        pal = active_palette()
    bg = _c(pal, key)
    hover = _c(pal, hover_key) if hover_key else _lighten(bg)
    fg = _c(pal, text_key) if text_key else contrast_text(bg)
    dis_bg = _c(pal, disabled_bg_key)
    dis_fg = _c(pal, disabled_fg_key)
    return (
        f"QPushButton{{background:{bg}; color:{fg}; border:none;"
        f" padding:{padding}; border-radius:4px; font-weight:600;}}"
        f"QPushButton:hover{{background:{hover};}}"
        f"QPushButton:disabled{{background:{dis_bg}; color:{dis_fg};}}")


def ok_text(pal: dict | None = None) -> str:
    """Palette colour for success/green status labels (was hardcoded #6bc76b)."""
    return _c(pal or active_palette(), "TEXT_OK_BRIGHT")


def err_text(pal: dict | None = None) -> str:
    """Palette colour for error/red status labels (was hardcoded #e06c6c)."""
    return _c(pal or active_palette(), "TEXT_ERR_BRIGHT")


def warn_text(pal: dict | None = None) -> str:
    """Palette colour for warning/amber status labels.

    For an operation that finished but whose result needs checking - neither
    ok_text()'s "all good" nor err_text()'s "it failed".
    """
    return _c(pal or active_palette(), "TEXT_WARN_BRIGHT")


def close_button(text: str = "✕ Close", pal: dict | None = None):
    """Shared neutral close button for tab/scoped views and overlays."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    if pal is None:
        pal = active_palette()
    bg = _c(pal, "BG_ROW")
    hover = _c(pal, "BG_ROW_HOVER")
    pressed = _c(pal, "ACCENT")
    fg = _c(pal, "TEXT_MAIN")
    pressed_fg = contrast_text(pressed)
    border = _c(pal, "BORDER")
    disabled_fg = _c(pal, "TEXT_DIM")
    disabled_border = _c(pal, "BORDER_FAINT")
    btn = QPushButton(text)
    btn.setObjectName("CloseButton")
    btn.setFixedSize(*CLOSE_BTN_SIZE)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton{{background:{bg}; color:{fg}; border:1px solid {border};"
        f" border-radius:4px; padding:0 14px; font-size:13px;}}"
        f"QPushButton:hover{{background:{hover};}}"
        f"QPushButton:pressed{{background:{pressed}; color:{pressed_fg};}}"
        f"QPushButton:disabled{{background:{bg}; color:{disabled_fg};"
        f" border:1px solid {disabled_border};}}")
    return btn


def apply_theme(app, palette: dict | None = None) -> dict:
    """Apply a saved theme or an explicit non-persistent preview palette.

    ``palette is None`` clears any editor preview and reloads the theme selected
    in ``amethyst.ini``.  An explicit palette is copied before becoming the
    runtime active palette, so subsequent editor mutations do not leak into the
    UI until the editor deliberately applies them.

    Existing widgets keep their state. Their tagged local QSS is regenerated,
    then bound non-QSS consumers (delegates, models, icons and rich text) are
    notified. The resulting palette is returned for focused tests/callers.
    """
    global _active_palette_cache, _applied_base_style_name, _applied_system_theme_id
    # Follow the platform scheme while appearance_mode is 'system' (option 2).
    _connect_system_scheme_listener()
    # Record the applied system id BEFORE touching Qt: the palette work below
    # emits ApplicationPaletteChange, and the watcher must see the id already
    # updated or it would loop.
    if get_appearance_mode() == "system":
        _applied_system_theme_id = system_theme_id()
    else:
        _applied_system_theme_id = None
    old = _active_palette_cache
    if palette is None:
        _active_palette_cache = None
        p = active_palette()
    else:
        p = dict(palette)
        _active_palette_cache = p

    changed_roles = _changed_palette_roles(old, p)
    platform_mode = bool(p.get(_PLATFORM_DERIVED))
    style_name = _resolve_base_style_name(p)
    style_changed = (not platform_mode) and _applied_base_style_name != style_name

    # The editor can confirm the currently selected colour. Keep the existing
    # runtime snapshot in that case so identity-based palette references remain
    # connected for the next real update, and do no Qt work at all.
    if old is not None and not changed_roles and not style_changed:
        _active_palette_cache = old
        return old

    if style_changed:
        base = _resolve_base_style(p)
        app.setStyle(_make_proxy_style(base))
        _applied_base_style_name = style_name

    if platform_mode:
        # NEVER override the application palette or style in system mode:
        # native dialogs, widgets and the desktop's own scheme stay exact.
        pass
    elif style_changed or old is None or not _QPALETTE_ROLES.isdisjoint(changed_roles):
        _apply_qpalette(app, p)
    application_restyled = _refresh_application_stylesheet(
        app, old, p, changed_roles)
    locally_restyled = _refresh_widget_styles(app, old, p, changed_roles)
    if (not application_restyled
            and not frozenset(_QSS_PALETTE_EXPRESSIONS).isdisjoint(
                changed_roles)):
        _repolish_palette_qss(app, locally_restyled)
    _notify_theme_bindings(old, p, changed_roles)
    # Themes may ask for a CRT scanline sheet; themes that don't create no
    # widget, so this is a no-op for all but Pip-Boy.
    try:
        from gui_qt.scanline_overlay import sync as sync_scanlines
        sync_scanlines(app, p)
    except Exception as exc:
        print(f"[theme] scanline overlay failed: {exc}", flush=True)
    try:
        for top in app.topLevelWidgets():
            top.update()
    except Exception:
        pass
    return p
