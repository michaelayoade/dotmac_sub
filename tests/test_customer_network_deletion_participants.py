"""Fast typed-contract tests; mocks do not claim PostgreSQL locking evidence."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.network import CPEDevice, DeviceStatus, IPAssignment
from app.services.audit_adapter import AuditActor
from app.services.cpe_subscriber_retirement import (
    RetireSubscriberCPECommand,
    SubscriberCPERetirementError,
    stage_subscriber_cpe_retirement,
)
from app.services.ip_assignment_lifecycle import (
    IPv4AssignmentLifecycleError,
    ReleaseSubscriberIPAssignmentsCommand,
    stage_subscriber_ip_assignment_release,
)
from app.services.owner_commands import CommandContext, OwnerCommandDefinition

DEFINITION = OwnerCommandDefinition(
    owner="customer.account_deletion",
    concern="customer account hard deletion",
    name="delete_customer_account",
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="user:administrator",
        scope="customers:delete",
        reason="requested deletion",
    )


@pytest.mark.parametrize("participant", ["ip", "cpe"])
@pytest.mark.parametrize(
    "mismatch",
    ["absent", "owner", "concern", "name", "context", "actor", "subscriber", "session"],
)
def test_exact_admission_refuses_without_read_or_flush(
    participant: str, mismatch: str
) -> None:
    db = MagicMock(spec=Session)
    context = _context()
    db.info = {
        "_dotmac_active_owner_command": DEFINITION,
        "_dotmac_active_owner_command_context": context,
    }
    actor = AuditActor.user("administrator")
    subscriber_id = uuid4()
    if mismatch in {"absent", "session"}:
        db.info = {}
    elif mismatch in {"owner", "concern", "name"}:
        db.info["_dotmac_active_owner_command"] = replace(
            DEFINITION, **{mismatch: "other"}
        )
    elif mismatch == "context":
        db.info["_dotmac_active_owner_command_context"] = _context()
    elif mismatch == "actor":
        actor = AuditActor.user("different")
    elif mismatch == "subscriber":
        subscriber_id = str(subscriber_id)
    with pytest.raises((IPv4AssignmentLifecycleError, SubscriberCPERetirementError)):
        if participant == "ip":
            stage_subscriber_ip_assignment_release(
                db,
                command=ReleaseSubscriberIPAssignmentsCommand(
                    context, subscriber_id, actor
                ),
            )
        else:
            stage_subscriber_cpe_retirement(
                db, command=RetireSubscriberCPECommand(context, subscriber_id, actor)
            )
    db.scalars.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.parametrize("participant", ["ip", "cpe"])
def test_participant_exact_sorted_nowait_refresh_and_no_transaction_completion(
    participant: str,
) -> None:
    db = MagicMock(spec=Session)
    context = _context()
    subscriber_id = uuid4()
    actor = AuditActor.user("administrator")
    db.info = {
        "_dotmac_active_owner_command": DEFINITION,
        "_dotmac_active_owner_command_context": context,
    }
    if participant == "ip":
        active = IPAssignment(id=uuid4(), subscriber_id=subscriber_id, is_active=True)
        history_time = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
        historical = IPAssignment(
            id=uuid4(),
            subscriber_id=subscriber_id,
            is_active=False,
            created_at=history_time,
            updated_at=history_time,
        )
        db.scalars.return_value = [active, historical]
        result = stage_subscriber_ip_assignment_release(
            db,
            command=ReleaseSubscriberIPAssignmentsCommand(
                context, subscriber_id, actor
            ),
        )
        assert result.released_assignment_ids == (active.id,)
        assert active.is_active is False
        assert historical.is_active is False
        assert historical.subscriber_id == subscriber_id
        assert historical.created_at == history_time
        assert historical.updated_at == history_time
        table = "ip_assignments"
    else:
        active = CPEDevice(
            id=uuid4(), subscriber_id=subscriber_id, status=DeviceStatus.active
        )
        historical = CPEDevice(
            id=uuid4(), subscriber_id=subscriber_id, status=DeviceStatus.inactive
        )
        db.scalars.return_value = [active, historical]
        result = stage_subscriber_cpe_retirement(
            db, command=RetireSubscriberCPECommand(context, subscriber_id, actor)
        )
        assert result.retired_cpe_ids == (active.id,)
        assert active.status is DeviceStatus.retired
        assert historical.status is DeviceStatus.inactive
        table = "cpe_devices"
    statement = db.scalars.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert f"WHERE {table}.subscriber_id =" in sql
    assert f"ORDER BY {table}.id FOR UPDATE NOWAIT" in sql
    assert statement.get_execution_options()["populate_existing"] is True
    db.no_autoflush.__enter__.assert_called_once()
    db.flush.assert_called_once()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.begin.assert_not_called()
    db.begin_nested.assert_not_called()


@pytest.mark.parametrize("participant", ["ip", "cpe"])
def test_empty_history_has_typed_empty_outcome(participant: str) -> None:
    db = MagicMock(spec=Session)
    context = _context()
    db.info = {
        "_dotmac_active_owner_command": DEFINITION,
        "_dotmac_active_owner_command_context": context,
    }
    db.scalars.return_value = []
    if participant == "ip":
        result = stage_subscriber_ip_assignment_release(
            db,
            command=ReleaseSubscriberIPAssignmentsCommand(
                context, uuid4(), AuditActor.user("administrator")
            ),
        )
        assert result.released_assignment_ids == ()
    else:
        result = stage_subscriber_cpe_retirement(
            db,
            command=RetireSubscriberCPECommand(
                context, uuid4(), AuditActor.user("administrator")
            ),
        )
        assert result.retired_cpe_ids == ()
