"""Walled-account healing is a registered, scheduled, gated owner command.

Before this it was a helper with no Celery task and no beat entry, reachable
only from `scripts/one_off/unwall_paid_accounts.py`, while the scheduled
detector hard-coded `apply=False`.
"""

from __future__ import annotations

from pathlib import Path

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


def test_application_is_gated_by_a_registered_scheduler_boolean() -> None:
    spec = _source("app/services/settings_spec.py")
    scheduled = _source("app/services/enforcement_scheduled.py")

    assert '(SettingDomain.billing, "walled_account_healing_enabled")' in spec
    assert '(SettingDomain.billing, "walled_account_healing_apply_enabled")' in spec
    assert '"walled_account_healing_apply_enabled"' in scheduled


def test_scheduled_application_requires_proven_zero_overdue_receivable() -> None:
    owner = _source("app/services/billing/unwall_paid_accounts.py")

    assert "def decide_unwall(" in owner
    assert "lock_account(" in owner, "the recomputation must hold an account lock"
    assert "overdue_receivable_snapshot(" in owner
    assert "require_zero_overdue_receivable" in owner
    assert "_stage_unwall_exception(" in owner


def test_healing_introduces_no_money_tolerance() -> None:
    """Exact arithmetic: no epsilon, tolerance, or de-minimis threshold."""
    owner = _source("app/services/billing/unwall_paid_accounts.py")
    lowered = owner.lower()

    for forbidden in ("epsilon", "de_minimis", "de-minimis", "tolerance_amount"):
        assert forbidden not in lowered.replace("no tolerance", ""), (
            f"{forbidden!r} suggests a money tolerance was introduced"
        )
