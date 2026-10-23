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

#: Where each attribute may be ASSIGNED.
ASSIGN_ALLOWED = {
    MARKER: {OWNER, THROTTLE},
    SLOT: {OWNER, THROTTLE, PLAN_OWNER, MIRROR},
}
#: Constructor keywords: callee name -> files that may pass ``radius_profile_id``.
KEYWORD_ALLOWED = {
    "AccessCredential": set(),
    "Subscription": set(),
    "RadiusUser": {MIRROR},
}

#: Reads of ``.radius_profile_id`` / ``.radius_profile`` outside the owner that are
#: not a rate decision. Each needs a reason; none may resolve a rate.
READ_ALLOWED = {
    THROTTLE: "the collections throttle path owns the marker and the slot",
    PLAN_OWNER: "compat projection: previous/current package link on plan change",
    MIRROR: "radius_users mirror compares its own stored value",
    "app/services/access_credential_binding.py": "audit evidence of the rebinding",
    "app/services/subscription_correction.py": "preview evidence",
    "app/services/subscription_change_execution.py": "verification of the mirror",
    "app/services/cross_app_drift.py": "drift evidence for an unresolvable throttle",
    "app/services/web_catalog_offers.py": "offer profile edit: compat projection",
    "app/services/web_catalog_subscriptions.py": "form echo of the compat field",
    "app/services/web_catalog_subscription_workflows.py": "create outcome echo",
    "app/services/web_provisioning_bulk_activate.py": "compat projection on activate",
    "app/services/catalog/credentials.py": "API payload echo for the D1 refusal",
}


def _python_files():
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel.startswith(("app/models/", "app/schemas/")):
            continue
        yield rel, path


def writes(source: str) -> list[tuple[int, str, str]]:
    """``(line, kind, detail)`` for every write of the guarded attributes."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in (MARKER, SLOT):
                    found.append((node.lineno, "assign", target.attr))
        elif isinstance(node, ast.Call):
            callee = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            for keyword in node.keywords:
                if keyword.arg in (MARKER, SLOT) and callee in KEYWORD_ALLOWED:
                    found.append((node.lineno, "keyword", f"{callee}:{keyword.arg}"))
            if (
                callee == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in (MARKER, SLOT)
            ):
                found.append((node.lineno, "setattr", str(node.args[1].value)))
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
    for line, kind, detail in writes(source):
        if kind == "assign" and rel in ASSIGN_ALLOWED[detail]:
            continue
        if kind == "keyword":
            callee, _, _attr = detail.partition(":")
            if rel in KEYWORD_ALLOWED[callee]:
                continue
        out.append(f"{rel}:{line} writes {detail} ({kind})")
    return out


def test_only_the_named_modules_write_the_credential_rate_fields() -> None:
    violations: list[str] = []
    for rel, path in _python_files():
        if rel in WRITER_BASELINE:
            continue
        violations += _violations(rel, path.read_text(encoding="utf-8"))
    assert not violations, (
        "a plan rate, override or throttle is written into a credential outside "
        "the collections throttle path and the rate owner:\n  "
        + "\n  ".join(sorted(violations))
    )


def test_nothing_outside_the_owner_reads_the_marker_or_resolves_a_rate() -> None:
    violations: list[str] = []
    for rel, path in _python_files():
        if rel in READER_BASELINE or rel == OWNER:
            continue
        for line, attr in reads(path.read_text(encoding="utf-8")):
            if attr == MARKER and rel != THROTTLE:
                violations.append(f"{rel}:{line} reads {MARKER}")
            elif attr != MARKER and rel not in READ_ALLOWED:
                violations.append(f"{rel}:{line} reads {attr}")
    assert not violations, (
        "rate is resolved outside the single owner "
        "(app/services/access_rate_profile.py). Ask the owner:\n  "
        + "\n  ".join(sorted(violations))
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


def test_guard_sees_an_assignment_a_keyword_and_a_setattr() -> None:
    source = (
        "def f(cred, sub):\n"
        "    cred.radius_profile_id = sub.radius_profile_id\n"
        "    cred.pre_throttle_radius_profile_id = None\n"
        "    AccessCredential(radius_profile_id=1)\n"
        "    setattr(cred, 'radius_profile_id', 2)\n"
        "    CorrectSubscriptionOutcome(radius_profile_id=3)\n"
    )
    assert [kind for _line, kind, _detail in writes(source)] == [
        "assign",
        "assign",
        "keyword",
        "setattr",
    ]
    assert len(_violations("app/services/other.py", source)) == 4
    # The named owners are excused for the fields they own.
    assert not _violations(
        THROTTLE, "def f(c):\n    c.pre_throttle_radius_profile_id = 1\n"
    )


def test_guard_sees_reads() -> None:
    source = (
        "def f(c):\n    return c.radius_profile_id, c.pre_throttle_radius_profile_id\n"
    )
    assert [attr for _line, attr in reads(source)] == [SLOT, MARKER]
