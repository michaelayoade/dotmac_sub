from __future__ import annotations

import json
import stat
import uuid
from types import SimpleNamespace

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.sales import Lead, SalesOrder, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.services import party_identity_audit as identity_audit
from scripts.migration.audit_subscriber_identity import (
    _set_transaction_read_only,
    write_audit_artifacts,
)


def _facts(**overrides) -> identity_audit.SubscriberIdentityFacts:
    values = {
        "subscriber_id": uuid.uuid4(),
        "first_name": "Ada",
        "last_name": "Okafor",
        "email": f"ada-{uuid.uuid4().hex}@dotmac.ng",
        "phone": "+2348012345678",
        "account_status": "active",
        "is_active": True,
    }
    values.update(overrides)
    return identity_audit.SubscriberIdentityFacts(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"has_active_subscription": True}, "active_subscriber"),
        ({"has_any_subscription": True}, "inactive_subscriber"),
        ({"has_customer_sales_order": True}, "customer"),
        ({"has_sales_order": True}, "lead"),
        ({"has_lead": True}, "lead"),
        ({"email_verified": True}, "verified_contact"),
        ({}, "unverified_record"),
    ),
)
def test_lifecycle_cohorts_follow_strongest_native_evidence(overrides, expected):
    cohort, _evidence, _contradictions = identity_audit.classify_lifecycle(
        _facts(**overrides)
    )

    assert cohort.value == expected


def test_lifecycle_contradictions_are_review_findings():
    cohort, _evidence, contradictions = identity_audit.classify_lifecycle(
        _facts(
            has_active_subscription=True,
            is_active=False,
            account_status="canceled",
            party_status="lead",
        )
    )

    assert cohort is identity_audit.LifecycleCohort.active_subscriber
    assert "active_subscription_on_inactive_account" in contradictions
    assert "commercial_evidence_exceeds_party_status" in contradictions


def test_billing_block_does_not_demote_active_subscriber_lifecycle():
    cohort, evidence, contradictions = identity_audit.classify_lifecycle(
        _facts(
            has_active_subscription=True,
            has_any_subscription=True,
            account_status="blocked",
            is_active=False,
        )
    )

    assert cohort is identity_audit.LifecycleCohort.active_subscriber
    assert "active_subscription" in evidence
    assert "active_subscription_on_inactive_account" not in contradictions


def test_support_history_is_context_and_does_not_promote_lifecycle():
    cohort, evidence, _contradictions = identity_audit.classify_lifecycle(
        _facts(has_support_history=True)
    )

    assert cohort is identity_audit.LifecycleCohort.unverified_record
    assert "support_history" in evidence


def test_nonproduction_declarations_and_heuristics_stay_distinct():
    declared, _ = identity_audit.classify_record(_facts(metadata={"is_test": True}))
    suspected, evidence = identity_audit.classify_record(
        _facts(email="customer@example.com")
    )
    production, _ = identity_audit.classify_record(
        _facts(email="customer@realbusiness.ng")
    )

    assert declared is identity_audit.RecordClassification.declared_nonproduction
    assert suspected is identity_audit.RecordClassification.suspected_nonproduction
    assert evidence == ("test_email_domain_pattern",)
    assert production is identity_audit.RecordClassification.production_candidate


def test_duplicate_groups_are_evidence_and_never_merge_authority():
    first = _facts(
        email="shared@realbusiness.ng",
        phone="08012345678",
        nin="12345678901",
        verified_nin=True,
    )
    second = _facts(
        email="SHARED@REALBUSINESS.NG",
        phone="+234 801 234 5678",
        nin="12345678901",
        verified_nin=True,
    )

    groups = identity_audit.build_duplicate_candidate_groups((first, second))

    assert {group.evidence_type for group in groups} >= {
        "verified_nin_exact",
        "email_phone_exact",
        "name_phone_exact",
        "email_exact",
        "phone_exact",
    }
    assert any(
        group.confidence is identity_audit.DuplicateConfidence.high for group in groups
    )
    assert all(group.automatic_merge_allowed is False for group in groups)


def test_weak_shared_contact_does_not_force_duplicate_disposition():
    first = _facts(
        email="reseller-owner@realbusiness.ng",
        phone="08011111111",
        has_lead=True,
    )
    second = _facts(
        first_name="Bola",
        last_name="Adeyemi",
        email="reseller-owner@realbusiness.ng",
        phone="08022222222",
        has_lead=True,
    )

    audit = identity_audit.resolve_subscriber_identity_audit((first, second))

    assert all(
        row.strongest_duplicate_confidence is identity_audit.DuplicateConfidence.weak
        for row in audit.rows
    )
    assert all(
        row.recommended_disposition
        is identity_audit.ReviewDisposition.ready_for_party_backfill
        for row in audit.rows
    )


