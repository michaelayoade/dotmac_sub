"""Owner resolution of reconciliation_needed dispatches and head lifecycle sync."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.network import OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_service_configuration import (
    OntServiceConfigurationHead,
    OntServiceConfigurationPhase,
    OntServiceConfigurationRevision,
)
from app.services import network_operation_dispatch as dispatch_owner
from app.services.network_operation_dispatch import (
    DispatchResolution,
    NetworkOperationCommand,
    NetworkOperationDispatchError,
    ResolveDispatchCommand,
)
from app.services.network_operations import network_operations
from app.services.web_network_operations import operation_dispatch_summary

NEEDS = NetworkOperationDispatchStatus.reconciliation_needed.value


def _needs_review(db_session):
    ont = OntUnit(serial_number="RESOLVE-ONT", is_active=True)
    db_session.add(ont)
    db_session.flush()
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_ont_sync,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_status_refresh:{ont.id}",
        input_payload={"action": "status_refresh"},
        initiated_by="Network Admin",
    )
    dispatch = dispatch_owner.stage_dispatch(
        db_session, operation, NetworkOperationCommand.ont_status_refresh_v1
    )
    db_session.commit()
    current = datetime.now(UTC)
    assert dispatch_owner.claim_dispatch_execution(
        db_session, str(dispatch.id), now=current
    )
    db_session.commit()
    dispatch_owner.reconcile_dispatches(
        db_session,
        now=current + dispatch_owner.DEFAULT_EXECUTION_TIMEOUT + timedelta(seconds=1),
    )
    db_session.refresh(dispatch)
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed
    return ont, operation, dispatch


def _resolve(db_session, command, **kwargs):
    """Owner commands need a transaction-free session at entry."""
    db_session.commit()
    return dispatch_owner.resolve_reconciliation_needed_dispatch(
        db_session, command, **kwargs
    )


def _command(dispatch, **overrides):
    values = {
        "dispatch_id": str(dispatch.id),
        "resolution": DispatchResolution.confirmed_landed,
        "evidence": "ACS readback matches desired values on 2026-10-24",
        "actor": "user-1234",
        "expected_status": NEEDS,
    }
    values.update(overrides)
    return ResolveDispatchCommand(**values)


def test_resolve_happy_path_records_verdict_and_leaves_operation_alone(db_session):
    _, operation, dispatch = _needs_review(db_session)
    db_session.refresh(operation)
    before = (operation.status, operation.error, operation.output_payload)

    result = _resolve(db_session, _command(dispatch))

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    assert result.id == dispatch.id
    assert dispatch.status == NetworkOperationDispatchStatus.resolved
    assert dispatch.resolution == "confirmed_landed"
    assert dispatch.resolution_evidence.startswith("ACS readback")
    assert dispatch.resolved_by == "user-1234"
    assert dispatch.resolved_at is not None
    assert (operation.status, operation.error, operation.output_payload) == before
    summary = operation_dispatch_summary(operation)
    assert summary["status"] == "resolved"
    assert summary["attention_required"] is False
    snapshot = dispatch_owner.health_snapshot(db_session)
    assert snapshot["dispatches_resolved"] == 1
    assert snapshot["dispatches_reconciliation_needed"] == 0


@pytest.mark.parametrize("resolution", list(DispatchResolution))
def test_resolve_accepts_each_resolution(db_session, resolution):
    _, _, dispatch = _needs_review(db_session)
    _resolve(db_session, _command(dispatch, resolution=resolution))
    db_session.refresh(dispatch)
    assert dispatch.resolution == resolution.value


@pytest.mark.parametrize(
    "status",
    [
        NetworkOperationDispatchStatus.pending,
        NetworkOperationDispatchStatus.acknowledged,
        NetworkOperationDispatchStatus.completed,
        NetworkOperationDispatchStatus.failed,
        NetworkOperationDispatchStatus.resolved,
    ],
)
def test_resolve_refused_on_wrong_status(db_session, status):
    _, _, dispatch = _needs_review(db_session)
    dispatch.status = status
    db_session.commit()

    with pytest.raises(NetworkOperationDispatchError) as excinfo:
        _resolve(db_session, _command(dispatch))

    assert excinfo.value.code == "dispatch_status_conflict"
    db_session.refresh(dispatch)
    assert dispatch.status == status
    assert dispatch.resolution is None


def test_resolve_refuses_a_second_resolution(db_session):
    _, _, dispatch = _needs_review(db_session)
    _resolve(db_session, _command(dispatch))
    with pytest.raises(NetworkOperationDispatchError) as excinfo:
        _resolve(
            db_session, _command(dispatch, resolution=DispatchResolution.superseded)
        )
    assert excinfo.value.code == "dispatch_status_conflict"
    db_session.refresh(dispatch)
    assert dispatch.resolution == "confirmed_landed"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expected_status": "completed"}, "invalid_expected_status"),
        ({"evidence": "  short "}, "invalid_evidence"),
        ({"evidence": "x" * 2001}, "invalid_evidence"),
        ({"actor": "  "}, "actor_required"),
        ({"resolution": "deleted"}, "invalid_resolution"),
        ({"dispatch_id": "not-a-uuid"}, "dispatch_not_found"),
    ],
)
def test_resolve_validates_inputs_without_changing_the_row(db_session, overrides, code):
    _, _, dispatch = _needs_review(db_session)
    with pytest.raises(NetworkOperationDispatchError) as excinfo:
        _resolve(db_session, _command(dispatch, **overrides))
    assert excinfo.value.code == code
    db_session.refresh(dispatch)
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed


def test_resolve_refused_while_operation_is_active(db_session):
    _, operation, dispatch = _needs_review(db_session)
    operation.status = NetworkOperationStatus.running
    db_session.commit()
    with pytest.raises(NetworkOperationDispatchError) as excinfo:
        _resolve(db_session, _command(dispatch))
    assert excinfo.value.code == "operation_still_active"


def test_resolve_is_recorded_in_audit(db_session):
    _, _, dispatch = _needs_review(db_session)
    _resolve(db_session, _command(dispatch, resolution=DispatchResolution.superseded))

    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "network_operation_dispatch.resolve"
        )
    ).all()
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "network_operation_dispatch"
    assert event.entity_id == str(dispatch.id)
    assert event.actor_id == "user-1234"
    assert event.metadata_["resolution"] == "superseded"
    assert event.metadata_["previous_status"] == NEEDS
    assert event.metadata_["evidence"].startswith("ACS readback")


def test_refused_resolution_writes_no_audit(db_session):
    _, _, dispatch = _needs_review(db_session)
    with pytest.raises(NetworkOperationDispatchError):
        _resolve(db_session, _command(dispatch, evidence="short"))
    assert (
        db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "network_operation_dispatch.resolve"
            )
        ).first()
        is None
    )


def test_list_returns_only_reconciliation_needed(db_session):
    _, _, dispatch = _needs_review(db_session)
    assert [
        d.id for d in dispatch_owner.list_reconciliation_needed_dispatches(db_session)
    ] == [dispatch.id]
    _resolve(db_session, _command(dispatch))
    assert dispatch_owner.list_reconciliation_needed_dispatches(db_session) == []


# ── lifecycle sync ──────────────────────────────────────────────────────────


def _config_dispatch(db_session):
    ont = OntUnit(serial_number="SYNC-ONT", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.pending,
        correlation_key=f"sync-test:{ont.id}",
        input_payload={},
        initiated_by="test",
    )
    db_session.add(operation)
    db_session.flush()
    head = OntServiceConfigurationHead(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        current_revision=1,
        latest_operation_id=operation.id,
        phase=OntServiceConfigurationPhase.queued,
        waiting_reason="awaiting_dispatch",
    )
    db_session.add(head)
    db_session.flush()
    revision = OntServiceConfigurationRevision(
        head_id=head.id,
        assignment_id=assignment.id,
        revision=1,
        section="wan",
        command_fingerprint="a" * 64,
        idempotency_key="sync-key",
        desired_change_evidence={},
        operation_id=operation.id,
        phase=OntServiceConfigurationPhase.queued,
    )
    db_session.add(revision)
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": 1,
    }
    db_session.flush()
    dispatch = dispatch_owner.stage_dispatch(
        db_session, operation, NetworkOperationCommand.ont_service_config_apply_v1
    )
    db_session.commit()
    return head, revision, operation, dispatch


def test_stale_ack_moves_queued_head_to_failed_interrupted(db_session):
    head, revision, operation, dispatch = _config_dispatch(db_session)
    current = datetime.now(UTC)
    assert dispatch_owner.claim_dispatch_execution(
        db_session, str(dispatch.id), now=current
    )
    db_session.commit()

    dispatch_owner.reconcile_dispatches(
        db_session,
        now=current + dispatch_owner.DEFAULT_EXECUTION_TIMEOUT + timedelta(seconds=1),
    )

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    db_session.refresh(head)
    db_session.refresh(revision)
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed
    assert operation.status == NetworkOperationStatus.failed
    assert head.phase is OntServiceConfigurationPhase.failed
    assert head.failure_code == "operation_interrupted"
    assert head.waiting_reason is None
    assert revision.phase is OntServiceConfigurationPhase.failed
    assert revision.failure_code == "operation_interrupted"


def test_worker_failure_after_terminal_operation_still_syncs_head(db_session):
    """The 409 'already failed' case: operation terminal, head left queued."""
    head, _, operation, dispatch = _config_dispatch(db_session)
    current = datetime.now(UTC)
    assert dispatch_owner.claim_dispatch_execution(
        db_session, str(dispatch.id), now=current
    )
    network_operations.mark_failed(db_session, str(operation.id), "known rejection")
    db_session.commit()

    dispatch_owner.fail_dispatch_execution(db_session, str(dispatch.id), "boom")
    db_session.commit()

    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.failed
    assert head.failure_code == "operation_interrupted"


def test_publish_exhaustion_syncs_head(db_session):
    head, _, operation, dispatch = _config_dispatch(db_session)
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(operation)
    db_session.refresh(head)
    assert operation.status == NetworkOperationStatus.failed
    assert head.phase is OntServiceConfigurationPhase.failed


def test_sync_leaves_a_head_that_already_reached_a_verdict(db_session):
    head, _, operation, dispatch = _config_dispatch(db_session)
    head.phase = OntServiceConfigurationPhase.verified
    db_session.commit()
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.verified
    assert head.failure_code is None


def test_sync_ignores_head_that_moved_to_a_newer_operation(db_session):
    head, _, operation, dispatch = _config_dispatch(db_session)
    newer = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=operation.target_id,
        status=NetworkOperationStatus.running,
        correlation_key="sync-newer",
        input_payload={},
        initiated_by="test",
    )
    db_session.add(newer)
    db_session.flush()
    head.latest_operation_id = newer.id
    db_session.commit()
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.queued


def test_sync_leaves_head_untouched_when_current_revision_is_missing(db_session):
    """Head and revision move together or not at all."""
    head, revision, operation, dispatch = _config_dispatch(db_session)
    db_session.delete(revision)
    db_session.commit()
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(head)
    db_session.refresh(operation)
    assert operation.status == NetworkOperationStatus.failed
    assert head.phase is OntServiceConfigurationPhase.queued
    assert head.failure_code is None


def test_sync_leaves_head_untouched_when_revision_belongs_to_another_operation(
    db_session,
):
    head, revision, operation, dispatch = _config_dispatch(db_session)
    other = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=operation.target_id,
        status=NetworkOperationStatus.succeeded,
        correlation_key="sync-other-revision",
        input_payload={},
        initiated_by="test",
    )
    db_session.add(other)
    db_session.flush()
    revision.operation_id = other.id
    db_session.commit()
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(head)
    db_session.refresh(revision)
    assert head.phase is OntServiceConfigurationPhase.queued
    assert revision.phase is OntServiceConfigurationPhase.queued


def test_non_configuration_operation_does_not_touch_any_head(db_session):
    _needs_review(db_session)  # an olt_ont_sync operation; must not raise
    assert db_session.scalars(select(OntServiceConfigurationHead)).first() is None


# ── migration ───────────────────────────────────────────────────────────────


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/699_dispatch_reconciliation_resolution.py"
    )
    spec = importlib.util.spec_from_file_location("migration_699_dispatch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(fn_name: str) -> str:
    migration = _migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(migration, fn_name)()
    return output.getvalue()


def test_migration_chains_from_current_head():
    migration = _migration()
    assert migration.revision == "699_dispatch_reconciliation_resolution"
    assert migration.down_revision == "698_credential_deactivation_reason"


def test_migration_up_adds_enum_value_and_columns_idempotently():
    sql = _sql("upgrade")
    assert (
        "ALTER TYPE networkoperationdispatchstatus ADD VALUE IF NOT EXISTS 'resolved'"
        in sql
    )
    for column in (
        "resolution VARCHAR(32)",
        "resolution_evidence TEXT",
        "resolved_by VARCHAR(160)",
        "resolved_at TIMESTAMP WITH TIME ZONE",
    ):
        assert (
            f"ALTER TABLE network_operation_dispatches ADD COLUMN IF NOT EXISTS {column}"
            in sql
        )
    assert "DROP" not in sql.upper()


def test_migration_down_refuses_to_destroy_evidence_then_drops_columns():
    sql = _sql("downgrade")
    assert "status::text = 'resolved'" in sql
    assert "RAISE EXCEPTION" in sql
    for column in ("resolved_at", "resolved_by", "resolution_evidence", "resolution"):
        assert (
            f"ALTER TABLE network_operation_dispatches DROP COLUMN IF EXISTS {column}"
            in sql
        )
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP COLUMN")


def test_model_columns_match_migration():
    columns = NetworkOperationDispatch.__table__.c
    for name in ("resolution", "resolution_evidence", "resolved_by", "resolved_at"):
        assert columns[name].nullable is True


def test_sync_leaves_head_alone_when_revision_is_missing_or_settled(db_session):
    head, revision, operation, dispatch = _config_dispatch(db_session)
    revision.phase = OntServiceConfigurationPhase.verified
    db_session.commit()
    dispatch_owner._mark_publish_exhausted(
        db_session, dispatch, now=datetime.now(UTC), unknown_delivery=True
    )
    db_session.commit()
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.queued
    assert head.failure_code is None
