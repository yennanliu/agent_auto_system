"""Static guards for the browser UI's ES modules (``ui/js/``).

There is no JS test suite and no build step, so a broken import only shows up in a
browser. These checks catch the mechanical mistakes cheaply in CI:

* an ``import`` naming a file or export that doesn't exist (fails the whole module graph),
* a module nobody loads,
* an inline ``on*="fn()"`` handler whose function isn't published on ``window``
  (module scope is not global),
* a name exported by one module and used in another without importing it.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
JS = UI / "js"
MODULES = sorted(JS.rglob("*.js"))

_IMPORT_RE = re.compile(r"^import\s*(?:\{([^}]*)\}\s*from\s*)?'([^']+)';", re.M | re.S)
_EXPORT_RE = re.compile(r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)
_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M
)
_HANDLER_RE = re.compile(r"""\bon[a-z]+=\\?["']([^"']*)""")


def _rel(p: Path) -> str:
    return p.relative_to(JS).as_posix()


def _imports(path: Path) -> list[tuple[Path, list[str]]]:
    out = []
    for names, spec in _IMPORT_RE.findall(path.read_text()):
        target = (path.parent / spec).resolve()
        out.append((target, [n.strip() for n in names.split(",") if n.strip()]))
    return out


def _exports(path: Path) -> set[str]:
    return set(_EXPORT_RE.findall(path.read_text()))


def test_index_html_loads_the_module_entry_point():
    html = (UI / "index.html").read_text()
    assert '<script type="module" src="/ui/js/main.js"></script>' in html
    assert "/ui/app.js" not in html


def test_every_import_resolves_to_an_existing_export():
    problems = []
    for mod in MODULES:
        for target, names in _imports(mod):
            if not target.is_file():
                problems.append(f"{_rel(mod)}: imports missing file {target.name}")
                continue
            missing = [n for n in names if n not in _exports(target)]
            if missing:
                problems.append(f"{_rel(mod)}: {_rel(target)} does not export {missing}")
    assert not problems, "\n".join(problems)


def test_import_specifiers_are_relative_with_js_extension():
    # Native modules have no resolver: a bare or extension-less specifier 404s.
    for mod in MODULES:
        for _names, spec in _IMPORT_RE.findall(mod.read_text()):
            assert spec.startswith(("./", "../")) and spec.endswith(".js"), (_rel(mod), spec)


def test_main_loads_every_module():
    loaded = {t for t, _ in _imports(JS / "main.js")}
    orphans = [_rel(m) for m in MODULES if m.name != "main.js" and m.resolve() not in loaded]
    assert not orphans, f"not imported by main.js (its top-level wiring never runs): {orphans}"


def test_inline_handlers_are_published_on_window():
    """Functions called from on*="…" attributes must be on the window bridge in main.js."""
    main = (JS / "main.js").read_text()
    bridge = main[main.index("Object.assign(window, {"):]
    bridge = bridge[: bridge.index("});")]
    published = set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*,", bridge, re.M))

    called = set()
    for src in [UI / "index.html", *MODULES]:
        for body in _HANDLER_RE.findall(src.read_text()):
            called |= set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", body))
    assert published, "window bridge not found in main.js"
    assert called - published == set(), (
        f"inline handlers call {sorted(called - published)} — add them to the "
        "Object.assign(window, {…}) bridge in ui/js/main.js (or use addEventListener)"
    )


@pytest.mark.parametrize("mod", MODULES, ids=_rel)
def test_cross_module_names_are_imported(mod):
    """A name another module exports must be imported before it is used here.

    Heuristic (regex, not a parser): it looks for bare-identifier uses, so property
    access (``x.name``), object keys (``name:``), CSS-ish tokens (``a-name``) and plain
    string literals are ignored. If it ever flags a genuine false positive, rename the local.
    """
    text = mod.read_text()
    local = set(_DECL_RE.findall(text))
    imported = {n for _t, names in _imports(mod) for n in names}
    foreign = set()
    for other in MODULES:
        if other != mod:
            foreign |= _exports(other)

    code = re.sub(r"^\s*//.*$", "", text, flags=re.M)  # drop full-line comments
    # drop plain single-line string literals (prose); template literals are kept
    # because their ${…} holes contain real code.
    code = re.sub(r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*\"""", "''", code)
    missing = []
    for name in sorted(foreign - local - imported):
        pat = rf"""(?<![\w.$'"#-]){re.escape(name)}(?![\w$'"-])(?!\s*:)"""
        if re.search(pat, code):
            missing.append(name)
    assert not missing, f"{_rel(mod)} uses {missing} without importing them"


async def test_ui_modules_served_as_revalidated_javascript(anon_client):
    resp = await anon_client.get("/ui/js/main.js")
    assert resp.status_code == 200
    # Browsers reject module scripts without a JS MIME type.
    assert "javascript" in resp.headers["content-type"]
    # no-cache = always revalidate, so a client never mixes old and new modules.
    assert resp.headers["cache-control"] == "no-cache"
