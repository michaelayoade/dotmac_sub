from __future__ import annotations

import json

from app.services import cutover_balance_audit as audit


def _row(account_id: str, *, current: str, target: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "subscriber_name": "Subscriber",
        "subscriber_status": "active",
        "current_available": current,
        "target_available": target,
        "post_adjustment_entry_count": 0,
        "post_adjustment_net": "0.00",
        "target_adjustment_entry_count": 0,
        "target_adjustment_net": "0.00",
        "excluded_adjustment_entry_count": 0,
        "excluded_adjustment_net": "0.00",
        "inactive_opening_debits": "0.00",
    }


def _write_registry(tmp_path, entries: list[dict[str, str]]):
    path = tmp_path / "cutover_variances.json"
    path.write_text(json.dumps({"version": 1, "entries": entries}))
    return path


def test_accepted_variance_suppresses_matching_drift(monkeypatch, tmp_path):
    account_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda _db: [_row(account_id, current="125.00", target="100.00")],
    )
    registry = _write_registry(
        tmp_path,
        [
            {
                "account_id": account_id,
                "expected_drift": "25.00",
                "status": "accepted",
                "reason": "verified write-off variance",
                "recorded_at": "2026-07-04",
            }
        ],
    )

    result = audit.audit_cutover_balance_invariant(
        object(), variance_registry_path=registry
    )

    assert result["ok"] is True
    assert result["raw_drift_count"] == 1
    assert result["drift_count"] == 0
    assert result["registered_variance_count"] == 1
    assert result["registered_variance_total"] == "25.00"


def test_candidate_variance_does_not_suppress_drift(monkeypatch, tmp_path):
    account_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda _db: [_row(account_id, current="125.00", target="100.00")],
    )
    registry = _write_registry(
        tmp_path,
        [
            {
                "account_id": account_id,
                "expected_drift": "25.00",
                "status": "candidate",
                "reason": "not verified yet",
                "recorded_at": "2026-07-04",
            }
        ],
    )

    result = audit.audit_cutover_balance_invariant(
        object(), variance_registry_path=registry
    )

    assert result["ok"] is False
    assert result["raw_drift_count"] == 1
    assert result["drift_count"] == 1
    assert result["samples"][0]["drift"] == "25.00"
    assert result["registered_variance_count"] == 0


def test_partial_registered_variance_reports_residual(monkeypatch, tmp_path):
    account_id = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setattr(
        audit,
        "_rows",
        lambda _db: [_row(account_id, current="125.00", target="100.00")],
    )
    registry = _write_registry(
        tmp_path,
        [
            {
                "account_id": account_id,
                "expected_drift": "20.00",
                "status": "accepted",
                "reason": "partial verified variance",
                "recorded_at": "2026-07-04",
            }
        ],
    )

    result = audit.audit_cutover_balance_invariant(
        object(), variance_registry_path=registry
    )

    assert result["ok"] is False
    assert result["raw_drift_count"] == 1
    assert result["drift_count"] == 1
    assert result["samples"][0]["raw_drift"] == "25.00"
    assert result["samples"][0]["registered_variance"] == "20.00"
    assert result["samples"][0]["drift"] == "5.00"


def test_stale_registered_variance_keeps_audit_non_ok(monkeypatch, tmp_path):
    account_id = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(audit, "_rows", lambda _db: [])
    registry = _write_registry(
        tmp_path,
        [
            {
                "account_id": account_id,
                "expected_drift": "10.00",
                "status": "accepted",
                "reason": "old variance",
                "recorded_at": "2026-07-04",
            }
        ],
    )

    result = audit.audit_cutover_balance_invariant(
        object(), variance_registry_path=registry
    )

    assert result["ok"] is False
    assert result["drift_count"] == 0
    assert result["stale_registered_variance_count"] == 1
    assert result["stale_registered_variance_accounts"] == [account_id]


def test_duplicate_accepted_variance_rejected(tmp_path):
    account_id = "55555555-5555-4555-8555-555555555555"
    registry = _write_registry(
        tmp_path,
        [
            {
                "account_id": account_id,
                "expected_drift": "10.00",
                "status": "accepted",
                "reason": "first",
                "recorded_at": "2026-07-04",
            },
            {
                "account_id": account_id,
                "expected_drift": "10.00",
                "status": "accepted",
                "reason": "second",
                "recorded_at": "2026-07-04",
            },
        ],
    )

    try:
        audit._load_registered_variances(object(), variance_registry_path=registry)
    except ValueError as exc:
        assert account_id in str(exc)
    else:
        raise AssertionError("duplicate accepted variance should fail")
