"""One owner for billing-period boundary geometry, and no day-count shortcuts.

``service_intent.subscription_billing_cadence``
(``app.services.catalog.subscriptions``) owns where a billing period starts and
ends. Until 2026-08-29 ``billing_automation.generate_cancellation_credit``
carried its own copy of the reverse, and the copy had two money defects: no
``quarterly`` branch (a cancelled quarterly subscription was credited against
one month of a three-month period — 2.9x over-issue) and
``datetime.replace(year=...)`` for ``annual``, which raises on 29 February and
left the customer silently uncredited.

These guards are about the NEXT copy, not the last one. Every detector below
carries a positive control, because a source scan that finds nothing passes
whether the code is clean or the detector is blind.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

OWNER = "app/services/catalog/subscriptions.py"

# Every module on the cancellation-credit call path.
CREDIT_PATH_MODULES = (
    OWNER,
    "app/services/billing_automation.py",
    "app/services/account_lifecycle.py",
    "app/services/web_catalog_calculator.py",
)

# Day counts that stand in for a calendar month/quarter/year.
APPROXIMATE_DAYS = frozenset({30, 31, 90, 91, 92, 180, 365, 366})
APPROXIMATE_WEEKS = frozenset({4, 13, 52})
SECONDS_PER_PERIOD = frozenset({2592000, 2678400, 7776000, 31536000, 31622400})


def _parse(relative: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relative).read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — the guard is now pointed at nothing")


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
def _cadence_branches(node: ast.AST) -> list[str]:
    """Comparisons against a BillingCycle member — a local cadence decision."""
    return [
        ast.unparse(cmp)
        for cmp in ast.walk(node)
        if isinstance(cmp, ast.Compare) and "BillingCycle." in ast.unparse(cmp)
    ]


def _day_count_approximations(tree: ast.Module) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "timedelta"
        ):
            for kw in node.keywords:
                if not isinstance(kw.value, ast.Constant):
                    continue
                value = kw.value.value
                if not isinstance(value, int):
                    continue
                if (kw.arg == "days" and value in APPROXIMATE_DAYS) or (
                    kw.arg == "weeks" and value in APPROXIMATE_WEEKS
                ):
                    hits.append((node.lineno, f"timedelta({kw.arg}={value})"))
        if isinstance(node, ast.Constant) and node.value in SECONDS_PER_PERIOD:
            hits.append((node.lineno, f"seconds-per-period literal {node.value}"))
    return hits


def _unclamped_year_replacements(tree: ast.Module) -> list[tuple[int, str]]:
    """``x.replace(year=...)`` without a day clamp — the 29 February crash shape."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
        ):
            kwargs = {kw.arg for kw in node.keywords}
            if "year" in kwargs and "day" not in kwargs:
                hits.append((node.lineno, ast.unparse(node)[:100]))
    return hits


# ---------------------------------------------------------------------------
# Positive controls — the detectors must bite before we trust a clean result.
# ---------------------------------------------------------------------------
def test_the_cadence_branch_detector_bites() -> None:
    planted = ast.parse(
        "def f(cycle):\n"
        "    if cycle == BillingCycle.daily:\n"
        "        return 1\n"
        "    return 2\n"
    )
    assert _cadence_branches(planted) == ["cycle == BillingCycle.daily"]


def test_the_day_count_detector_bites() -> None:
    planted = ast.parse(
        "from datetime import timedelta\n"
        "a = x - timedelta(days=90)\n"
        "b = y - timedelta(days=365)\n"
        "c = z / 2592000\n"
    )
    assert len(_day_count_approximations(planted)) == 3


def test_the_year_replacement_detector_bites() -> None:
    planted = ast.parse("a = d.replace(year=d.year - 1)\n")
    assert len(_unclamped_year_replacements(planted)) == 1
    clamped = ast.parse("a = d.replace(year=y, month=m, day=day)\n")
    assert _unclamped_year_replacements(clamped) == []


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------
def test_the_owner_declares_a_total_table_and_two_public_boundaries() -> None:
    tree = _parse(OWNER)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert {"billing_cycle_start", "billing_cycle_end", "_shift_by_cycle"} <= names
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (
            [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        )
        if isinstance(target, ast.Name)
    }
    assert "_CYCLE_PERIOD_LENGTH" in assigned


def test_the_cancellation_credit_delegates_its_cadence_to_the_owner() -> None:
    tree = _parse("app/services/billing_automation.py")
    fn = _function(tree, "generate_cancellation_credit")

    branches = _cadence_branches(fn)
    assert not branches, (
        "generate_cancellation_credit decides cadence locally again: "
        f"{branches}. Period boundaries belong to "
        "service_intent.subscription_billing_cadence — call "
        "catalog.subscriptions.billing_cycle_start."
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and "billing_cycle_start" in ast.unparse(node.func)
    ]
    assert calls, "generate_cancellation_credit no longer asks the cadence owner"


@pytest.mark.parametrize("module", CREDIT_PATH_MODULES)
def test_no_day_count_approximation_on_the_cancellation_credit_path(
    module: str,
) -> None:
    hits = _day_count_approximations(_parse(module))
    assert not hits, (
        f"{module} approximates a calendar period with a fixed day count: {hits}. "
        "A quarter is 89-92 days and a year is 365 or 366; use the owner's "
        "clamped calendar arithmetic."
    )


@pytest.mark.parametrize("module", CREDIT_PATH_MODULES)
def test_no_unclamped_year_replacement_on_the_cancellation_credit_path(
    module: str,
) -> None:
    hits = _unclamped_year_replacements(_parse(module))
    assert not hits, (
        f"{module} shifts a year without clamping the day: {hits}. "
        "That raises ValueError on 29 February and, inside "
        "cancel_subscription's broad except, silently denies the customer "
        "their credit."
    )


def test_the_registry_still_names_the_boundary_owner() -> None:
    from app.services.sot_registry.registry import service_relationship

    service = service_relationship("service_intent.subscription_billing_cadence")
    assert service.module == "app.services.catalog.subscriptions"
    assert (
        "billing-period boundary geometry (cycle start and cycle end)" in service.owns
    )
