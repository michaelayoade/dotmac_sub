from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException

from app.api import domains_network_access
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.schemas.network import OntAssignmentCreate, OntAssignmentUpdate
from app.services.network.ont_assignment_identity import (
    OntAssignmentIdentityError,
    approve_assignment_identity_repair,
    execute_assignment_identity_repair,
    preview_assignment_identity_repair,
    propose_assignment_identity_repair,
)
from app.services.web_network_ont_identity_reviews import (
    list_assignment_identity_candidates,
    preview_from_explicit_form,
    propose_from_explicit_preview,
)


def _identity_plant(db_session, subscription):
    suffix = uuid.uuid4().hex[:10]
    old_olt = OLTDevice(
        name=f"Old OLT {suffix}", hostname=f"old-olt-{suffix}", is_active=True
    )
    target_olt = OLTDevice(
        name=f"Target OLT {suffix}",
        hostname=f"target-olt-{suffix}",
        is_active=True,
    )
    db_session.add_all([old_olt, target_olt])
    db_session.flush()
    old_pon = PonPort(olt_id=old_olt.id, name="0/1/1", is_active=True)
    target_pon = PonPort(olt_id=target_olt.id, name="0/2/3", is_active=True)
    db_session.add_all([old_pon, target_pon])
    db_session.flush()
    primary_ont = OntUnit(
        serial_number=f"PRIMARY-{suffix}",
        olt_device_id=old_olt.id,
        pon_port_id=old_pon.id,
        is_active=True,
    )
    duplicate_ont = OntUnit(
        serial_number=f"DUPLICATE-{suffix}",
        olt_device_id=target_olt.id,
        pon_port_id=target_pon.id,
        is_active=True,
    )
    db_session.add_all([primary_ont, duplicate_ont])
    db_session.flush()
    primary = OntAssignment(
        ont_unit_id=primary_ont.id,
        pon_port_id=old_pon.id,
        active=True,
    )
    duplicate = OntAssignment(
        ont_unit_id=duplicate_ont.id,
        pon_port_id=target_pon.id,
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        active=True,
    )
    db_session.add_all([primary, duplicate])
    db_session.commit()
    return {
        "old_pon": old_pon,
        "target_olt": target_olt,
        "target_pon": target_pon,
        "primary_ont": primary_ont,
        "duplicate_ont": duplicate_ont,
        "primary": primary,
        "duplicate": duplicate,
    }


def _propose_canonical(db_session, subscription, plant):
    return propose_assignment_identity_repair(
        db_session,
        "canonicalize",
        plant["primary"].id,
        target_subscription_id=subscription.id,
        target_pon_port_id=plant["target_pon"].id,
        target_olt_id=plant["target_olt"].id,
        duplicate_assignment_ids=[plant["duplicate"].id],
        proposed_by="planner@example.com",
        reason="Exact subscription, ONT, PON, and OLT identifiers verified",
    )


