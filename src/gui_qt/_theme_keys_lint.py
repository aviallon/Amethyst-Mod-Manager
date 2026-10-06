#!/usr/bin/env python3
"""Static theme-key linter (no Qt, no runtime derivation).

Why this exists (2026-10-06): three real bugs shipped past every test at
least once -
  * a DUPLICATE dict key in derive_platform_tokens (BG_PANEL: the later
    ToolTipBase line silently overwrote the Button line),
  * MISSING keys consumed through a VARIABLE (_STATE_COLORS table) that the
    call-site scanners could not see -> white-on-white fallback,
  * undeclared/typo keys reaching _c() and falling back to #1a1a1a.

mypy alone cannot see these: the palette is a plain dict of strings. This
linter is the static layer:
  1. DUPLICATE keys in any theme dict literal (the F601 class, also covered
     by ruff);
  2. COVERAGE: every key the Theme Editor declares (= the ground truth of
     every theme key that exists) must be defined in derive_platform_tokens
     or _SEMANTIC_CONSTANTS;
  3. CALL SITES: every key literal passed to _c/qc/qc_contrast/c/ct/cf -
     including keys inside *_COLORS-style mapping tables - must belong to the
     declared universe.

Value semantics (contrast, palette-family, exact recipes) are NOT static
claims: they live in src/gui_qt/_theme_state_colors_selftest.py.

Run:  python3 src/gui_qt/_theme_keys_lint.py     (exit 0 = clean)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THEME_QT = REPO / "src" / "gui_qt" / "theme_qt.py"
THEME_EDITOR = REPO / "src" / "gui_qt" / "theme_editor_groups.py"
GUI_QT = Path(__file__).resolve().parent

KEY_FUNCS = {"_c", "qc", "qc_contrast", "c", "ct", "cf"}


def key_literals_in(node: ast.AST) -> set[str]:
    """All all-caps string literals under a node (keys and table values)."""
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.isupper() and "_" in n.value}


def dict_literal_keys(node: ast.Dict, where: str, errors: list[str]) -> set[str]:
    keys: set[str] = set()
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            if k.value in keys:
                errors.append(f"{where}: duplicate dict key {k.value!r}")
            keys.add(k.value)
    return keys


def main() -> int:
    errors: list[str] = []

    # ---- universe: the Theme Editor's declared keys (ground truth) --------
    editor_keys = key_literals_in(ast.parse(THEME_EDITOR.read_text()))

    # ---- defined: derive dict + semantic constants -----------------------
    tq = ast.parse(THEME_QT.read_text())
    defined: set[str] = set()
    for node in ast.walk(tq):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if isinstance(node.value, ast.Dict):
                defined |= dict_literal_keys(
                    node.value,
                    f"theme_qt.py:{node.lineno} ({','.join(names) or '?'})",
                    errors)

    missing = sorted(editor_keys - defined)
    if missing:
        errors.append(
            "theme keys declared in theme_editor_groups.py but NEVER defined "
            f"in theme_qt.py dicts: {missing}")

    # ---- call sites: every consumed key must be declared -----------------
    consumed: dict[str, str] = {}
    for f in sorted(GUI_QT.rglob("*.py")):
        if "_selftest" in f.name or f.name == "theme_editor_groups.py":
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if name not in KEY_FUNCS:
                continue
            # literal keys passed directly...
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.isupper() and "_" in arg.value:
                        consumed.setdefault(arg.value, f"{f.name}:{node.lineno}")
            # ...and keys inside tables passed through a variable
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    pass  # resolved below via the table sweep
        # *_COLORS-style mapping tables in this file carry indirection keys
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = key_literals_in(node)
                if len(keys) >= 2 and any(
                        k.endswith(("_BG", "_FG")) or "COLOR" in k for k in keys):
                    for k in keys:
                        consumed.setdefault(k, f"{f.name}:{node.lineno} (table)")

    undeclared = {k: w for k, w in consumed.items() if k not in defined}
    if undeclared:
        errors.append("consumed theme keys with NO definition: "
                      + ", ".join(f"{k} ({w})" for k, w in sorted(undeclared.items())))

    # ---- report ----------------------------------------------------------
    print(f"clés déclarées (Theme Editor) : {len(editor_keys)}")
    print(f"clés définies (theme_qt)       : {len(defined)}")
    print(f"clés consommées (gui_qt)       : {len(consumed)}")
    if errors:
        print()
        for e in errors:
            print(f"[FAIL] {e}")
        print("== LINT EN ÉCHEC")
        return 1
    print("== LINT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
