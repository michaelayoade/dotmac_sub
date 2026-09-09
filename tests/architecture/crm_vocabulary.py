"""Shared analysis for the CRM/Omni vocabulary freeze.

The `dotmac_crm` deployment ("Omni") was decommissioned on 2026-08-29. Its
surface inside Sub is being replaced domain by domain — Inbox/Chat, then
Support/Ticketing, Sales/Quotes, Party/Customer/Reseller, ERP modules, then a
final secrets/deployment/observability pass — each slice deleting residue
BESIDE its replacement owner rather than in a sweep.

A slice-by-slice replacement only works if the surface holds still between
slices. This freeze is what holds it: it fails when a NEW CRM dependency lands,
and it fails when one disappears without the baseline being lowered in the same
change. A silent removal is as damaging as a silent addition here, because the
whole programme is a sequence of deliberate, recorded removals.

## Why the match is on identifier FORM, not on the bare word

`\\b` is the trap. A word boundary does not fire beside `_` or a digit, so a
naive `\\bcrm\\b` reads `crm_subscriber_id`, `crm_ticket_pull` and `CRMClient`
as clean — which is nearly the entire real surface. This module instead splits
camelCase into `_`, splits on every non-alphanumeric run, lowercases, and
compares whole TOKENS against the frozen vocabulary.

Token equality also buys specificity for free: a base64 digest or a long opaque
string is one token and never equals `crm`, so it cannot false-positive the way
a substring search would.

## What counts as a member

A file is in the surface if the vocabulary appears in its CONTENT **or in its
PATH**. The path half is not decorative: `app/services/reseller_crm_views.py`
is named for the CRM and contains no `crm` token in its body, so a
content-only scan misses a file whose whole subject is the CRM.

Tests that assert a CRM/Omni alias is *refused* are members too — for example
the ERP contract tests pinning the retired `omni_id`. They reference the
vocabulary, and deleting one silently is exactly what the falling direction of
this ratchet exists to catch.

## How a literal is CONSUMED decides dependency vs. mention, for code

A file can legitimately need to NAME the vocabulary as DATA rather than as a
dependency — locating an existing baseline/ledger file to read, for example.
That does not execute anything; the string sits inert, used to find a file,
not imported. A REAL dependency looks different in the AST: a static
`import x.crm_y` / `from x.crm_y import z` statement, or a string handed to a
runtime import operation (`importlib.import_module(...)`, `__import__(...)`)
— both cause code to load and run.

`_is_characterization_only` draws exactly that line, for `.py` files: a
vocabulary-bearing line inside a real or dynamic import is ALWAYS a
dependency, full stop, regardless of what it names — importing a
well-established, long-frozen module is still importing it, and this
exemption never overrides that. A vocabulary-bearing line whose string
literal is the SOLE argument to `Path(...)` — locating a data file to read,
not a module to import — is data, not code, and does not by itself make the
file a new dependency. Anything else, DELIBERATELY INCLUDING a string
sitting in a list/tuple/set/dict, keeps counting exactly as before: a
container literal is not, on its own, proof of inert data —
`app/services/infrastructure_health.py` keys a real health-check dispatch
off a `"crm"` string in a plain dict, so a blanket "any container is a
mention" rule would have silently exempted a genuine, live dependency. This
exemption only ever SUBTRACTS membership for the one specifically
recognized, unambiguously-inert data shape (`Path("literal")`), never grants
a general "it's just a mention" allowance for container literals at large.
The deciding question is how the literal is consumed, not whether it looks
like a module or file path — the same name can be a dependency in one file
and a mention in another (see the paired plants in
`test_crm_vocabulary_freeze.py`:
`test_a_real_import_of_a_crm_module_still_counts_as_a_dependency` and
`test_the_identical_name_as_a_path_argument_does_not_count`).
"""

from __future__ import annotations

import ast
import re
import subprocess
from functools import cache
from pathlib import Path

#: The frozen vocabulary. `omni` is here because the deployment was named Omni
#: and its identifiers outlived the CRM name in several contracts.
CRM_TERMS: frozenset[str] = frozenset({"crm", "omni"})

#: Entry-point families. Application code with its Celery tasks and workers
#: (`app`), schema migrations (`alembic`), operator CLI, seeds and one-offs
#: (`scripts`), the test suite, and the documentation that carries the
#: programme's contracts.
LANES: tuple[str, ...] = ("app", "alembic", "docs", "scripts", "tests")

# Split `fooBar` and `CRMClient` alike: the second alternative is what turns
# `CRMClient` into `CRM_Client` rather than leaving one opaque token.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    """Lowercase identifier tokens, with camelCase treated as a separator."""

    split = _CAMEL_BOUNDARY.sub("_", text)
    return frozenset(part.lower() for part in _NON_ALNUM.split(split) if part)


