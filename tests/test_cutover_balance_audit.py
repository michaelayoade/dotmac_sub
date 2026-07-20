from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
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


def test_export_reconstructed_balance_packet_writes_review_files(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda db: [
            {
                **_row("00000000-0000-0000-0000-000000000004", "150.00", "100.00"),
                "deposit": Decimal("120.00"),
                "post_cutover_payments": Decimal("30.00"),
                "post_cutover_invoices": Decimal("50.00"),
                "target_adjustment_net": Decimal("0.00"),
            },
            {
                **_row("00000000-0000-0000-0000-000000000005", "75.00", "75.00"),
                "deposit": Decimal("75.00"),
            },
        ],
    )

    manifest = audit.export_reconstructed_balance_packet(
        DummyDb(), tmp_path, include_transaction_rows=False
    )

    assert manifest["population"] == 2
    assert manifest["drift_count"] == 1
    assert manifest["overcredited_count"] == 1
    assert manifest["overcredited_total"] == "50.00"
    assert (tmp_path / "manifest.json").exists()
    drift_text = (tmp_path / "drift_cases.csv").read_text()
    assert "difference_current_minus_reconstructed" in drift_text
    assert "00000000-0000-0000-0000-000000000004" in drift_text


def test_reconstructed_balance_correction_apply_is_idempotent(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        audit,
        "iter_reconstructed_balance_rows",
        lambda db: [
            {
                "account_id": str(subscriber.id),
                "subscriber_name": "Subscriber",
                "subscriber_status": "active",
                "current_local_available": "0.00",
                "reconstructed_balance": "25.00",
                "difference_current_minus_reconstructed": "-25.00",
            }
        ],
    )

    items = audit.build_reconstructed_balance_correction_items(
        db_session, snapshot_date="2026-07-09"
    )
    payload = audit.apply_reconstructed_balance_corrections(
        db_session, items, apply=True
    )
    db_session.commit()

    assert payload["counts"]["apply"] == 1
    entry = db_session.query(LedgerEntry).one()
    assert entry.entry_type is LedgerEntryType.credit
    assert entry.source is LedgerSource.adjustment
    assert entry.amount == Decimal("25.00")
    assert entry.memo.startswith("Correction: cutover reconstructed balance true-up")

    second_payload = audit.apply_reconstructed_balance_corrections(
        db_session, items, apply=True
    )
    assert second_payload["counts"]["skip"] == 1
