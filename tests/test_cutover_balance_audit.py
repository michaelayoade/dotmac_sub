from __future__ import annotations

from decimal import Decimal

from app.services import cutover_balance_audit as audit


class DummyDb:
    pass


def _row(account_id: str, current: str, target: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "subscriber_name": f"Account {account_id}",
        "subscriber_status": "active",
        "current_available": Decimal(current),
        "target_available": Decimal(target),
        "post_adjustment_entry_count": 0,
        "post_adjustment_net": Decimal("0.00"),
        "target_adjustment_entry_count": 0,
        "target_adjustment_net": Decimal("0.00"),
        "excluded_adjustment_entry_count": 0,
        "excluded_adjustment_net": Decimal("0.00"),
        "inactive_opening_debits": Decimal("0.00"),
    }


def test_registered_variance_suppresses_exact_known_drift(monkeypatch):
    account_id = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(
        audit,
        "_load_registered_variances_from_db",
        lambda db: {account_id: Decimal("25.00")},
    )
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda db: [_row(account_id, "25.00", "0.00")],
    )

    result = audit.audit_cutover_balance_invariant(DummyDb())

    assert result["ok"] is True
    assert result["raw_drift_count"] == 1
    assert result["drift_count"] == 0
    assert result["registered_variance_count"] == 1
    assert result["registered_variance_total"] == "25.00"
    assert result["stale_registered_variance_count"] == 0


def test_changed_registered_variance_leaves_delta_and_reports_stale(monkeypatch):
    account_id = "00000000-0000-0000-0000-000000000002"
    monkeypatch.setattr(
        audit,
        "_load_registered_variances_from_db",
        lambda db: {account_id: Decimal("25.00")},
    )
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda db: [_row(account_id, "30.00", "0.00")],
    )

    result = audit.audit_cutover_balance_invariant(DummyDb())

    assert result["ok"] is False
    assert result["raw_drift_count"] == 1
    assert result["drift_count"] == 1
    assert result["overcredited_total"] == "5.00"
    assert result["stale_registered_variance_count"] == 1
    assert result["stale_registered_variance_accounts"] == [account_id]
    assert result["samples"][0]["registered_variance"] == "25.00"
    assert result["samples"][0]["drift"] == "5.00"


def test_registered_variance_for_missing_seeded_account_reports_stale(monkeypatch):
    account_id = "00000000-0000-0000-0000-000000000003"
    monkeypatch.setattr(
        audit,
        "_load_registered_variances_from_db",
        lambda db: {account_id: Decimal("25.00")},
    )
    monkeypatch.setattr(audit, "_rows", lambda db: [])

    result = audit.audit_cutover_balance_invariant(DummyDb())

    assert result["ok"] is False
    assert result["drift_count"] == 0
    assert result["stale_registered_variance_count"] == 1
    assert result["stale_registered_variance_accounts"] == [account_id]