def test_canonical_repair_requires_exact_conflicts_and_preserves_audit(
    db_session, subscription
):
    plant = _identity_plant(db_session, subscription)

    with pytest.raises(OntAssignmentIdentityError, match="exactly cover"):
        preview_assignment_identity_repair(
            db_session,
            "canonicalize",
            plant["primary"].id,
            target_subscription_id=subscription.id,
            target_pon_port_id=plant["target_pon"].id,
            target_olt_id=plant["target_olt"].id,
        )
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0

    decision = _propose_canonical(db_session, subscription, plant)
    replay = _propose_canonical(db_session, subscription, plant)
    assert replay.id == decision.id
    with pytest.raises(OntAssignmentIdentityError, match="proposer cannot review"):
        approve_assignment_identity_repair(
            db_session,
            decision.id,
            reviewed_by="planner@example.com",
            review_notes="Self review must fail",
        )

    approve_assignment_identity_repair(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Electronic identifiers independently checked",
    )
    applied = execute_assignment_identity_repair(
        db_session, decision.id, executed_by="executor@example.com"
    )
    db_session.refresh(plant["primary"])
    db_session.refresh(plant["duplicate"])
    db_session.refresh(plant["primary_ont"])

    assert applied.status == "applied"
    assert plant["primary"].subscription_id == subscription.id
    assert plant["primary"].subscriber_id == subscription.subscriber_id
    assert plant["primary"].pon_port_id == plant["target_pon"].id
    assert plant["primary_ont"].pon_port_id == plant["target_pon"].id
    assert plant["primary_ont"].olt_device_id == plant["target_olt"].id
    assert plant["duplicate"].active is False
    assert plant["duplicate"].release_reason == "identity_repair_duplicate"
    expected_digest = hashlib.sha256(
        json.dumps(
            applied.result_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert applied.result_sha256 == expected_digest
    assert (
        execute_assignment_identity_repair(
            db_session, decision.id, executed_by="another-executor@example.com"
        ).id
        == decision.id
    )


def test_repair_closes_stale_without_overwriting_new_identity(db_session, subscription):
    plant = _identity_plant(db_session, subscription)
    decision = _propose_canonical(db_session, subscription, plant)
    approve_assignment_identity_repair(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Electronic identifiers independently checked",
    )
    plant["primary"].subscriber_id = subscription.subscriber_id
    db_session.commit()

    closed = execute_assignment_identity_repair(
        db_session, decision.id, executed_by="executor@example.com"
    )
    db_session.refresh(plant["primary"])
    db_session.refresh(plant["duplicate"])

    assert closed.status == "closed"
    assert closed.closed_reason == "authoritative_assignment_identity_inputs_changed"
    assert closed.result_payload["outcome"] == "closed_stale"
    assert plant["primary"].pon_port_id == plant["old_pon"].id
    assert plant["duplicate"].active is True


def test_same_subscriber_without_exact_subscription_is_not_inferred(
    db_session, subscription
):
    plant = _identity_plant(db_session, subscription)
    plant["duplicate"].active = False
    legacy_ont = OntUnit(
        serial_number=f"LEGACY-{uuid.uuid4().hex[:10]}", is_active=True
    )
    db_session.add(legacy_ont)
    db_session.flush()
    legacy = OntAssignment(
        ont_unit_id=legacy_ont.id,
        subscriber_id=subscription.subscriber_id,
        subscription_id=None,
        active=True,
    )
    db_session.add(legacy)
    db_session.commit()

    preview = preview_assignment_identity_repair(
        db_session,
        "canonicalize",
        plant["primary"].id,
        target_subscription_id=subscription.id,
        target_pon_port_id=plant["target_pon"].id,
        target_olt_id=plant["target_olt"].id,
        duplicate_assignment_ids=[],
    )

    assert preview.duplicate_assignment_ids == ()
    assert str(legacy.id) not in json.dumps(preview.input_snapshot)
    assert legacy.active is True


def test_reviewed_deactivation_records_exact_result(db_session, subscription):
    plant = _identity_plant(db_session, subscription)
    decision = propose_assignment_identity_repair(
        db_session,
        "deactivate",
        plant["primary"].id,
        proposed_by="planner@example.com",
        reason="Assignment is confirmed orphaned",
    )
    approve_assignment_identity_repair(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Orphan state independently verified",
    )
    applied = execute_assignment_identity_repair(
        db_session, decision.id, executed_by="executor@example.com"
    )
    db_session.refresh(plant["primary"])

    assert applied.status == "applied"
    assert plant["primary"].active is False
    assert plant["primary"].release_reason == "identity_repair_deactivated"


def test_public_assignment_mutations_are_retired(db_session):
    create_payload = OntAssignmentCreate(ont_unit_id=uuid.uuid4())
    update_payload = OntAssignmentUpdate(active=False)

    with pytest.raises(HTTPException) as created:
        domains_network_access.create_ont_assignment(create_payload, db_session)
    with pytest.raises(HTTPException) as updated:
        domains_network_access.update_ont_assignment(
            str(uuid.uuid4()), update_payload, db_session
        )
    with pytest.raises(HTTPException) as deleted:
        domains_network_access.delete_ont_assignment(str(uuid.uuid4()), db_session)

    assert created.value.status_code == 410
    assert updated.value.status_code == 410
    assert deleted.value.status_code == 410
    assert "reviewed" in created.value.detail


def test_admin_candidate_detection_observes_without_proposing(db_session, subscription):
    plant = _identity_plant(db_session, subscription)

    candidates = list_assignment_identity_candidates(
        db_session, query=str(plant["primary"].id)
    )

    assert len(candidates) == 1
    assert candidates[0].assignment_id == str(plant["primary"].id)
    assert "missing_subscription" in candidates[0].reasons
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_admin_preview_derives_olt_and_enumerates_exact_conflicts(
    db_session, subscription
):
    plant = _identity_plant(db_session, subscription)

    preview = preview_from_explicit_form(
        db_session,
        action="canonicalize",
        primary_assignment_id=plant["primary"].id,
        target_subscription_id=subscription.id,
        target_pon_port_id=plant["target_pon"].id,
    )

    assert preview.target_olt_id == plant["target_olt"].id
    assert preview.target_subscriber_id == subscription.subscriber_id
    assert preview.duplicate_assignment_ids == (plant["duplicate"].id,)
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_admin_proposal_rejects_stale_preview(db_session, subscription):
    plant = _identity_plant(db_session, subscription)
    preview = preview_from_explicit_form(
        db_session,
        action="canonicalize",
        primary_assignment_id=plant["primary"].id,
        target_subscription_id=subscription.id,
        target_pon_port_id=plant["target_pon"].id,
    )
    plant["primary"].subscriber_id = subscription.subscriber_id
    db_session.commit()

    with pytest.raises(OntAssignmentIdentityError, match="changed after preview"):
        propose_from_explicit_preview(
            db_session,
            action="canonicalize",
            primary_assignment_id=plant["primary"].id,
            target_subscription_id=subscription.id,
            target_pon_port_id=plant["target_pon"].id,
            expected_input_sha256=preview.input_sha256,
            proposed_by="system_user:planner",
            reason="Exact identifiers verified in the admin review queue",
        )
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_admin_review_routes_are_registered():
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in domains_network_access.router.routes
    }
    assert routes

    from app.web.admin import network_fiber_plant

    web_routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in network_fiber_plant.router.routes
    }
    assert ("/network/ont-identity-reviews", ("GET",)) in web_routes
    assert ("/network/ont-identity-reviews/preview", ("POST",)) in web_routes
    assert ("/network/ont-identity-reviews/propose", ("POST",)) in web_routes
    assert (
        "/network/ont-identity-reviews/{decision_id}/approve",
        ("POST",),
    ) in web_routes
    assert (
        "/network/ont-identity-reviews/{decision_id}/execute",
        ("POST",),
    ) in web_routes
