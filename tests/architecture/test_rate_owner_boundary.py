"""Boundary guard for the single rate owner (``access.rate_profile``).

A service's rate is decided in one place, ``app/services/access_rate_profile.py``.
Before it, about fifteen call sites wrote a plan rate, a manual override or a
throttle into ``access_credentials.radius_profile_id`` and four resolvers read it
back in four different orders. This guard keeps that from coming back, with an
EMPTY baseline: no module is excused.

Writers (assignment, constructor keyword, ``setattr``):

- ``pre_throttle_radius_profile_id``: the collections throttle path and the owner;
- ``radius_profile_id``: the same two, plus the plan owner
  (``Subscription.radius_profile_id``, a compatibility projection of the package
  link) and the ``radius_users`` mirror in ``radius.py``;
- a credential is never *constructed* with a profile.

Readers: nothing outside the owner reads ``pre_throttle_radius_profile_id``
except the collections throttle path, and ``.radius_profile_id`` /
``.radius_profile`` are read elsewhere only as evidence, display or a
compatibility field, each listed below with its reason. Resolving a rate means
asking the owner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

OWNER = "app/services/access_rate_profile.py"
THROTTLE = "app/services/collections/_core.py"
PLAN_OWNER = "app/services/catalog/subscriptions.py"
MIRROR = "app/services/radius.py"

#: Empty by design. Adding an entry means a new split-brain writer or reader.
WRITER_BASELINE: set[str] = set()
READER_BASELINE: set[str] = set()

MARKER = "pre_throttle_radius_profile_id"
SLOT = "radius_profile_id"
GUARDED = (MARKER, SLOT)

#: Where each attribute may be ASSIGNED, and on which base object. A named
#: writer may write only its intended target: the plan owner the subscription
#: field, the mirror the ``radius_users`` row, the throttle path and the owner a
#: credential.
ASSIGN_ALLOWED: dict[str, dict[str, set[str]]] = {
    MARKER: {OWNER: {"credential"}, THROTTLE: {"credential"}},
    SLOT: {
        OWNER: {"credential"},
        THROTTLE: {"credential"},
        PLAN_OWNER: {"subscription"},
        MIRROR: {"existing_user"},
    },
}
#: Constructor keywords: callee name -> files that may pass ``radius_profile_id``.
KEYWORD_ALLOWED: dict[str, set[str]] = {
    "AccessCredential": set(),
    "Subscription": set(),
    "RadiusUser": {MIRROR},
}
#: Core / ORM update calls that take the column as a keyword. Nobody may.
UPDATE_CALLEES = frozenset({"values", "update", "insert"})

#: Raw SQL text that writes either column.
RAW_SQL_WRITE = re.compile(
    r"\b(update|insert|set)\b[\s\S]*\b(pre_throttle_)?radius_profile_id\b",
    re.IGNORECASE,
)

#: Reads of ``.radius_profile_id`` / ``.radius_profile`` / the marker outside the
#: owner, as exact per-file COUNTS that can only shrink. Each is evidence,
#: display or a compatibility field -- none may resolve a rate. A file that reads
#: more fails; a file that reads less must lower its count here.
READ_COUNTS: dict[str, int] = {
    THROTTLE: 9,  # the collections throttle path owns the marker and the slot
    PLAN_OWNER: 5,  # compat projection: previous/current package link
    "app/services/catalog/credentials.py": 3,  # API payload echo for the D1 refusal
    "app/services/web_catalog_offers.py": 3,  # offer profile edit: compat projection
    "app/services/subscription_change_execution.py": 3,  # mirror verification
    "app/services/subscription_correction.py": 3,  # preview evidence
    "app/services/web_catalog_subscriptions.py": 2,  # form echo of the compat field
    MIRROR: 1,  # radius_users mirror compares its own stored value
    "app/services/access_credential_binding.py": 1,  # audit evidence of the rebind
    "app/services/cross_app_drift.py": 1,  # drift evidence for a bad throttle
    "app/services/web_catalog_subscription_workflows.py": 1,  # create outcome echo
    "app/services/web_provisioning_bulk_activate.py": 1,  # compat projection
}


def _python_files():
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel.startswith(("app/models/", "app/schemas/")):
            continue
        yield rel, path


def _base_name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else ast.unparse(node)


def _callee(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def writes(source: str) -> list[tuple[int, str, str, str]]:
    """``(line, kind, attribute-or-callee, base)`` for every guarded write."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    found: list[tuple[int, str, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in GUARDED:
                    found.append(
                        (node.lineno, "assign", target.attr, _base_name(target.value))
                    )
        elif isinstance(node, ast.Call):
            callee = _callee(node)
            for keyword in node.keywords:
                if keyword.arg not in GUARDED:
                    continue
                if callee in KEYWORD_ALLOWED:
                    found.append((node.lineno, "keyword", callee, keyword.arg))
                elif callee in UPDATE_CALLEES:
                    found.append((node.lineno, "update-keyword", callee, keyword.arg))
            if (
                callee == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in GUARDED
            ):
                found.append((node.lineno, "setattr", str(node.args[1].value), ""))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and RAW_SQL_WRITE.search(node.value)
        ):
            found.append((node.lineno, "raw-sql", "radius_profile_id", ""))
    return found


def reads(source: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in (MARKER, SLOT, "radius_profile")
        ):
            found.append((node.lineno, node.attr))
    return found


