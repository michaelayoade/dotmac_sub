"""Tests for the bank cutover reconciliation classifier (read-only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.payment_proof import PaymentProof, PaymentProofStatus
from scripts.one_off.bank_cutover_reconcile import (
    LocalRecord,
    StatementRow,
    _load_local_records,
    _matches,
)


def _statement(**over) -> StatementRow:
    base = {
        "row_number": 2,
        "paid_date": datetime(2026, 6, 16, tzinfo=UTC).date(),
        "amount": Decimal("5000.00"),
        "reference": "",
        "narration": "",
        "raw": {},
    }
    base.update(over)
    return StatementRow(**base)


def _record(**over) -> LocalRecord:
    base = {
        "record_type": "payment",
        "record_id": str(uuid.uuid4()),
        "account_id": "",
        "subscriber_number": "",
        "subscriber_name": "",
        "status": "succeeded",
        "paid_at": datetime(2026, 6, 16, tzinfo=UTC),
        "amount": Decimal("5000.00"),
        "reference": "",
        "memo": "",
    }
    base.update(over)
    return LocalRecord(**base)


def _classify(statement: StatementRow, records: list[LocalRecord]) -> str:
    matches = _matches(statement, records)
    if len(matches) == 1:
        return "matched"
    if not matches:
        return "missing_from_local"
    return "ambiguous"


def test_strong_reference_match_is_matched():
    statement = _statement(reference="TRF-12345", narration="John Doe")
    record = _record(reference="TRF-12345")
    matches = _matches(statement, [record])
    assert matches == [record]
    assert _classify(statement, [record]) == "matched"


def test_text_match_on_subscriber_number_is_matched():
    statement = _statement(narration="payment for SUB-001 thanks")
    record = _record(subscriber_number="SUB-001")
    assert _classify(statement, [record]) == "matched"


def test_amount_date_only_coincidence_is_not_a_match():
    # Two records share the credit amount within tolerance and fall in the date
    # window, but neither has a reference or text match: must NOT be counted.
    statement = _statement(reference="TRF-REAL", narration="ACME LTD")
    coincidence_a = _record(reference="TRF-OTHER-1", subscriber_number="SUB-A")
    coincidence_b = _record(reference="TRF-OTHER-2", subscriber_number="SUB-B")
    matches = _matches(statement, [coincidence_a, coincidence_b])
    assert matches == []
    # Not a false matched and not a false ambiguous either.
    assert _classify(statement, [coincidence_a, coincidence_b]) == "missing_from_local"


def test_amount_tolerance_excludes_distant_amounts():
    statement = _statement(reference="TRF-1", amount=Decimal("5000.00"))
    record = _record(reference="TRF-1", amount=Decimal("5000.50"))
    assert _matches(statement, [record]) == []


def test_reference_match_wins_over_text_only_matches():
    statement = _statement(reference="TRF-1", narration="SUB-001")
    ref_record = _record(reference="TRF-1", record_id="ref")
    text_record = _record(subscriber_number="SUB-001", record_id="text")
    matches = _matches(statement, [ref_record, text_record])
    assert matches == [ref_record]
    assert _classify(statement, [ref_record, text_record]) == "matched"


def test_two_text_matches_are_ambiguous():
    statement = _statement(narration="SUB-001 / SUB-002")
    a = _record(subscriber_number="SUB-001", record_id="a")
    b = _record(subscriber_number="SUB-002", record_id="b")
    assert _classify(statement, [a, b]) == "ambiguous"


def test_rejected_proof_excluded_from_local_records(db_session):
    start = datetime(2026, 6, 15, tzinfo=UTC)
    end = datetime(2026, 6, 19, tzinfo=UTC)
    created = datetime(2026, 6, 16, tzinfo=UTC)
    db_session.add_all(
        [
            PaymentProof(
                account_id=None,
                amount=Decimal("5000.00"),
                reference="TRF-VERIFIED",
                status=PaymentProofStatus.verified,
                file_path="proofs/verified.pdf",
                created_at=created,
            ),
            PaymentProof(
                account_id=None,
                amount=Decimal("5000.00"),
                reference="TRF-REJECTED",
                status=PaymentProofStatus.rejected,
                file_path="proofs/rejected.pdf",
                created_at=created,
            ),
            PaymentProof(
                account_id=None,
                amount=Decimal("5000.00"),
                reference="TRF-SUBMITTED",
                status=PaymentProofStatus.submitted,
                file_path="proofs/submitted.pdf",
                created_at=created,
            ),
        ]
    )
    db_session.commit()

    records = _load_local_records(db_session, start, end)
    proof_refs = {r.reference for r in records if r.record_type == "payment_proof"}
    assert "TRF-VERIFIED" in proof_refs
    assert "TRF-REJECTED" not in proof_refs
    assert "TRF-SUBMITTED" not in proof_refs