def mentions_crm(text: str) -> bool:
    """Whether a fragment carries the frozen vocabulary as a whole token."""

    return bool(tokens(text) & CRM_TERMS)


@cache
def tracked_files() -> tuple[str, ...]:
    """Every tracked path, from git.

    Tracked rather than on-disk on purpose: a freeze is a statement about the
    repository, and an untracked scratch file in `docs/` must not be able to
    trip it.
    """

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(sorted(path for path in result.stdout.split("\0") if path))


def _reads_as_text(path: Path) -> str | None:
    """Decode a tracked file, or None if it is binary or unreadable.

    The binary check is load-bearing. `docs/My Map.kmz` is a zip archive whose
    bytes, forced through a lossy UTF-8 decode, happen to yield a `crm` token
    and put a map file in the CRM surface. A scanner that decodes binary does
    not find things; it invents them.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="ignore")


_DYNAMIC_IMPORT_CALLEES: frozenset[str] = frozenset({"import_module", "__import__"})


def _dependency_lines(tree: ast.Module) -> frozenset[int]:
    """Lines where a vocabulary-bearing literal is genuinely CONSUMED as
    code: a real `import`/`from ... import` statement (always, regardless
    of what module it names — this is what makes the exemption below
    non-overridable by a real import), or a string literal passed to a
    runtime import operation (`importlib.import_module(...)`,
    `__import__(...)`)."""

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            end = getattr(node, "end_lineno", None) or node.lineno
            lines.update(range(node.lineno, end + 1))
        elif isinstance(node, ast.Import) and node.names:
            end = getattr(node, "end_lineno", None) or node.lineno
            lines.update(range(node.lineno, end + 1))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _DYNAMIC_IMPORT_CALLEES:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        lines.add(arg.lineno)
    return frozenset(lines)


def _characterization_data_lines(tree: ast.Module) -> frozenset[int]:
    """Lines where a vocabulary-bearing string literal is CONSUMED as the
    sole argument to `Path(...)` — locating a data file to read, not a
    module to import, not application logic acting on the name.

    Deliberately narrower than "any list/tuple/set/dict literal": a
    registry list or a name->value dict can be exactly as live a dependency
    as an import — `app/services/infrastructure_health.py` keys a real
    health-check dispatch off a `"crm"` string in a plain dict, which is
    unambiguously still a dependency and must keep counting. A bare
    `Path("literal/path.txt")` construction has no such reading: it can
    only ever locate a file for ordinary I/O, which is what makes it safe
    to treat as characterization input rather than a general "container
    literal" exemption.
    """

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and len(node.args) == 1
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            lines.add(arg.lineno)
    return frozenset(lines)


def _is_characterization_only(path: Path, text: str) -> bool:
    """Whether every vocabulary-bearing line in a `.py` file is DATA
    consumption, never a real or dynamic import.

    True means this file adds no new coupling to the frozen surface: it
    only names the vocabulary inside a recognized data shape (see
    `_characterization_data_lines`). False — the default, unconditional
    posture — means at least one occurrence is a real dependency
    (`_dependency_lines`) or an unrecognized shape (a bare identifier, an
    f-string, prose, a plain assignment), and the file is a genuine surface
    member exactly as before this exemption existed. A dependency line
    always wins over a data-shaped line on the same file: this exemption
    only ever subtracts membership for a specifically recognized data
    shape, never adds a blanket allowance. Only `.py` files can qualify —
    a `.md`/`.txt`/`.json` file has no AST to classify.
    """

    if path.suffix != ".py":
        return False
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return False
    dependency_lines = _dependency_lines(tree)
    data_lines = _characterization_data_lines(tree)
    saw_any = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not mentions_crm(line):
            continue
        saw_any = True
        if lineno in dependency_lines:
            return False
        if lineno not in data_lines:
            return False
    return saw_any


@cache
def surface_by_lane() -> dict[str, frozenset[str]]:
    """Files carrying the CRM/Omni vocabulary, grouped by entry-point family."""

    found: dict[str, set[str]] = {lane: set() for lane in LANES}
    for tracked in tracked_files():
        lane, _, _ = tracked.partition("/")
        if lane not in found:
            continue
        if mentions_crm(tracked):
            found[lane].add(tracked)
            continue
        text = _reads_as_text(Path(tracked))
        if text is None or not any(mentions_crm(line) for line in text.splitlines()):
            continue
        if _is_characterization_only(Path(tracked), text):
            continue
        found[lane].add(tracked)
    return {lane: frozenset(paths) for lane, paths in found.items()}


def surface_paths() -> frozenset[str]:
    """The whole frozen surface as one set."""

    return frozenset().union(*surface_by_lane().values())
