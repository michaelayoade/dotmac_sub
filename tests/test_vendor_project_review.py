"""Staff verification and rework stay inside the vendor lifecycle owner."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.project import Project
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
    Vendor,
)
from app.models.work_order import WorkOrder
from app.services import vendor_project_review_proposals
from app.services.vendor_portal_errors import VendorProjectLifecycleError
from app.services.vendor_portal_operations import vendor_portal_operations


def _completed(
    db_session,
    *,
    evidence_required: bool = False,
    create_work_order: bool = True,
):
    subscriber = Subscriber(
        first_name="Vendor",
        last_name="Customer",
        email=f"vendor-review-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    project = Project(
        name="Completed vendor installation",
        subscriber_id=subscriber.id,
    )
    vendor = Vendor(name="Review Vendor", code=f"RV-{uuid4().hex[:8]}")
    reviewer = SystemUser(
        first_name="Field",
        last_name="Reviewer",
        email=f"reviewer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, reviewer])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
        status=InstallationProjectStatus.completed.value,
    )
    db_session.add(installation)
    if create_work_order:
        db_session.add(
            WorkOrder(
                subscriber_id=subscriber.id,
                project_id=project.id,
                title="Vendor installation",
                requires_as_built_evidence=evidence_required,
            )
        )
    db_session.commit()
    return installation, vendor, reviewer


def test_staff_verification_records_actor_time_reason_and_event(db_session):
    installation, vendor, reviewer = _completed(db_session)

    result = vendor_portal_operations.transition_staff_project(
        db_session,
        str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
        reason="Route and workmanship accepted",
    )

    db_session.refresh(installation)
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    outbox = db_session.query(EventStore).one()
    assert installation.status == InstallationProjectStatus.verified.value
    assert result["status"] == InstallationProjectStatus.verified.value
    assert evidence.vendor_id == vendor.id
    assert evidence.actor_type == "staff_user"
    assert evidence.actor_id == str(reviewer.id)
    assert evidence.reason == "Route and workmanship accepted"
    policy = evidence.decision_context["verification_evidence"]
    assert policy["required"] is False
    assert policy["eligible"] is True
    assert policy["source"] == "work_order"
    assert policy["latest_as_built"] is None
    assert evidence.event_type == "vendor_project.verified"
    assert evidence.occurred_at is not None
    assert outbox.event_id == evidence.event_id
    assert outbox.payload["reason"] == "Route and workmanship accepted"
    assert outbox.payload["verification_evidence"]["required"] is False


def test_required_policy_blocks_until_latest_as_built_is_accepted(db_session):
    installation, _vendor, reviewer = _completed(db_session, evidence_required=True)

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_portal_operations.preview_staff_project_lifecycle(
            db_session,
            str(installation.id),
            action="verify",
        )
    assert exc.value.code == "as_built_evidence_required"

    accepted = AsBuiltRoute(
        project_id=installation.id,
        status=AsBuiltRouteStatus.accepted.value,
        version=1,
    )
    db_session.add(accepted)
    db_session.commit()

    result = vendor_portal_operations.transition_staff_project(
        db_session,
        str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
    )

    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    policy = evidence.decision_context["verification_evidence"]
    assert result["status"] == InstallationProjectStatus.verified.value
    assert policy["required"] is True
    assert policy["latest_as_built"]["id"] == str(accepted.id)
    assert policy["latest_as_built"]["status"] == AsBuiltRouteStatus.accepted.value


def test_newer_unaccepted_as_built_supersedes_older_acceptance(db_session):
    installation, _vendor, _reviewer = _completed(db_session, evidence_required=True)
    db_session.add_all(
        [
            AsBuiltRoute(
                project_id=installation.id,
                status=AsBuiltRouteStatus.accepted.value,
                version=1,
            ),
            AsBuiltRoute(
                project_id=installation.id,
                status=AsBuiltRouteStatus.submitted.value,
                version=2,
            ),
        ]
    )
    db_session.commit()

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_portal_operations.preview_staff_project_lifecycle(
            db_session,
            str(installation.id),
            action="verify",
        )

    assert exc.value.code == "as_built_evidence_required"
    assert "currently submitted" in exc.value.message


def test_missing_work_order_uses_default_enabled_policy(db_session):
    installation, _vendor, _reviewer = _completed(db_session, create_work_order=False)

    projected = vendor_portal_operations.list_reviewable_projects(db_session)[0]

    assert projected["verification_evidence"]["source"] == "default_enabled"
    assert projected["verification_evidence"]["required"] is True
    assert projected["verify_action"].allowed is False


def test_rework_requires_reason_and_returns_project_to_vendor(db_session):
    installation, _vendor, reviewer = _completed(db_session)

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_portal_operations.transition_staff_project(
            db_session,
            str(installation.id),
            action="rework",
            actor_id=str(reviewer.id),
        )
    assert exc.value.code == "reason_required"

    result = vendor_portal_operations.transition_staff_project(
        db_session,
        str(installation.id),
        action="rework",
        actor_id=str(reviewer.id),
        reason="Replace the unsupported span",
    )
    assert result["status"] == InstallationProjectStatus.in_progress.value
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    assert evidence.event_type == "vendor_project.rework_requested"
    assert evidence.reason == "Replace the unsupported span"


def test_staff_review_confirmation_is_stale_safe_and_exact_replay(db_session):
    installation, _vendor, reviewer = _completed(db_session)
    proposal = vendor_project_review_proposals.issue_review(
        db_session,
        project_id=str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
        reason="Completion accepted",
    )

    first = vendor_project_review_proposals.confirm_review(
        db_session,
        confirmation_token=proposal.confirmation_token,
        project_id=str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
    )
    replay = vendor_project_review_proposals.confirm_review(
        db_session,
        confirmation_token=proposal.confirmation_token,
        project_id=str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.lifecycle_event_id == first.lifecycle_event_id
    assert db_session.query(InstallationProjectLifecycleEvent).count() == 1
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == "vendor_project_verify")
        .count()
        == 1
    )


def test_staff_queue_projects_owner_actions_with_granular_permission(db_session):
    installation, _vendor, _reviewer = _completed(db_session)
    project = vendor_portal_operations.list_reviewable_projects(db_session)[0]

    assert str(project["id"]) == str(installation.id)
    assert project["verify_action"].permission == "inventory:write"
    assert project["verify_action"].requires_confirmation is True
    assert project["rework_action"].permission == "inventory:write"


def test_staff_review_routes_and_templates_are_thin_adapters():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "app/web/admin/vendor_operations.py").read_text(encoding="utf-8")
    queue = (root / "templates/admin/vendors/operations.html").read_text(
        encoding="utf-8"
    )
    confirm = (root / "templates/admin/vendors/project_review_confirm.html").read_text(
        encoding="utf-8"
    )
    vendor = (root / "templates/vendor/project_detail.html").read_text(encoding="utf-8")
    sot = (root / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")
    migration = (
        root / "alembic/versions/373_vendor_lifecycle_review_evidence.py"
    ).read_text(encoding="utf-8")
    policy_migration = (
        root / "alembic/versions/375_work_order_evidence_policy.py"
    ).read_text(encoding="utf-8")

    assert "vendor_project_review_proposals.issue_review(" in routes
    assert "vendor_project_review_proposals.confirm_review(" in routes
    assert "transition_staff_project(" not in routes
    assert "action_permitted(request, project.verify_action)" in queue
    assert "proposal.confirmation_token" in confirm
    assert "project.lifecycle_events" in vendor
    assert "operations.vendor_project_review_confirmation" in sot
    assert "vendor_project.verified" in sot
    assert "vendor_project.rework_requested" in sot
    assert 'sa.Column("reason", sa.Text(), nullable=True)' in migration
    assert '"requires_as_built_evidence"' in policy_migration
    assert '"decision_context"' in policy_migration
    assert "vendor-supplied `work_order_ref` remains observational" in sot