def _violations(rel: str, source: str) -> list[str]:
    out: list[str] = []
    for line, kind, name, base in writes(source):
        if kind == "assign" and base in ASSIGN_ALLOWED[name].get(rel, set()):
            continue
        if kind == "keyword" and rel in KEYWORD_ALLOWED[name]:
            continue
        out.append(f"{rel}:{line} writes {name} on {base or '?'} ({kind})")
    return out


def test_only_the_named_modules_write_their_intended_targets() -> None:
    violations: list[str] = []
    for rel, path in _python_files():
        if rel in WRITER_BASELINE:
            continue
        violations += _violations(rel, path.read_text(encoding="utf-8"))
    assert not violations, (
        "a plan rate, override or throttle is written outside the named owners, "
        "or a named owner writes a target that is not its own:\n  "
        + "\n  ".join(sorted(violations))
    )


def test_reads_outside_the_owner_only_shrink() -> None:
    counts: dict[str, int] = {}
    for rel, path in _python_files():
        if rel in READER_BASELINE or rel == OWNER:
            continue
        n = len(reads(path.read_text(encoding="utf-8")))
        if n:
            counts[rel] = n
    grew = {
        rel: (n, READ_COUNTS.get(rel, 0))
        for rel, n in counts.items()
        if n > READ_COUNTS.get(rel, 0)
    }
    stale = {
        rel: (counts.get(rel, 0), n)
        for rel, n in READ_COUNTS.items()
        if counts.get(rel, 0) < n
    }
    assert not grew, (
        "rate is resolved outside the single owner "
        "(app/services/access_rate_profile.py); ask the owner instead. "
        f"(actual, allowed): {grew}"
    )
    assert not stale, (
        f"these files read fewer rate fields than recorded; lower READ_COUNTS: {stale}"
    )


def test_no_second_rate_resolver_is_defined() -> None:
    """The retired per-module resolvers must not be re-created."""
    retired = {
        "_effective_profile",
        "_radius_profile_inherits_previous_offer",
        "_radius_profiles_are_speed_equivalent",
        "_radius_profile_matches_offer",
        "stage_subscription_radius_profile",
        "apply_radius_profile_to_subscription",
        "_throttle_account",
        "_restore_throttle",
        "_resolve_offer_radius_profile_id",
    }
    found: list[str] = []
    for rel, path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name in retired:
                    found.append(f"{rel}:{node.lineno} defines {node.name}")
    assert not found, "retired rate writers/resolvers came back:\n  " + "\n  ".join(
        found
    )


def test_baselines_are_empty() -> None:
    assert WRITER_BASELINE == set() and READER_BASELINE == set()


# --- sensitivity proofs: the guard really sees what it claims to -----------


def test_guard_sees_every_write_shape() -> None:
    source = (
        "def f(cred, sub, db):\n"
        "    cred.radius_profile_id = sub.radius_profile_id\n"
        "    cred.pre_throttle_radius_profile_id = None\n"
        "    AccessCredential(radius_profile_id=1)\n"
        "    setattr(cred, 'radius_profile_id', 2)\n"
        "    CorrectSubscriptionOutcome(radius_profile_id=3)\n"
        "    db.execute(update(T).values(radius_profile_id=4))\n"
        "    db.execute(text('UPDATE access_credentials SET radius_profile_id = 5'))\n"
        "    db.execute(text('SELECT radius_profile_id FROM access_credentials'))\n"
    )
    assert [kind for _l, kind, _n, _b in writes(source)] == [
        "assign",
        "assign",
        "keyword",
        "setattr",
        "update-keyword",
        "raw-sql",
    ]
    assert len(_violations("app/services/other.py", source)) == 6


def test_a_named_writer_may_write_only_its_own_target() -> None:
    credential_write = "def f(c):\n    c.radius_profile_id = 1\n"
    subscription_write = (
        "def f(subscription):\n    subscription.radius_profile_id = 1\n"
    )
    mirror_write = "def f(existing_user):\n    existing_user.radius_profile_id = 1\n"
    marker_write = (
        "def f(credential):\n    credential.pre_throttle_radius_profile_id = 1\n"
    )

    # Each owner is excused for its own target ...
    assert not _violations(PLAN_OWNER, subscription_write)
    assert not _violations(MIRROR, mirror_write)
    assert not _violations(THROTTLE, marker_write.replace("credential", "credential"))
    # ... and only that one: the plan owner and the mirror cannot write a credential.
    assert _violations(PLAN_OWNER, credential_write.replace("c.", "credential."))
    assert _violations(MIRROR, subscription_write)
    assert _violations(PLAN_OWNER, mirror_write)
    assert _violations(MIRROR, marker_write)
    # A credential constructor never takes a profile, even in the named files.
    assert _violations(
        THROTTLE, "def f():\n    AccessCredential(radius_profile_id=1)\n"
    )
    assert not _violations(MIRROR, "def f():\n    RadiusUser(radius_profile_id=1)\n")


def test_guard_sees_reads_and_the_baselines_are_a_ratchet() -> None:
    source = (
        "def f(c):\n    return c.radius_profile_id, c.pre_throttle_radius_profile_id\n"
    )
    assert [attr for _line, attr in reads(source)] == [SLOT, MARKER]
    assert all(count > 0 for count in READ_COUNTS.values())
