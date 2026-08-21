"""Static writer census for the cohort-isp-01 source surface.

`app/migration_source/cohort.py` declares which Sub tables hold the cohort's
source facts. This module answers the only question a retirement ratchet
needs: **which files mutate them, and from which entry-point family**.

## Families, not directories

A guard scoped to one directory states an unenforceable premise — that writers
only ever appear there. They do not: this repository writes cohort tables from
services, HTTP routes, Jinja/HTMX web routes, Celery tasks, event handlers,
importers, one-off scripts and migrations. So the census enumerates
*entry-point families* and asserts that every executable root maps to one.
`unmapped_code_roots` is the load-bearing half: a new `app/<something>/`
package that nobody classified fails the guard rather than silently escaping
it, which is how an omitted family is caught before a writer hides in it.

## What counts as a write

Four independent shapes, because no single one of them is complete:

1. **ORM construction** — `Subscriber(...)`, `Party(...)`.
2. **Tracked-entity mutation** — an attribute assignment on a local name that
   this module proved holds a cohort entity (bound from a construction, a
   `db.get(Model, ...)`, a `query(Model)` terminal, or an annotated
   parameter). The binding tracker is what keeps `x.status = ...` from being
   counted across the whole repository.
3. **Set-based DML** — `update(Model)`, `delete(Model)`,
   `query(Model).update(...)`, `Model.__table__.insert()`.
4. **Raw SQL** — an `INSERT INTO`/`UPDATE`/`DELETE FROM` naming a cohort table
   in any string literal, which is how migrations and repair scripts write.

Shape 2 is a heuristic and is documented as one. It is deliberately tuned to
under-report an unrelated file rather than over-report: an uncounted writer is
found by the next census, whereas a census nobody believes gets suppressed.

## What this module is not

It is not a boundary. It records who writes today and freezes that set; it
retires nothing. Baseline entries are lowered in the cutover pull request that
actually removes a writer, never in advance.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(PROJECT_ROOT))

from app.migration_source.cohort import (  # noqa: E402
    cohort_model_names,
    cohort_table_names,
)


class EntryPointFamily(StrEnum):
    """The executable families a cohort write can arrive from."""

    API_ROUTE = "api_route"
    #: An inbound HTTP handler for another system's callback. Positionally an
    #: API route, split out because "does anything a foreign system can
    #: trigger write our cohort?" is a question worth answering on its own —
    #: and because the brief for this census names webhook handlers as a
    #: family in their own right, so a coverage claim that quietly folded
    #: them into `api_route` would be unverifiable.
    WEBHOOK_HANDLER = "webhook_handler"
    WEB_ROUTE = "web_route"
    #: `app/services/web_*.py`. Positionally a service module, but the
    #: repository's own convention (see `scripts/architecture/sot_debt.py`)
    #: treats an undeclared one as a web presenter — an adapter reached only
    #: from a route. Split out so "an adapter started writing" is legible in
    #: the census instead of hiding inside the service total.
    WEB_PRESENTER = "web_presenter"
    SERVICE = "service"
    TASK_WORKER = "task_worker"
    SCHEDULED_JOB = "scheduled_job"
    EVENT_HANDLER = "event_handler"
    WEBSOCKET = "websocket"
    IMPORTER = "importer"
    POLLER = "poller"
    CLI_SCRIPT = "cli_script"
    MIGRATION = "migration"
    #: An `app/` module belonging to no more specific family — models,
    #: schemas, validators, the session authority. They are counted because a
    #: model-layer default or an `app/db.py` helper can write just as
    #: effectively as a route can.
    APP_MODULE = "app_module"
    #: A loose Python file at the repository root, outside every package.
    REPOSITORY_ROOT = "repository_root"


#: Repository-relative prefixes, longest match wins. Ordered most specific
#: first so `app/services/events/handlers` is an event handler rather than a
#: service, and `app/api/webhooks` stays an API route (a webhook handler is an
#: authenticated HTTP route here, not a separate runtime).
#: Matched before the prefix table: a webhook handler lives under `app/api/`
#: and would otherwise be classified by position alone.
_WEBHOOK_RE: Final[re.Pattern[str]] = re.compile(
    r"^app/api/.*(webhook|callback).*\.py$"
)

_FAMILY_PREFIXES: Final[tuple[tuple[str, EntryPointFamily], ...]] = (
    ("app/services/events/handlers/", EntryPointFamily.EVENT_HANDLER),
    ("app/services/web_", EntryPointFamily.WEB_PRESENTER),
    ("app/services/", EntryPointFamily.SERVICE),
    ("app/api/", EntryPointFamily.API_ROUTE),
    ("app/web/", EntryPointFamily.WEB_ROUTE),
    ("app/web_", EntryPointFamily.WEB_ROUTE),
    ("app/tasks/", EntryPointFamily.TASK_WORKER),
    ("app/celery_scheduler.py", EntryPointFamily.SCHEDULED_JOB),
    ("app/celery_app.py", EntryPointFamily.TASK_WORKER),
    ("app/websocket/", EntryPointFamily.WEBSOCKET),
    ("app/imports/", EntryPointFamily.IMPORTER),
    ("app/poller/", EntryPointFamily.POLLER),
    ("app/syslog/", EntryPointFamily.POLLER),
    ("scripts/", EntryPointFamily.CLI_SCRIPT),
    ("alembic/", EntryPointFamily.MIGRATION),
    ("app/", EntryPointFamily.APP_MODULE),
)

#: Trees scanned for writers, plus every loose Python file at the repository
#: root. `tests/` is excluded on purpose: a fixture that builds a Party is not
#: a production writer, and counting fixtures would move the ratchet for
#: reasons unrelated to the writer surface.
SCAN_ROOTS: Final[tuple[str, ...]] = ("app", "scripts", "alembic")

#: Subtrees inside a scan root that are not part of the executable surface.
#: `versions_archive` holds superseded migrations that Alembic's configured
#: `script_location` never loads; counting them would freeze history rather
#: than the current writer surface.
_SKIPPED_SUBTREES: Final[frozenset[str]] = frozenset({"versions_archive"})

#: Python-bearing repository roots that are deliberately outside the census,
#: each with the reason it is not a writer surface. Stated as data rather than
#: prose so `unscanned_python_roots` can prove the list still covers reality:
#: a new root appears as a failure, not as silence.
EXCLUDED_PYTHON_ROOTS: Final[dict[str, str]] = {
    "tests": "test code; a fixture that builds a Party is not a production writer",
    "stubs": "typeshed stubs; declarations with no runtime behaviour",
    "mobile": "a static-asset dev server, no database session",
    "examples": "a sample connector shipped for documentation",
}

#: Receivers whose `.get(...)`/`.query(...)` mean "database", used to bind a
#: local name to a cohort entity. Matches the token convention already used by
#: `scripts/architecture/sot_debt.py` so two guards read sessions alike.
_SESSION_TOKENS: Final[frozenset[str]] = frozenset(
    {"db", "session", "uow", "conn", "connection", "query"}
)

_QUERY_TERMINALS: Final[frozenset[str]] = frozenset(
    {"first", "one", "one_or_none", "scalar", "scalar_one", "scalar_one_or_none"}
)

_SET_DML_FUNCTIONS: Final[frozenset[str]] = frozenset({"update", "delete", "insert"})

_SQL_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(select|insert|update|delete|from|into|join|truncate)\b", re.IGNORECASE
)

_RAW_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+(?:only\s+)?[\"'`]?(\w+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class CohortWriteSite:
    """One counted mutation of the cohort surface."""

    family: str
    path: str
    count: int
    #: Which cohort models this file was seen writing. Reporting only — the
    #: baseline stores the count, because attributing every mutation of a
    #: tracked local to an exact model would claim a precision the binding
    #: tracker does not have.
    models: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """`family|path`, the stable identity a baseline line names."""

        return f"{self.family}|{self.path}"


def family_for(relative_path: str) -> EntryPointFamily:
    """Classify a repository-relative path into an entry-point family."""

    if _WEBHOOK_RE.match(relative_path):
        return EntryPointFamily.WEBHOOK_HANDLER
    for prefix, family in _FAMILY_PREFIXES:
        if relative_path.startswith(prefix):
            return family
    return EntryPointFamily.REPOSITORY_ROOT


def unscanned_python_roots(*, project_root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """Return Python-bearing repository roots the census neither scans nor excuses.

    The sensitivity half of this guard, and the reason it enumerates families
    rather than trusting one directory. A family list is only as honest as its
    coverage, and coverage lapses silently the moment a new top-level package
    appears — a `workers/` tree, a second `services/` root, a vendored
    integration. Rather than assert the list is complete, this recomputes it:
    every root containing Python must be scanned or must appear in
    `EXCLUDED_PYTHON_ROOTS` with a stated reason.

    A root that is merely absent from both is not "probably fine". It is
    unmonitored, which is exactly the state ADR-0018 refuses to let a guard
    call an exemption.
    """

    unscanned: list[str] = []
    for child in sorted(project_root.iterdir()):
        name = child.name
        if name.startswith(".") or name in EXCLUDED_PYTHON_ROOTS:
            continue
        if child.is_file():
            continue
        if any(name == root.split("/", 1)[0] for root in SCAN_ROOTS):
            continue
        if name in {"node_modules", "__pycache__"}:
            continue
        try:
            has_python = next(child.rglob("*.py"), None) is not None
        except OSError:  # pragma: no cover - unreadable tree
            continue
        if has_python:
            unscanned.append(f"{name}/")
    return tuple(unscanned)


def _scan_paths(project_root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for root in SCAN_ROOTS:
        base = project_root / root
        if not base.exists():
            continue
        paths.extend(
            path
            for path in base.rglob("*.py")
            if path.is_file()
            and "__pycache__" not in path.parts
            and not _SKIPPED_SUBTREES & set(path.parts)
        )
    paths.extend(
        path
        for path in project_root.glob("*.py")
        if path.is_file() and not path.name.startswith("test_")
    )
    return tuple(sorted(paths))


def _receiver_tokens(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return set(node.id.lower().split("_"))
    if isinstance(node, ast.Attribute):
        return _receiver_tokens(node.value) | set(node.attr.lower().split("_"))
    if isinstance(node, ast.Call):
        return _receiver_tokens(node.func)
    if isinstance(node, ast.Subscript):
        return _receiver_tokens(node.value)
    return set()


def _named_model(node: ast.expr | None, models: frozenset[str]) -> str | None:
    """Return the cohort model a syntactic reference names, if any."""

    if isinstance(node, ast.Name) and node.id in models:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in models:
        return node.attr
    if isinstance(node, ast.Subscript):
        return _named_model(node.value, models)
    if isinstance(node, ast.BinOp):  # `Party | None` annotations
        return _named_model(node.left, models) or _named_model(node.right, models)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in models else None
    return None


def _bound_cohort_model(value: ast.expr, models: frozenset[str]) -> str | None:
    """Return the cohort model an expression yields an instance of, if any.

    Recognises construction, `db.get(Model, ...)`, and a `query(Model)` chain
    ending in a scalar terminal. Anything else returns `None`, which keeps an
    untracked local out of the mutation count — the deliberate under-report
    described in this module's docstring.
    """

    if isinstance(value, ast.Call):
        func = value.func
        constructed = _named_model(func, models)
        if constructed is not None:
            return constructed
        if isinstance(func, ast.Attribute):
            if func.attr == "get" and bool(
                _receiver_tokens(func.value) & _SESSION_TOKENS
            ):
                for argument in value.args:
                    named = _named_model(argument, models)
                    if named is not None:
                        return named
                return None
            if func.attr in _QUERY_TERMINALS:
                return _query_chain_model(func.value, models)
    if isinstance(value, ast.Await):
        return _bound_cohort_model(value.value, models)
    return None


def _query_chain_model(node: ast.expr, models: frozenset[str]) -> str | None:
    """Walk back through a `.filter().order_by()` chain to its `query(Model)`."""

    current: ast.expr | None = node
    while isinstance(current, ast.Call):
        func = current.func
        if isinstance(func, ast.Attribute) and func.attr in {"query", "execute"}:
            for arg in current.args:
                named = _named_model(arg, models)
                if named is not None:
                    return named
        for arg in current.args:
            named = _named_model(arg, models)
            if named is not None:
                return named
        current = func.value if isinstance(func, ast.Attribute) else None
    return None


class _ModuleWriteCounter(ast.NodeVisitor):
    """Count cohort writes in one module, tracking entity-bound locals."""

    def __init__(self, models: frozenset[str]) -> None:
        self.models = models
        self.count = 0
        self.seen: set[str] = set()
        self._bound: dict[str, str] = {}

    # -- scope handling -------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer = self._bound
        self._bound = dict(outer)
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            annotated = _named_model(argument.annotation, self.models)
            if annotated is not None:
                self._bound[argument.arg] = annotated
        for child in node.body:
            self.visit(child)
        self._bound = outer

    # -- bindings and writes --------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        bound = _bound_cohort_model(node.value, self.models)
        if bound is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bound[target.id] = bound
        self._count_targets(node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        bound = _named_model(node.annotation, self.models) or (
            _bound_cohort_model(node.value, self.models)
            if node.value is not None
            else None
        )
        if isinstance(node.target, ast.Name) and bound is not None:
            self._bound[node.target.id] = bound
        self._count_targets([node.target])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._count_targets([node.target])
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        iterated = _query_chain_model(node.iter, self.models)
        if isinstance(node.target, ast.Name) and iterated is not None:
            self._bound[node.target.id] = iterated
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        constructed = _named_model(node.func, self.models)
        if constructed is not None:
            self._record(constructed)
        elif isinstance(node.func, ast.Name) and node.func.id in _SET_DML_FUNCTIONS:
            for argument in node.args:
                named = _named_model(argument, self.models)
                if named is not None:
                    self._record(named)
                    break
        elif isinstance(node.func, ast.Attribute) and node.func.attr in (
            _SET_DML_FUNCTIONS
        ):
            named = _named_model(node.func.value, self.models) or _query_chain_model(
                node.func.value, self.models
            )
            if named is not None:
                self._record(named)
        self.generic_visit(node)

    def _count_targets(self, targets: list[ast.expr]) -> None:
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in self._bound
                and not target.attr.startswith("_")
            ):
                self._record(self._bound[target.value.id])

    def _record(self, model: str) -> None:
        self.count += 1
        self.seen.add(model)


def _raw_sql_write_tables(tree: ast.AST, tables: frozenset[str]) -> tuple[str, ...]:
    """Return one entry per string literal issuing DML against a cohort table."""

    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for _, table in _RAW_SQL_RE.findall(node.value):
            if table.lower() in tables:
                hits.append(table.lower())
    return tuple(hits)


@cache
def cohort_write_sites(
    *, project_root: Path = PROJECT_ROOT
) -> tuple[CohortWriteSite, ...]:
    """Return every counted cohort-surface write, ordered and deduplicated."""

    models = cohort_model_names()
    tables = frozenset(name.lower() for name in cohort_table_names())
    sites: list[CohortWriteSite] = []
    for path in _scan_paths(project_root):
        relative = path.relative_to(project_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - lint gate
            continue
        counter = _ModuleWriteCounter(models)
        counter.visit(tree)
        raw_tables = _raw_sql_write_tables(tree, tables)
        total = counter.count + len(raw_tables)
        if total:
            sites.append(
                CohortWriteSite(
                    family=str(family_for(relative)),
                    path=relative,
                    count=total,
                    models=tuple(sorted(counter.seen)),
                )
            )
    return tuple(sorted(sites))


def cohort_write_counts(*, project_root: Path = PROJECT_ROOT) -> dict[str, int]:
    """Return `{family|path: count}`, the shape the baseline stores."""

    return {
        site.key: site.count for site in cohort_write_sites(project_root=project_root)
    }


def _names_a_cohort_table(value: str, tables: frozenset[str]) -> bool:
    """Whether a string literal names a cohort table.

    Two shapes, and the second one needs the SQL guard. An exact match is the
    common case — a table name passed to a helper. A *substring* match is how
    raw statements name their table, and matching a bare word anywhere would
    sweep in every docstring containing "parties" or "addresses"; requiring a
    SQL keyword in the same literal keeps the census measuring code rather
    than prose.
    """

    lowered = value.lower()
    if lowered in tables:
        return True
    if not _SQL_KEYWORD_RE.search(lowered):
        return False
    return any(re.search(rf"\b{re.escape(table)}\b", lowered) for table in tables)


@cache
def cohort_reference_sites(
    *, project_root: Path = PROJECT_ROOT
) -> tuple[tuple[str, str], ...]:
    """Return `(family, path)` for every file that so much as names a cohort table.

    The reader half of the inventory. Writers are the set a cutover has to
    displace; references are the set a cutover has to *not surprise* — reports,
    projections, exporters, list screens, importers. It is deliberately a
    coarse measure (a model name or a table name anywhere in the module) and is
    reported as a bounded reach rather than a precise dependency graph, because
    overstating precision here would invite someone to treat it as a complete
    impact analysis.
    """

    models = cohort_model_names()
    tables = frozenset(name.lower() for name in cohort_table_names())
    found: list[tuple[str, str]] = []
    for path in _scan_paths(project_root):
        relative = path.relative_to(project_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - lint gate
            continue
        for node in ast.walk(tree):
            named = (
                (isinstance(node, ast.Name) and node.id in models)
                or (isinstance(node, ast.Attribute) and node.attr in models)
                or (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _names_a_cohort_table(node.value, tables)
                )
            )
            if named:
                found.append((str(family_for(relative)), relative))
                break
    return tuple(sorted(found))


def reference_counts_by_family(*, project_root: Path = PROJECT_ROOT) -> Counter[str]:
    """Per-family counts of files referencing cohort state at all."""

    totals: Counter[str] = Counter()
    for family, _ in cohort_reference_sites(project_root=project_root):
        totals[family] += 1
    return totals


def counts_by_family(*, project_root: Path = PROJECT_ROOT) -> Counter[str]:
    """Return per-family write totals, for the readiness report."""

    totals: Counter[str] = Counter()
    for site in cohort_write_sites(project_root=project_root):
        totals[site.family] += site.count
    return totals


def render_baseline(*, project_root: Path = PROJECT_ROOT) -> str:
    """Render the ratchet baseline file body."""

    lines = [
        "# cohort-isp-01 source-surface writer baseline.",
        "#",
        "# Format: `<count> <family>|<path>`. Generated by",
        "# `python -m scripts.architecture.isp_cohort_writers --baseline`.",
        "#",
        "# This is a two-directional ratchet. A new or grown writer fails the",
        "# guard; so does a removed writer whose line was not lowered in the",
        "# same change. Lower a line only in the pull request that actually",
        "# retires the writer, after the cohort's sealed authority switch.",
        "",
    ]
    for site in cohort_write_sites(project_root=project_root):
        lines.append(f"{site.count} {site.key}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the census as JSON, or regenerate the baseline body."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="print the ratchet baseline body instead of JSON",
    )
    arguments = parser.parse_args(argv)
    if arguments.baseline:
        print(render_baseline(), end="")
        return 0
    print(
        json.dumps(
            {
                "unscanned_python_roots": list(unscanned_python_roots()),
                "by_family": dict(sorted(counts_by_family().items())),
                "references_by_family": dict(
                    sorted(reference_counts_by_family().items())
                ),
                "sites": [
                    {
                        "family": site.family,
                        "path": site.path,
                        "count": site.count,
                        "models": list(site.models),
                    }
                    for site in cohort_write_sites()
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
