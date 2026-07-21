from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.subscriber import Subscriber
from app.services import party as party_service
from app.services.customer_name_repairs import CustomerNameRepairError
from scripts.one_off.restore_crm_placeholder_identity import (
    BATCH_ACTION,
    REMEDIATION_ACTION,
    RecoveryCandidate,
    apply_recovery,
    plan_recovery,
    recovery_manifest_digest,
)


def _incident_event(subscriber: Subscriber, changes: dict) -> AuditEvent:
    return AuditEvent(
        occurred_at=datetime(2026, 7, 20, 15, 5, tzinfo=UTC),
        actor_type=AuditActorType.service,
        actor_id="crm_webhook",
        action="crm_customer_identity_update",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        status_code=200,
        is_success=True,
        metadata_={"source": "crm_customer_webhook", "changes": changes},
    )


def test_recovery_plan_contains_only_audited_name_fields_and_writes_nothing(
    db_session,
):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Customer Customer",
        email="remote@example.com",
        phone="08010000000",
        subscriber_number="100000001",
        metadata_={"subscriber_category": "residential"},
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Existing", "new": "Customer"},
                "last_name": {"old": "Identity", "new": "Customer"},
                "display_name": {
                    "old": "Existing Identity",
                    "new": "Customer Customer",
                },
                "email": {
                    "old": "existing@example.com",
                    "new": "remote@example.com",
                },
                "phone": {"old": "+2348010000000", "new": "08010000000"},
                "category": {"old": "business", "new": "residential"},
            },
        )
    )
    db_session.commit()

    planned = plan_recovery(db_session)
    assert len(planned) == 1
    assert planned[0].classification == "eligible"
    assert set(planned[0].restorations) == {
        "first_name",
        "last_name",
        "display_name",
    }

    db_session.refresh(subscriber)
    assert (subscriber.first_name, subscriber.last_name, subscriber.display_name) == (
        "Customer",
        "Customer",
        "Customer Customer",
    )
    assert subscriber.email == "remote@example.com"
    assert subscriber.phone == "08010000000"
    assert subscriber.category.value == "residential"
    assert db_session.query(AuditEvent).count() == 1


def test_recovery_skips_whole_account_when_any_field_has_later_drift(db_session):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Manually Corrected",
        email="remote@example.com",
        subscriber_number="100000002",
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Original", "new": "Customer"},
                "last_name": {"old": "Identity", "new": "Customer"},
                "display_name": {
                    "old": "Original Identity",
                    "new": "Customer Customer",
                },
                "email": {
                    "old": "original@example.com",
                    "new": "remote@example.com",
                },
            },
        )
    )
    db_session.commit()

    candidates = plan_recovery(db_session)
    assert candidates[0].classification == "skip_drift"
    assert candidates[0].conflict_fields == ["display_name"]
    db_session.refresh(subscriber)
    assert subscriber.first_name == "Customer"
    assert subscriber.email == "remote@example.com"
    assert db_session.query(AuditEvent).count() == 1


def test_recovery_ignores_non_placeholder_crm_updates(db_session):
    subscriber = Subscriber(
        first_name="Changed",
        last_name="Name",
        display_name="Changed Name",
        email="changed@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Original", "new": "Changed"},
                "display_name": {"old": "Original Name", "new": "Changed Name"},
            },
        )
    )
    db_session.commit()

    assert plan_recovery(db_session) == []


def test_recovery_plan_includes_partial_placeholder_name_fields(db_session):
    subscriber = Subscriber(
        first_name="Existing",
        last_name="Customer",
        display_name="Existing Customer",
        email="partial-placeholder@example.com",
        subscriber_number="100000008",
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Existing", "new": "Existing"},
                "last_name": {"old": "Identity", "new": "Customer"},
                "display_name": {
                    "old": "Existing Identity",
                    "new": "Existing Customer",
                },
            },
        )
    )
    db_session.commit()

    candidates = plan_recovery(db_session)

    assert len(candidates) == 1
    assert candidates[0].classification == "eligible"
    assert candidates[0].already_restored_fields == ["first_name"]
    assert candidates[0].restorations == {
        "last_name": "Identity",
        "display_name": "Existing Identity",
    }


