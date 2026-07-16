"""Reviewed gap actions cannot bypass independent prepaid reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from scripts.one_off.adjudicate_prepaid_funding_gaps import (
    CANONICAL_PAYMENT_REQUIRED,
    DECISION_SCHEMA,
    QUARANTINE,
    SOURCE_EVIDENCE_REQUIRED,
    GapAdjudicationError,
    build_gap_action_plan,
)
from scripts.one_off.export_prepaid_funding_snapshot import FundingSnapshotExport


def _blockers() -> tuple[dict, str, str]:
    service_account = str(uuid4())
    baseline_account = str(uuid4())
    export = FundingSnapshotExport(
        captured_at=datetime(2026, 7, 12, tzinfo=UTC),
        source="splynx-final-plus-native-events:reviewed-test",
        currency="NGN",
        candidate_ids=(service_account, baseline_account),
        positions={service_account: Decimal("10.00")},
        incomplete={service_account: ("source_service_without_paid_through_period",)},
        missing_baseline=(baseline_account,),
    )
    return export.diagnostics_payload(), service_account, baseline_account


def _decision_packet(blockers: dict, decisions: list[dict]) -> dict:
    return {
        "schema": DECISION_SCHEMA,
        "blocker_manifest_sha256": blockers["blocker_manifest_sha256"],
        "review_id": "finance-review:prepaid-gap-test",
        "reviewed_by": "finance-reviewer-test",
        "reviewed_at": "2026-07-13T00:00:00Z",
        "decisions": decisions,
    }


def _source_decision(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "reason": "source_service_without_paid_through_period",
        "disposition": SOURCE_EVIDENCE_REQUIRED,
        "evidence_ref": "finance-case:source-service-test",
    }


def _quarantine_decision(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "reason": "missing_source_baseline",
        "disposition": QUARANTINE,
        "evidence_ref": "finance-case:missing-baseline-test",
    }


def test_decisions_cover_exact_hash_bound_blocker_pairs():
    blockers, service_account, baseline_account = _blockers()
    packet = _decision_packet(
        blockers,
        [_source_decision(service_account), _quarantine_decision(baseline_account)],
    )

    plan = build_gap_action_plan(
        blockers,
        packet,
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert plan["status"] == "blocked_pending_owner_actions_and_independent_replay"
    assert plan["action_count"] == 2
    assert plan["disposition_counts"] == {
        QUARANTINE: 1,
        SOURCE_EVIDENCE_REQUIRED: 1,
    }
    assert {action["next_action"] for action in plan["actions"]} == {
        "keep_quarantined_and_rerun_after_resolution",
        "replace_independent_source_evidence_and_rerun",
    }


def test_missing_or_stale_decisions_cannot_produce_an_action_plan():
    blockers, service_account, _baseline_account = _blockers()
    incomplete = _decision_packet(blockers, [_source_decision(service_account)])

    with pytest.raises(GapAdjudicationError, match="exact blocker manifest"):
        build_gap_action_plan(
            blockers,
            incomplete,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )

    stale = {**incomplete, "blocker_manifest_sha256": "0" * 64}
    with pytest.raises(GapAdjudicationError, match="not bound"):
        build_gap_action_plan(
            blockers,
            stale,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )


def test_bank_amount_and_date_without_attribution_cannot_authorize_payment():
    blockers, service_account, baseline_account = _blockers()
    payment = {
        "account_id": baseline_account,
        "reason": "missing_source_baseline",
        "disposition": CANONICAL_PAYMENT_REQUIRED,
        "evidence_ref": "bank-review:statement-credit-test",
        "amount": "2500.00",
        "currency": "NGN",
        "occurred_at": "2026-07-01T12:00:00Z",
        "definitive_attribution": False,
        "evidence_sha256": "1" * 64,
    }
    packet = _decision_packet(
        blockers,
        [_source_decision(service_account), payment],
    )

    with pytest.raises(GapAdjudicationError, match="amount/date coincidence"):
        build_gap_action_plan(
            blockers,
            packet,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )

    pre_handoff = {
        **payment,
        "occurred_at": "2026-06-15T12:00:00Z",
        "definitive_attribution": True,
    }
    packet = _decision_packet(
        blockers,
        [_source_decision(service_account), pre_handoff],
    )
    with pytest.raises(GapAdjudicationError, match="reviewed source baseline"):
        build_gap_action_plan(
            blockers,
            packet,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )


def test_definitively_attributed_post_handoff_payment_routes_to_owner():
    blockers, service_account, baseline_account = _blockers()
    payment = {
        "account_id": baseline_account,
        "reason": "missing_source_baseline",
        "disposition": CANONICAL_PAYMENT_REQUIRED,
        "evidence_ref": "bank-review:statement-credit-test",
        "amount": "2500.00",
        "currency": "NGN",
        "occurred_at": "2026-07-01T12:00:00Z",
        "definitive_attribution": True,
        "evidence_sha256": "1" * 64,
    }
    packet = _decision_packet(
        blockers,
        [_source_decision(service_account), payment],
    )

    plan = build_gap_action_plan(
        blockers,
        packet,
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    payment_action = next(
        action
        for action in plan["actions"]
        if action["disposition"] == CANONICAL_PAYMENT_REQUIRED
    )

    assert payment_action["action_owner"] == "financial.payments"
    assert payment_action["next_action"] == "preview_and_confirm_missing_payment"
    assert payment_action["idempotency_key"].startswith("prepaid-gap-payment:")
    assert "definitive_attribution" not in payment_action

    duplicate_evidence = {
        **payment,
        "account_id": service_account,
        "reason": "source_service_without_paid_through_period",
    }
    duplicate_packet = _decision_packet(
        blockers,
        [duplicate_evidence, payment],
    )
    with pytest.raises(GapAdjudicationError, match="cannot authorize multiple"):
        build_gap_action_plan(
            blockers,
            duplicate_packet,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )


def test_decision_packet_rejects_raw_bank_or_customer_fields():
    blockers, service_account, baseline_account = _blockers()
    payment = {
        "account_id": baseline_account,
        "reason": "missing_source_baseline",
        "disposition": CANONICAL_PAYMENT_REQUIRED,
        "evidence_ref": "bank-review:statement-credit-test",
        "amount": "2500.00",
        "currency": "NGN",
        "occurred_at": "2026-07-01T12:00:00Z",
        "definitive_attribution": True,
        "evidence_sha256": "1" * 64,
        "narration": "raw bank narration must stay outside this packet",
    }
    packet = _decision_packet(
        blockers,
        [_source_decision(service_account), payment],
    )

    with pytest.raises(GapAdjudicationError, match="unexpected=narration"):
        build_gap_action_plan(
            blockers,
            packet,
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )
