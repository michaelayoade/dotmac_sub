"""Static analysis behind the PostgreSQL lane isolation proof.

`scripts/ci/classify_postgresql_changes.py` exempts two kinds of path on a
claim about REACHABILITY rather than about the path itself: test packages the
lane never loads, and the `templates`/`static` trees it never reads.  ADR-0018
rule 23 says such an exemption states an enforceable premise or the region is
unmonitored rather than exempt.

This module is the enforcement, kept separate from the test that consumes it so
each detector can be driven with synthetic sources.  A detector nobody has
proven bites is not evidence, and the render/request surface is a FAMILY of
entry points -- naming only `TestClient` would leave an httpx ASGI transport,
an async client fixture, a local client wrapper, or a direct Jinja render
completely unmonitored.

Design rule throughout: anything this module cannot resolve is a FINDING, never
a silent skip.  A dynamic import it cannot follow means the closure is
incomplete, and an incomplete closure cannot support an exemption.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPOSITORY_ROOT / "tests"

# --- render / request entry-point families ---------------------------------

#: Bare call names that construct an HTTP client over the ASGI application.
_TEST_CLIENT_NAMES = frozenset({"TestClient"})

#: Call names that build an ASGI transport for httpx.
_ASGI_TRANSPORT_NAMES = frozenset({"ASGITransport"})

#: httpx client constructors. Only a finding when handed an app or a transport,
#: because an httpx client aimed at a real URL is not a render path.
_HTTPX_CLIENT_NAMES = frozenset({"AsyncClient", "Client"})
_HTTPX_ASGI_KEYWORDS = frozenset({"app", "transport"})

#: Direct template rendering, however it is reached.
_TEMPLATE_CALL_NAMES = frozenset(
    {
        "Jinja2Templates",
        "TemplateResponse",
        "render_template",
        "render_to_string",
        "get_template",
        "select_template",
    }
)
_TEMPLATE_ATTRIBUTE_NAMES = frozenset({"render", "render_async", "TemplateResponse"})
_TEMPLATE_MODULES = frozenset({"jinja2", "starlette.templating", "fastapi.templating"})

#: Modules whose import means the ASGI application itself is being built.
_ASGI_APP_MODULES = frozenset({"app.main"})
_ASGI_APP_NAMES = frozenset({"create_app"})

#: Local wrappers around any of the above. A project-defined helper called
#: `admin_client()` is exactly as much a render entry point as `TestClient()`,
#: and naming only the library form is how this guard would rot.
_CLIENT_WRAPPER_SUFFIXES = ("_client", "_testclient")
_CLIENT_WRAPPER_NAMES = frozenset({"client", "make_client", "build_client"})


class RenderFamily:
    """Stable identifiers for the recognised entry-point families."""

    test_client = "starlette_testclient"
    httpx_asgi = "httpx_asgi"
    client_wrapper = "application_client_wrapper"
    template_render = "template_render"
    asgi_app = "asgi_application_import"
    unresolvable_import = "unresolvable_import"


ALL_RENDER_FAMILIES = frozenset(
    {
        RenderFamily.test_client,
        RenderFamily.httpx_asgi,
        RenderFamily.client_wrapper,
        RenderFamily.template_render,
        RenderFamily.asgi_app,
        RenderFamily.unresolvable_import,
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    family: str
    module: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} [{self.family}] {self.detail}"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_client_wrapper(name: str) -> bool:
    return name in _CLIENT_WRAPPER_NAMES or name.endswith(_CLIENT_WRAPPER_SUFFIXES)


def find_render_entry_points(source: str, module: str) -> list[Finding]:
    """Report every request/render entry point reachable in one source file."""

    findings: list[Finding] = []
    tree = ast.parse(source, filename=module)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _TEMPLATE_MODULES or node.module.startswith("jinja2."):
                findings.append(
                    Finding(
                        RenderFamily.template_render,
                        module,
                        node.lineno,
                        f"imports templating module {node.module!r}",
                    )
                )
            if node.module in _ASGI_APP_MODULES or any(
                alias.name in _ASGI_APP_NAMES for alias in node.names
            ):
                findings.append(
                    Finding(
                        RenderFamily.asgi_app,
                        module,
                        node.lineno,
                        f"imports the ASGI application from {node.module!r}",
                    )
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _TEMPLATE_MODULES or alias.name.startswith("jinja2"):
                    findings.append(
                        Finding(
                            RenderFamily.template_render,
                            module,
                            node.lineno,
                            f"imports templating module {alias.name!r}",
                        )
                    )
                if alias.name in _ASGI_APP_MODULES:
                    findings.append(
                        Finding(
                            RenderFamily.asgi_app,
                            module,
                            node.lineno,
                            f"imports the ASGI application {alias.name!r}",
                        )
                    )
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name is None:
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            if name in _TEST_CLIENT_NAMES:
                findings.append(
                    Finding(
                        RenderFamily.test_client,
                        module,
                        node.lineno,
                        f"constructs {name}(...)",
                    )
                )
            elif name in _ASGI_TRANSPORT_NAMES:
                findings.append(
                    Finding(
                        RenderFamily.httpx_asgi,
                        module,
                        node.lineno,
                        f"constructs {name}(...)",
                    )
                )
            elif name in _HTTPX_CLIENT_NAMES and keywords & _HTTPX_ASGI_KEYWORDS:
                findings.append(
                    Finding(
                        RenderFamily.httpx_asgi,
                        module,
                        node.lineno,
                        f"constructs {name}(...) over an in-process application",
                    )
                )
            elif name in _TEMPLATE_CALL_NAMES or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _TEMPLATE_ATTRIBUTE_NAMES
            ):
                findings.append(
                    Finding(
                        RenderFamily.template_render,
                        module,
                        node.lineno,
                        f"renders a template via {name}(...)",
                    )
                )
            elif _is_client_wrapper(name):
                findings.append(
                    Finding(
                        RenderFamily.client_wrapper,
                        module,
                        node.lineno,
                        f"calls application-client wrapper {name}(...)",
                    )
                )
    return findings


# --- import closure ---------------------------------------------------------


def _literal_strings(node: ast.AST) -> list[str] | None:
    """Return the literal strings in a list/tuple node, or None if any is not."""

    if not isinstance(node, (ast.List, ast.Tuple)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        return None
    values: list[str] = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        values.append(element.value)
    return values


def find_unresolvable_imports(source: str, module: str) -> list[Finding]:
    """Report import forms this analysis cannot follow.

    An import the walker cannot resolve makes the closure incomplete, and an
    incomplete closure cannot justify an exemption. These are findings, not
    skips.
    """

    findings: list[Finding] = []
    tree = ast.parse(source, filename=module)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                findings.append(
                    Finding(
                        RenderFamily.unresolvable_import,
                        module,
                        node.lineno,
                        f"star-imports from {node.module!r}",
                    )
                )
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "__import__":
                findings.append(
                    Finding(
                        RenderFamily.unresolvable_import,
                        module,
                        node.lineno,
                        "calls __import__()",
                    )
                )
            elif name == "import_module":
                first = node.args[0] if node.args else None
                if not (
                    isinstance(first, ast.Constant) and isinstance(first.value, str)
                ):
                    findings.append(
                        Finding(
                            RenderFamily.unresolvable_import,
                            module,
                            node.lineno,
                            "calls importlib.import_module() with a non-literal name",
                        )
                    )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytest_plugins":
                    if _literal_strings(node.value) is None:
                        findings.append(
                            Finding(
                                RenderFamily.unresolvable_import,
                                module,
                                node.lineno,
                                "assigns pytest_plugins from a non-literal expression",
                            )
                        )
    return findings


def imported_module_names(source: str, module: str) -> Iterator[str]:
    """Yield every module name one source file pulls in, however it does so."""

    tree = ast.parse(source, filename=module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Call):
            # importlib.import_module("tests.x") is followed when it is literal;
            # the non-literal form is reported by find_unresolvable_imports.
            if _call_name(node) == "import_module" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    yield first.value
        elif isinstance(node, ast.Assign):
            # pytest loads `pytest_plugins` entries as modules.
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytest_plugins":
                    yield from _literal_strings(node.value) or []


def resolve_test_module(dotted: str) -> Path | None:
    """Resolve a ``tests.…`` module name to a file inside the repository."""

    if dotted != "tests" and not dotted.startswith("tests."):
        return None
    relative = Path(*dotted.split("."))
    candidate = REPOSITORY_ROOT / relative.with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = REPOSITORY_ROOT / relative / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def ancestor_conftests(path: Path) -> list[Path]:
    """Every conftest.py pytest loads for one test file, innermost last.

    Nested conftest loading is part of the import surface: a conftest in any
    directory between `tests/` and the module is executed for it, so it belongs
    in the closure even when nothing imports it explicitly.
    """

    found: list[Path] = []
    directory = path.parent
    while True:
        conftest = directory / "conftest.py"
        if conftest.is_file():
            found.append(conftest)
        if directory == TESTS_ROOT or directory == REPOSITORY_ROOT:
            break
        if TESTS_ROOT not in directory.parents:
            break
        directory = directory.parent
    return found


def module_label(path: Path) -> str:
    """Repository-relative label, or the absolute path for a synthetic file."""

    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_closure(entry_points: list[Path]) -> dict[Path, str]:
    """Every repository file the given entry points can transitively load."""

    pending = list(entry_points)
    for entry in entry_points:
        pending.extend(ancestor_conftests(entry))
    seen: dict[Path, str] = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        source = path.read_text(encoding="utf-8")
        seen[path] = source
        for dotted in imported_module_names(source, module_label(path)):
            resolved = resolve_test_module(dotted)
            if resolved is not None and resolved not in seen:
                pending.append(resolved)
                pending.extend(ancestor_conftests(resolved))
    return seen