def test_recovery_apply_is_guarded_audited_and_idempotent(db_session):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Customer Customer",
        email="guarded-repair@example.com",
        subscriber_number="100000003",
    )
    db_session.add(subscriber)
    db_session.flush()
    incident = _incident_event(
        subscriber,
        {
            "first_name": {"old": "Guarded", "new": "Customer"},
            "last_name": {"old": "Identity", "new": "Customer"},
            "display_name": {
                "old": "Guarded Identity",
                "new": "Customer Customer",
            },
        },
    )
    db_session.add(incident)
    db_session.flush()
    subscriber_id = subscriber.id
    incident_id = incident.id
    db_session.commit()

    candidates = [
        RecoveryCandidate(
            subscriber_id=subscriber_id,
            source_audit_ids=[str(incident_id)],
            expected_current={
                "first_name": "Customer",
                "last_name": "Customer",
                "display_name": "Customer Customer",
            },
            replacement={
                "first_name": "Guarded",
                "last_name": "Identity",
                "display_name": "Guarded Identity",
            },
            restorations={
                "first_name": "Guarded",
                "last_name": "Identity",
                "display_name": "Guarded Identity",
            },
        )
    ]
    digest = recovery_manifest_digest(candidates)
    applied, already_applied = apply_recovery(
        db_session,
        candidates,
        manifest_digest=digest,
        actor_id="incident-repair-test",
        reason="Restore audited pre-incident identity",
        target="test-db",
    )

    assert (applied, already_applied) == (1, False)
    db_session.refresh(subscriber)
    assert (subscriber.first_name, subscriber.last_name, subscriber.display_name) == (
        "Guarded",
        "Identity",
        "Guarded Identity",
    )
    row_audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == REMEDIATION_ACTION)
        .one()
    )
    assert row_audit.metadata_["manifest_digest"] == digest
    assert row_audit.metadata_["source_audit_ids"] == [str(incident.id)]
    batch_audit = (
        db_session.query(AuditEvent).filter(AuditEvent.action == BATCH_ACTION).one()
    )
    assert batch_audit.entity_id == digest

    db_session.commit()
    assert apply_recovery(
        db_session,
        candidates,
        manifest_digest=digest,
        actor_id="incident-repair-test",
        reason="Restore audited pre-incident identity",
        target="test-db",
    ) == (0, True)
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == REMEDIATION_ACTION)
        .count()
        == 1
    )


def test_recovery_apply_rolls_back_every_row_when_manifest_drifts(db_session):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Customer Customer",
        email="stale-repair@example.com",
        subscriber_number="100000004",
    )
    db_session.add(subscriber)
    db_session.flush()
    incident = _incident_event(
        subscriber,
        {
            "first_name": {"old": "Original", "new": "Customer"},
            "last_name": {"old": "Identity", "new": "Customer"},
            "display_name": {
                "old": "Original Identity",
                "new": "Customer Customer",
            },
        },
    )
    db_session.add(incident)
    db_session.flush()
    subscriber_id = subscriber.id
    incident_id = incident.id
    db_session.commit()
    candidates = [
        RecoveryCandidate(
            subscriber_id=subscriber_id,
            source_audit_ids=[str(incident_id)],
            expected_current={
                "first_name": "Customer",
                "last_name": "Customer",
                "display_name": "Customer Customer",
            },
            replacement={
                "first_name": "Original",
                "last_name": "Identity",
                "display_name": "Original Identity",
            },
            restorations={
                "first_name": "Original",
                "last_name": "Identity",
                "display_name": "Original Identity",
            },
        )
    ]
    digest = recovery_manifest_digest(candidates)

    subscriber.display_name = "Operator Changed"
    db_session.commit()
    with pytest.raises(CustomerNameRepairError) as exc_info:
        apply_recovery(
            db_session,
            candidates,
            manifest_digest=digest,
            actor_id="incident-repair-test",
            reason="Restore audited pre-incident identity",
            target="test-db",
        )
    assert exc_info.value.code == "customer.name_repairs.stale_manifest"

    db_session.refresh(subscriber)
    assert subscriber.first_name == "Customer"
    assert subscriber.display_name == "Operator Changed"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == REMEDIATION_ACTION)
        .count()
        == 0
    )


