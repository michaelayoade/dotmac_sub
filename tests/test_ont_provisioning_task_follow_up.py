from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.network import OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_provisioning_commands import (
    request_bootstrap_verification,
)
from app.services.network_operations import network_operations


def test_queue_post_authorization_bootstrap_follow_up_creates_child_operation(
    db_session,
):
    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_authorize,
        NetworkOperationTargetType.ont,
        str(uuid.uuid4()),
        correlation_key=f"ont_authorize:test:{uuid.uuid4()}",
        initiated_by="tester",
    )
    network_operations.mark_running(db_session, str(parent.id))

    ont = OntUnit(serial_number="BOOTSTRAP-CHILD-COMMAND")
    db_session.add(ont)
    db_session.flush()
    ont_id = str(ont.id)
    result = request_bootstrap_verification(
        db_session,
        ont_id=ont_id,
        parent_operation_id=str(parent.id),
        initiated_by="tester",
    )

    db_session.expire_all()
    child = db_session.scalars(
        select(NetworkOperation).where(
            NetworkOperation.correlation_key == f"tr069_bootstrap:{ont_id}"
        )
    ).one()
    dispatch = db_session.scalars(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == child.id
        )
    ).one()

    assert result.accepted is True
    assert result.operation_id == str(child.id)
    assert result.dispatch_id == str(dispatch.id)
    assert result.duplicate is False
    assert child.operation_type == NetworkOperationType.tr069_bootstrap
    assert child.target_type == NetworkOperationTargetType.ont
    assert str(child.target_id) == ont_id
    assert str(child.parent_id) == str(parent.id)
    assert child.initiated_by == "tester"
    assert dispatch.dispatch_key == "attempt:0"
    assert dispatch.command_name == "ont_bootstrap_verify.v1"
    assert dispatch.task_name == "app.tasks.tr069.wait_for_ont_bootstrap"
    assert dispatch.args_payload == [ont_id, str(child.id), 0]
    assert dispatch.status == NetworkOperationDispatchStatus.pending
