"""Fast unit evidence for opt-in selection; not migrated-schema acceptance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderClassificationRequest, WorkOrderHeaderRead
from app.services.owner_commands import CommandContext
from app.services.work_order_classification_contracts import ClassifyWorkOrderCommand
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    TemplateErrorCode,
    TemplateRequirementUse,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateError,
    WorkOrderTemplateQuery,
    WorkOrderTemplateRecommendation,
    recommend_template,
)


def _seed(
    db: Session, *, kind: str = "customer", status: str = "draft"
) -> tuple[UUID, datetime]:
    subscriber = Subscriber(
        first_name="Scope", last_name="Test", email=f"scope-{uuid4().hex}@example.com"
    )
    db.add(subscriber)
    db.flush()
    revision = datetime.now(UTC) - timedelta(minutes=1)
    row = WorkOrder(
        title="Explicit field work",
        public_id=f"sub-test-{uuid4().hex}",
        subscriber_id=subscriber.id if kind == "customer" else None,
        work_order_kind=kind,
        status=status,
        work_type="install",
        updated_at=revision,
        required_skills=[],
        tags=[],
    )
    db.add(row)
    db.flush()
    identity = row.id
    db.commit()
    return identity, revision


def _command(
    identity: UUID,
    revision: datetime,
    *,
    purpose: WorkOrderPurpose = WorkOrderPurpose.CUSTOMER_DEPLOYMENT,
) -> ClassifyWorkOrderCommand:
    return ClassifyWorkOrderCommand(
        context=CommandContext.system(
            actor="classification-unit-test",
            scope="operations:dispatch:write",
            reason="Supervisor explicitly selects the planned field scope",
            idempotency_key=str(uuid4()),
        ),
        work_order_id=identity,
        expected_revision=revision,
        classification=ExplicitWorkOrderClassification(
            purpose=purpose,
            access_technology=WorkOrderAccessTechnology.FIBER,
        ),
    )


def test_explicit_selection_is_persisted_without_execution_or_legacy_inference(
    db_session: Session,
) -> None:
    identity, revision = _seed(db_session)
    command = _command(identity, revision, purpose=WorkOrderPurpose.REPAIR)
    outcome = work_order_commands.classify(db_session, command=command)
    assert outcome.recommendation.use == TemplateRequirementUse.RECOMMENDATION_ONLY
    assert not outcome.replayed
    row = db_session.get(WorkOrder, identity)
    assert row is not None
    assert row.purpose == "repair"
    assert row.access_technology == "fiber"
    assert row.template_version == 1
    assert row.classification_source == "explicit_operator_selection"
    assert row.status == "draft"
    assert row.started_at is None and row.completed_at is None
    assert row.work_type == "install"
    header = WorkOrderHeaderRead.model_validate(row)
    assert header.purpose == WorkOrderPurpose.REPAIR
    assert header.access_technology == WorkOrderAccessTechnology.FIBER
    audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.classified")
        .all()
    )
    assert len(audits) == 1
    assert audits[0].metadata_["reason"] == command.context.reason
    events = db_session.query(EventStore).all()
    assert [event.event_type for event in events] == ["work_order.classified"]
    assert events[0].payload["schema_version"] == 1
    assert events[0].payload["purpose"] == "repair"
    db_session.commit()
    replay = work_order_commands.classify(db_session, command=command)
    assert replay.replayed
    assert replay.revision == outcome.revision
    assert replay.recommendation == outcome.recommendation
    assert db_session.query(EventStore).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.classified")
        .count()
        == 1
    )


def test_replay_rejects_catalog_version_drift_without_rewriting_history(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, revision = _seed(db_session)
    command = _command(identity, revision)
    original = work_order_commands.classify(db_session, command=command)

    def next_catalog_version(
        *, query: WorkOrderTemplateQuery
    ) -> WorkOrderTemplateRecommendation:
        recommendation = recommend_template(query=query)
        return replace(
            recommendation,
            identity=replace(
                recommendation.identity, version=recommendation.identity.version + 1
            ),
        )

    monkeypatch.setattr(
        import_module("app.services.work_order_commands"),
        "recommend_template",
        next_catalog_version,
    )
    with pytest.raises(WorkOrderCommandError) as rejected:
        work_order_commands.classify(db_session, command=command)
    assert rejected.value.code == "classification_receipt_invalid"
    row = db_session.get(WorkOrder, identity)
    assert row is not None
    assert row.template_version == original.recommendation.identity.version
    assert row.updated_at.replace(tzinfo=UTC) == original.revision
    assert db_session.query(EventStore).count() == 1
    receipts = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.classified")
        .all()
    )
    assert len(receipts) == 1
    assert receipts[0].metadata_["result"]["template_version"] == row.template_version


def test_same_identity_with_changed_selection_conflicts(db_session: Session) -> None:
    identity, revision = _seed(db_session)
    command = _command(identity, revision)
    work_order_commands.classify(db_session, command=command)
    changed = replace(
        command,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.CUSTOMER_DEPLOYMENT,
            access_technology=WorkOrderAccessTechnology.RADIO,
        ),
    )
    with pytest.raises(WorkOrderCommandError) as rejected:
        work_order_commands.classify(db_session, command=changed)
    assert rejected.value.code == "classification_idempotency_conflict"
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.access_technology == "fiber"


def test_stale_revision_fails_without_persisting_classification(
    db_session: Session,
) -> None:
    identity, revision = _seed(db_session)
    with pytest.raises(WorkOrderCommandError) as rejected:
        work_order_commands.classify(
            db_session, command=_command(identity, revision - timedelta(seconds=1))
        )
    assert rejected.value.code == "stale_classification"
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.purpose is None and row.template_version is None


@pytest.mark.parametrize(
    "status", ("dispatched", "in_progress", "paused", "completed", "canceled")
)
def test_classification_cannot_change_execution_scope(
    db_session: Session, status: str
) -> None:
    identity, revision = _seed(db_session, status=status)
    with pytest.raises(WorkOrderCommandError) as rejected:
        work_order_commands.classify(db_session, command=_command(identity, revision))
    assert rejected.value.code == "work_order_not_classifiable"
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.purpose is None and row.status == status


def test_shared_infrastructure_rollout_is_explicit_and_customer_deployment_rejected(
    db_session: Session,
) -> None:
    identity, revision = _seed(db_session, kind="infrastructure", status="scheduled")
    with pytest.raises(WorkOrderTemplateError) as rejected:
        work_order_commands.classify(db_session, command=_command(identity, revision))
    assert rejected.value.code == TemplateErrorCode.INCOMPATIBLE_KIND
    outcome = work_order_commands.classify(
        db_session,
        command=_command(
            identity, revision, purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT
        ),
    )
    assert outcome.recommendation.identity.kind.value == "infrastructure"
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.subscriber_id is None
    assert row.purpose == "infrastructure_rollout" and row.status == "scheduled"


def test_request_rejects_freeform_purpose_and_exposes_precise_enums() -> None:
    with pytest.raises(ValidationError):
        WorkOrderClassificationRequest.model_validate(
            {
                "command_id": str(uuid4()),
                "expected_revision": datetime.now(UTC).isoformat(),
                "reason": "Plan field work",
                "classification": {"purpose": "install", "access_technology": "fiber"},
            }
        )
    request = WorkOrderClassificationRequest.model_validate(
        {
            "command_id": str(uuid4()),
            "expected_revision": datetime.now(UTC).isoformat(),
            "reason": "Plan field work",
            "classification": {
                "purpose": "customer_deployment",
                "access_technology": "radio",
            },
        }
    )
    assert request.classification.purpose == WorkOrderPurpose.CUSTOMER_DEPLOYMENT
    assert request.classification.access_technology == WorkOrderAccessTechnology.RADIO
