#!/usr/bin/env python3
"""Fail if an inline `on*` handler names a function the frontend does not export.

The app's screens are ES modules, so a function declared in one of them is not
on ``window``. Inline handler attributes -- ``onclick="showSection('library')"``
in the markup, and the same thing inside the template literals the screens use
to generate markup -- are evaluated by the browser against ``window`` and
nothing else. ``main.js`` therefore ends with an explicit
``Object.assign(window, {...})`` block naming every function they call.

Getting that list wrong fails in the worst possible way: the page renders
perfectly and the button does nothing, with no error until someone clicks it.
Neither eslint nor the Python test suite can see it -- eslint does not read
HTML attributes, and no test drives a browser. Hence this check, which reads
both sides and compares them.

Usage:  python3 tools/dev/check_handlers.py [--list]
Exits 0 when they agree, 1 when they do not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent.parent / "trove" / "web"
INDEX = WEB / "index.html"
JS_DIR = WEB / "static" / "js"
MAIN = JS_DIR / "main.js"

# An attribute like onclick="..." or onerror='...'. The body is scanned rather
# than parsed: it is a JS expression, but the only thing we need from it is
# which names it calls.
_ATTR = re.compile(r"""\bon([a-z]+)\s*=\s*(["'])(.*?)\2""", re.S)
# `foo(` not preceded by a dot -- i.e. a call on a bare name, not a method.
# `this.remove()` and `event.stopPropagation()` are method calls on the element
# or the event; they resolve without `window` and must not be required here.
_CALL = re.compile(r"(?:^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(")
# Reserved words that can precede a `(` and are not functions.
_KEYWORDS = {"if", "for", "while", "return", "typeof", "new", "function", "catch", "switch"}

_EXPORT_BLOCK = re.compile(r"Object\.assign\(window,\s*\{(.*?)\}\s*\)\s*;", re.S)
_NAME = re.compile(r"[A-Za-z_$][\w$]*")


def without_interpolations(body: str) -> str:
    """An attribute body with every ``${...}`` span removed.

    The screens build their markup inside template literals, so an attribute
    can read ``onclick="openDocs('${esc(reader.docs)}')"``. Those are two
    different moments: ``esc`` runs while the *string* is being built, in the
    module, where it is imported normally; only ``openDocs`` is looked up on
    ``window`` when the button is clicked. Scanning the raw body counts both
    and demands an export for a function that never needed one.

    Brace-counted rather than a non-greedy regex, because an interpolation may
    contain braces of its own -- a nested template literal, or an object -- and
    stopping at the first ``}`` would leave its tail behind to be rescanned.

    A handler whose *name* is interpolated (``onclick="${fn}()"``) disappears
    with the rest, which is right: nothing static can say what it resolves to,
    and a guess would be the false positive this function exists to remove.
    """
    out: list[str] = []
    i, end = 0, len(body)
    while i < end:
        if body.startswith("${", i):
            depth = 1
            i += 2
            while i < end and depth:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def handler_names() -> dict[str, set[str]]:
    """Every bare-function name called from an inline handler, by source file."""
    found: dict[str, set[str]] = {}
    for path in [INDEX, *sorted(JS_DIR.glob("*.js"))]:
        names = set()
        for _event, _quote, body in _ATTR.findall(path.read_text()):
            names |= {n for n in _CALL.findall(without_interpolations(body)) if n not in _KEYWORDS}
        if names:
            found[path.name] = names
    return found


def exported_names() -> set[str]:
    """The names main.js puts on window."""
    m = _EXPORT_BLOCK.search(MAIN.read_text())
    if not m:
        sys.exit(f"{MAIN}: no Object.assign(window, {{...}}) block found")
    # The block is a shorthand object literal, so every identifier in it is a
    # key whose value is the same name. Comments are stripped first.
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return set(_NAME.findall(body))


def main() -> int:
    by_file = handler_names()
    used: set[str] = set().union(*by_file.values()) if by_file else set()
    exported = exported_names()

    if "--list" in sys.argv:
        for name in sorted(used):
            print(name)
        return 0

    missing = sorted(used - exported)
    extra = sorted(exported - used)
    if missing:
        print("Inline handlers call functions that main.js does not export:")
        for name in missing:
            where = ", ".join(f for f, ns in sorted(by_file.items()) if name in ns)
            print(f"  {name}  (called from {where})")
    if extra:
        print("main.js exports functions no inline handler calls:")
        for name in extra:
            print(f"  {name}")
    if missing or extra:
        print(
            f"\n{len(used)} handler functions, {len(exported)} exported. "
            "Update the Object.assign(window, {...}) block at the end of main.js."
        )
        return 1
    print(f"OK: {len(used)} inline handler functions, all exported by main.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
