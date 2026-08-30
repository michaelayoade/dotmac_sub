"""The inbox module is COMPOSED and not yet an authority, and that is checkable.

Composing `dotmac-inbox` creates `mod_inbox` and gives Sub a channel vocabulary
the module can read. It moves no authority: `public.inbox_conversations`,
`public.inbox_messages` and `public.inbox_conversation_read_states` remain the
only conversation, message and read-cursor owners until the separate cutover
slice ruled by `docs/adr/0013-inbox-conversation-authority.md`.

That sentence is the whole value of this increment, so it is asserted rather
than promised. The generic composition gate
(`tests/architecture/test_composed_module_lineages.py`) already proves the pin,
the lineage and the prerequisite binding agree. What it cannot know is that the
module is deliberately INERT here, which is what these checks add:

* exactly one module under `app/` may name `dotmac_inbox` at all, and it may
  reach only the declaration surface — never the service, the models or the
  history seam;
* nothing under `app/` imports that declaration module, so the runtime import
  graph is unchanged and the composition is reversible by removing three
  declarations.

Both fail the moment the writer slice begins, which is correct: that slice
changes this file in the same commit that changes the authority.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: The one module allowed to name the package, and the only surface it may use.
DECLARATION_MODULE = "app/services/inbox_channels.py"
ALLOWED_SUBMODULES = frozenset({"dotmac_inbox.channels"})

#: Dotted paths that reach the declaration module. Empty ON PURPOSE — see the
#: docstring. The writer slice replaces this with its adapter and says so.
ALLOWED_IMPORTERS_OF_THE_DECLARATION: frozenset[str] = frozenset()


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.append((node.lineno, node.module or ""))
    return found


def _app_files() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def test_only_the_declaration_module_names_the_inbox_package() -> None:
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line}:{module}"
        for path in _app_files()
        for line, module in _imported_modules(path)
        if module == "dotmac_inbox" or module.startswith("dotmac_inbox.")
        if path.relative_to(ROOT).as_posix() != DECLARATION_MODULE
    ]
    assert not offenders, (
        "dotmac-inbox is composed, not adopted: only "
        f"{DECLARATION_MODULE} may import it until the writer cutover slice "
        "lands.\n  " + "\n  ".join(offenders)
    )


def test_the_declaration_module_reaches_only_the_declaration_surface() -> None:
    """Importing the service or the models would be reaching for a writer.

    `dotmac_inbox.channels` is pure declaration: no session, no models, no
    kernel. Naming `dotmac_inbox.service`, `.history` or `.models` here is the
    first line of the cutover, not part of composing it.
    """

    path = ROOT / DECLARATION_MODULE
    used = {
        module
        for _, module in _imported_modules(path)
        if module == "dotmac_inbox" or module.startswith("dotmac_inbox.")
    }
    assert used, f"{DECLARATION_MODULE} declares nothing against the module"
    assert used <= ALLOWED_SUBMODULES, (
        f"{DECLARATION_MODULE} reaches beyond the declaration surface: "
        f"{sorted(used - ALLOWED_SUBMODULES)}"
    )


def test_nothing_under_app_imports_the_declaration_yet() -> None:
    """The composition is inert, so removing it is still a three-line revert."""

    target = DECLARATION_MODULE.removesuffix(".py").replace("/", ".")
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in _app_files()
        for line, module in _imported_modules(path)
        if (module == target or module.startswith(f"{target}."))
        if path.relative_to(ROOT).as_posix() not in ALLOWED_IMPORTERS_OF_THE_DECLARATION
    ]
    assert not offenders, (
        "app now depends on the inbox channel declarations. That is the "
        "writer slice; name the importer in ALLOWED_IMPORTERS_OF_THE_"
        "DECLARATION and update ADR-0013 in the same change.\n  "
        + "\n  ".join(offenders)
    )


def test_the_import_scan_would_actually_find_something() -> None:
    """A scanner that sees no imports at all passes every check above.

    Prove it reads the file it claims to read: the declaration module really
    does import the package, and the scan really does return it.
    """

    path = ROOT / DECLARATION_MODULE
    assert path.exists(), f"{DECLARATION_MODULE} is missing"
    modules = {module for _, module in _imported_modules(path)}
    assert "dotmac_inbox.channels" in modules
    assert "app.models.team_inbox" in modules