def test_recovery_apply_rejects_incomplete_audit_evidence(db_session):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Customer Customer",
        email="incomplete-evidence@example.com",
        subscriber_number="100000007",
    )
    db_session.add(subscriber)
    db_session.flush()
    incident = _incident_event(
        subscriber,
        {
            "first_name": {"old": "Original", "new": "Customer"},
            "last_name": {"old": "Identity", "new": "Customer"},
        },
    )
    db_session.add(incident)
    db_session.flush()
    subscriber_id = subscriber.id
    incident_id = incident.id
    db_session.commit()
    candidates = [
        RecoveryCandidate(
            subscriber_id=subscriber_id,
            source_audit_ids=[str(incident_id)],
            expected_current={
                "first_name": "Customer",
                "last_name": "Customer",
                "display_name": "Customer Customer",
            },
            replacement={
                "first_name": "Original",
                "last_name": "Identity",
                "display_name": "Original Identity",
            },
            restorations={
                "first_name": "Original",
                "last_name": "Identity",
                "display_name": "Original Identity",
            },
        )
    ]

    with pytest.raises(CustomerNameRepairError) as exc_info:
        apply_recovery(
            db_session,
            candidates,
            manifest_digest=recovery_manifest_digest(candidates),
            actor_id="incident-repair-test",
            reason="Restore audited pre-incident identity",
            target="test-db",
        )

    assert exc_info.value.code == "customer.name_repairs.invalid_evidence"
    db_session.refresh(subscriber)
    assert subscriber.display_name == "Customer Customer"


def test_recovery_public_manifest_contains_no_identity_values(db_session):
    subscriber = Subscriber(
        first_name="Unknown",
        last_name="Unknown",
        display_name="Unknown Unknown",
        email="pii-free-repair@example.com",
        subscriber_number="100000005",
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Private", "new": "Unknown"},
                "last_name": {"old": "Identity", "new": "Unknown"},
                "display_name": {
                    "old": "Private Identity",
                    "new": "Unknown Unknown",
                },
            },
        )
    )
    db_session.commit()

    public = str(plan_recovery(db_session)[0].public_dict())
    assert "Private" not in public
    assert "Unknown" not in public
    assert "100000005" not in public


def test_recovery_refuses_party_bound_subscriber(db_session):
    subscriber = Subscriber(
        first_name="Customer",
        last_name="Customer",
        display_name="Customer Customer",
        email="party-bound-repair@example.com",
        subscriber_number="100000006",
    )
    db_session.add(subscriber)
    db_session.flush()
    canonical_party = party_service.create_party(
        db_session,
        party_type="person",
        display_name="Canonical Identity",
    )
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=canonical_party.id,
        source="test",
        reason="prove remediation cutover guard",
    )
    db_session.add(
        _incident_event(
            subscriber,
            {
                "first_name": {"old": "Canonical", "new": "Customer"},
                "last_name": {"old": "Identity", "new": "Customer"},
                "display_name": {
                    "old": "Canonical Identity",
                    "new": "Customer Customer",
                },
            },
        )
    )
    db_session.commit()

    candidates = plan_recovery(db_session)
    assert len(candidates) == 1
    assert candidates[0].classification == "skip_party_bound"
    assert recovery_manifest_digest(candidates)
