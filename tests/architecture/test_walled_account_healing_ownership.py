"""Walled-account healing is a registered, scheduled, gated owner command.

Before this it was a helper with no Celery task and no beat entry, reachable
only from `scripts/one_off/unwall_paid_accounts.py`, while the scheduled
detector hard-coded `apply=False`.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.settings_spec import SCHEDULER_BOOLEAN_SETTING_KEYS, get_spec
from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE = PROJECT_ROOT / "tests" / "architecture" / "sot_writer_baseline.txt"
OWNER_MODULE = "app.services.billing.unwall_paid_accounts"
OWNER_NAME = "financial.walled_account_healing"
TASK_NAME = "app.tasks.enforcement.heal_walled_paid_accounts"


def _source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_the_healing_owner_is_declared_in_the_registry() -> None:
    services = {service.name: service for service in all_services()}
    owner = services.get(OWNER_NAME)
    assert owner is not None, "walled-account healing has no declared owner"
    assert owner.module == OWNER_MODULE


def test_the_healing_owner_left_the_undeclared_writer_baseline() -> None:
    """The baseline is a shrink-only ratchet; a resolved owner must leave it."""
    entries = {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert OWNER_MODULE not in entries


def test_healing_has_a_registered_task_and_beat_entry() -> None:
    tasks = _source("app/tasks/enforcement.py")
    scheduled = _source("app/services/enforcement_scheduled.py")
    beat = _source("app/services/scheduler_config.py")

    assert f'name="{TASK_NAME}"' in tasks
    assert "def heal_walled_paid_accounts(" in tasks
    assert "def heal_walled_paid_accounts(" in scheduled
    assert f'task_name="{TASK_NAME}"' in beat
    assert 'name="walled_account_healing"' in beat


def test_the_task_gate_is_a_registered_scheduler_boolean() -> None:
    """The beat task's on/off switch belongs to the scheduler-boolean registry."""
    assert (
        SettingDomain.billing,
        "walled_account_healing_enabled",
    ) in SCHEDULER_BOOLEAN_SETTING_KEYS


def test_application_is_gated_and_defaults_off() -> None:
    """The apply gate is a registered boolean that must default to False.

    It is deliberately NOT in ``SCHEDULER_BOOLEAN_SETTING_KEYS``: that set is
    required to equal exactly the ``_scheduler_setting_enabled`` call sites in
    ``scheduler_config.py``, and this gate is a per-run decision input resolved
    in ``enforcement_scheduled.py`` — not a beat-task switch. It still goes
    through the registered ``resolve_boolean`` path, never an ad-hoc
    environment/default fallback.
    """
    spec = get_spec(SettingDomain.billing, "walled_account_healing_apply_enabled")
    assert spec is not None, "the apply gate is not a registered setting"
    assert spec.value_type is SettingValueType.boolean
    assert spec.default is False, "scheduled application must default to off"
    assert (
        SettingDomain.billing,
        "walled_account_healing_apply_enabled",
    ) not in SCHEDULER_BOOLEAN_SETTING_KEYS

    scheduled = _source("app/services/enforcement_scheduled.py")
    assert '"walled_account_healing_apply_enabled"' in scheduled
    assert "resolve_boolean(" in scheduled
    assert "apply=apply" in scheduled


def test_scheduled_application_requires_proven_zero_overdue_receivable() -> None:
    owner = _source("app/services/billing/unwall_paid_accounts.py")

    assert "def decide_unwall(" in owner
    assert "lock_account(" in owner, "the recomputation must hold an account lock"
    assert "overdue_receivable_snapshot(" in owner
    assert "require_zero_overdue_receivable" in owner
    assert "_stage_unwall_exception(" in owner


_TOLERANCE_TOKENS = ("epsilon", "tolerance", "de_minimis", "deminimis", "fudge")


def _identifiers(tree: ast.Module) -> set[str]:
    """Every name this module actually binds or reads, excluding prose."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.arg):
            names.add(node.arg.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg.lower())
    return names


def _decimal_literals(tree: ast.Module) -> set[str]:
    """String literals handed straight to ``Decimal(...)`` — money constants."""
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "Decimal" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literals.add(first.value)
    return literals


def test_healing_introduces_no_money_tolerance() -> None:
    """Exact arithmetic: no epsilon, tolerance, or de-minimis threshold.

    Checked against the module's real identifiers and money constants rather
    than a substring sweep of the source, so a comment or docstring *stating*
    that no tolerance exists cannot trip the guard — and, more importantly,
    rewording prose cannot smuggle one past it either.
    """
    tree = ast.parse(
        (PROJECT_ROOT / "app/services/billing/unwall_paid_accounts.py").read_text(
            encoding="utf-8"
        )
    )

    offending = sorted(
        name
        for name in _identifiers(tree)
        for token in _TOLERANCE_TOKENS
        if token in name
    )
    assert not offending, (
        "identifiers suggest a money tolerance was introduced: " + ", ".join(offending)
    )

    non_zero = sorted(
        value for value in _decimal_literals(tree) if Decimal(value) != Decimal("0")
    )
    assert not non_zero, (
        "a non-zero Decimal constant in the healing owner would act as a "
        "de-minimis threshold: " + ", ".join(non_zero)
    )