def test_medium_duplicate_and_nonproduction_disposition_precedence():
    first = _facts(
        email="same@realbusiness.ng",
        phone="08012345678",
        has_lead=True,
        metadata={"is_test": True},
    )
    second = _facts(
        first_name="Bola",
        last_name="Adeyemi",
        email="same@realbusiness.ng",
        phone="08012345678",
        has_lead=True,
    )

    audit = identity_audit.resolve_subscriber_identity_audit((first, second))
    rows = {row.subscriber_id: row for row in audit.rows}

    assert rows[first.subscriber_id].recommended_disposition is (
        identity_audit.ReviewDisposition.review_and_quarantine_nonproduction
    )
    assert rows[second.subscriber_id].recommended_disposition is (
        identity_audit.ReviewDisposition.manual_duplicate_review
    )


def test_database_audit_uses_native_facts_and_remains_read_only(
    db_session, catalog_offer
):
    active = Subscriber(
        first_name="Active",
        last_name="Customer",
        email="active@realbusiness.ng",
        phone="08011111111",
    )
    lead = Subscriber(
        first_name="Lead",
        last_name="Person",
        email="lead@realbusiness.ng",
        phone="08022222222",
    )
    contact = Subscriber(
        first_name="Verified",
        last_name="Contact",
        email="contact@realbusiness.ng",
        email_verified=True,
    )
    confirmed_order_person = Subscriber(
        first_name="Confirmed",
        last_name="Order",
        email="confirmed-order@realbusiness.ng",
    )
    draft_order_person = Subscriber(
        first_name="Draft",
        last_name="Order",
        email="draft-order@realbusiness.ng",
    )
    db_session.add_all(
        (active, lead, contact, confirmed_order_person, draft_order_person)
    )
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=active.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
        )
    )
    db_session.add(Lead(subscriber_id=lead.id, title="New enquiry"))
    db_session.add_all(
        (
            SalesOrder(
                subscriber_id=confirmed_order_person.id,
                status=SalesOrderStatus.confirmed.value,
            ),
            SalesOrder(
                subscriber_id=draft_order_person.id,
                status=SalesOrderStatus.draft.value,
            ),
        )
    )
    db_session.flush()
    assert not db_session.new
    assert not db_session.dirty

    audit = identity_audit.build_subscriber_identity_audit(db_session)
    rows = {row.subscriber_id: row for row in audit.rows}

    assert rows[active.id].lifecycle_cohort is (
        identity_audit.LifecycleCohort.active_subscriber
    )
    assert rows[lead.id].lifecycle_cohort is identity_audit.LifecycleCohort.lead
    assert rows[contact.id].lifecycle_cohort is (
        identity_audit.LifecycleCohort.verified_contact
    )
    assert rows[confirmed_order_person.id].lifecycle_cohort is (
        identity_audit.LifecycleCohort.customer
    )
    assert rows[draft_order_person.id].lifecycle_cohort is (
        identity_audit.LifecycleCohort.lead
    )
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


def test_collector_supports_schema_before_field_verification_table(
    db_session, monkeypatch
):
    subscriber = Subscriber(
        first_name="Legacy",
        last_name="Schema",
        email="legacy-schema@realbusiness.ng",
        email_verified=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    monkeypatch.setattr(
        identity_audit,
        "inspect",
        lambda _bind: SimpleNamespace(get_table_names=lambda: []),
    )

    facts = identity_audit.collect_subscriber_identity_facts(db_session)
    selected = next(item for item in facts if item.subscriber_id == subscriber.id)

    assert selected.email_verified is True
    assert selected.verified_contact_methods == frozenset()


def test_private_artifacts_exclude_raw_identity_values(tmp_path):
    raw_email = "private.person@realbusiness.ng"
    raw_phone = "+2348012345678"
    raw_nin = "12345678901"
    facts = (
        _facts(
            email=raw_email,
            phone=raw_phone,
            nin=raw_nin,
            verified_nin=True,
            has_lead=True,
        ),
        _facts(
            email=raw_email,
            phone=raw_phone,
            nin=raw_nin,
            verified_nin=True,
            has_lead=True,
        ),
    )
    audit = identity_audit.resolve_subscriber_identity_audit(facts)

    paths = write_audit_artifacts(audit, tmp_path / "identity-audit")

    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert raw_email not in combined
    assert raw_phone not in combined
    assert raw_nin not in combined
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    summary = json.loads(paths[0].read_text(encoding="utf-8"))
    assert summary["artifact_contract"] == {
        "automatic_merge_allowed": False,
        "contains_raw_contact_values": False,
        "missing_optional_sources": [],
        "read_only": True,
    }


def test_postgres_audit_uses_one_repeatable_read_only_snapshot():
    statements: list[str] = []
    fake_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement: statements.append(str(statement)),
    )

    _set_transaction_read_only(fake_db)

    assert statements == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"]
