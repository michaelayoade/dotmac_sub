"""Two-directional ratchets for the Sub authorities Collections will retire.

These tests do not claim a cutover.  They make the current duplicate authority
an explicit, shrink-only inventory while the shared module shadows it.  A debt
site cannot grow, and a removed site must lower the baseline in the same change
so the improvement cannot be spent twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.architecture.collections_retirement_guards import (
    scan_collections_retirement_debt,
)

BASELINE = Path(__file__).with_name("collections_retirement_baseline.json")


def _read_baseline(path: Path = BASELINE) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    counts = payload["counts"]
    assert isinstance(counts, dict)
    assert all(isinstance(name, str) and name for name in counts)
    assert all(isinstance(count, int) and count > 0 for count in counts.values())
    return dict(sorted(counts.items()))


def _ratchet_drift(
    current: dict[str, int], baseline: dict[str, int]
) -> tuple[list[str], list[str], list[str]]:
    added = sorted(
        f"{name}: 0 -> {current[name]}" for name in current.keys() - baseline
    )
    grew = sorted(
        f"{name}: {baseline[name]} -> {current[name]}"
        for name in current.keys() & baseline
        if current[name] > baseline[name]
    )
    shrunk = sorted(
        f"{name}: {baseline[name]} -> {current.get(name, 0)}"
        for name in baseline
        if current.get(name, 0) < baseline[name]
    )
    return added, grew, shrunk


def test_collections_retirement_debt_matches_the_two_directional_baseline() -> None:
    current = scan_collections_retirement_debt()
    baseline = _read_baseline()
    added, grew, shrunk = _ratchet_drift(current, baseline)

    assert not added, (
        "new duplicate Collections authority appeared outside the retirement "
        "baseline; route it through the named owner instead:\n  " + "\n  ".join(added)
    )
    assert not grew, (
        "a displaced Collections authority gained new sites; do not expand "
        "migration debt:\n  " + "\n  ".join(grew)
    )
    assert not shrunk, (
        "Collections retirement debt decreased; lower or remove the matching "
        "baseline entry in this same change so the ratchet remains tight:\n  "
        + "\n  ".join(shrunk)
    )


def test_collections_retirement_baseline_is_canonical() -> None:
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    counts = payload["counts"]
    assert list(counts) == sorted(counts)
    assert len(counts) == len(set(counts))


def test_the_scanner_detects_each_retirement_family(tmp_path: Path) -> None:
    """Sensitivity proof: plant one real syntax site from every detector."""

    collections = tmp_path / "app/services/collections"
    collections.mkdir(parents=True)
    (collections / "probe.py").write_text(
        """
from datetime import datetime

class DunningCase:
    __tablename__ = "dunning_cases"

def run_prepaid_balance_sweep():
    pass

def _throttle_account():
    pass

def probe(db, account, credential, subscription, invoice):
    datetime.now()
    credential.radius_profile_id = object()
    account.prepaid_low_balance_at = object()
    subscription.status = object()
    apply_prepaid_overlap_hold(db, invoice)
    Invoices.mark_overdue_system(db, invoice.id)
    suspend_subscription(db, subscription.id)
    queue_customer_notification(db, object())
    notify(subject="Debt notice")
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "app/services/probe_consumer.py").write_text(
        """
from app.services import collections as collections_service
from app.services.collections._core import get_available_balance

get_available_balance(None, "subject:1")
collections_service.has_overdue_balance(None, "subject:1")
collections_service.restore_account_services(None, "subject:1")
""".lstrip(),
        encoding="utf-8",
    )
    scheduler = tmp_path / "app/services/scheduler_config.py"
    scheduler.write_text(
        """
def configure():
    _sync_scheduled_task(
        name="dunning_runner",
        task_name="app.tasks.collections.run_billing_enforcement",
    )
""".lstrip(),
        encoding="utf-8",
    )

    found = scan_collections_retirement_debt(tmp_path)

    assert found == {
        "ambient_clock_call:datetime.datetime.now": 1,
        "class:DunningCase": 1,
        "credential_write:radius_profile_id": 1,
        "direct_access_owner_call:suspend_subscription": 1,
        "finance_write_call:apply_prepaid_overlap_hold": 1,
        "finance_write_call:mark_overdue_system": 1,
        "function:_throttle_account": 1,
        "function:run_prepaid_balance_sweep": 1,
        "notice_subject_literal:Debt notice": 1,
        "notice_delivery_call:queue_customer_notification": 1,
        "prepaid_timer_write:prepaid_low_balance_at": 1,
        "private_collections_import:app/services/probe_consumer.py": 1,
        "product_state_write:subscription.status": 1,
        "legacy_access_call:restore_account_services": 1,
        "receivable_answer_call:get_available_balance": 1,
        "receivable_answer_call:has_overdue_balance": 1,
        "receivable_answer_import:get_available_balance": 1,
        "schedule:dunning_runner": 1,
        "table:dunning_cases": 1,
    }


def test_the_comparator_fails_in_both_directions() -> None:
    """Sensitivity proof for new, grown and silently retired debt."""

    baseline = {"class:DunningCase": 1, "schedule:dunning_runner": 1}

    added, grew, shrunk = _ratchet_drift(
        {
            "class:DunningCase": 1,
            "schedule:dunning_runner": 1,
            "table:dunning_cases": 1,
        },
        baseline,
    )
    assert added == ["table:dunning_cases: 0 -> 1"]
    assert grew == []
    assert shrunk == []

    added, grew, shrunk = _ratchet_drift(
        {"class:DunningCase": 2, "schedule:dunning_runner": 1}, baseline
    )
    assert added == []
    assert grew == ["class:DunningCase: 1 -> 2"]
    assert shrunk == []

    added, grew, shrunk = _ratchet_drift({"class:DunningCase": 1}, baseline)
    assert added == []
    assert grew == []
    assert shrunk == ["schedule:dunning_runner: 1 -> 0"]
