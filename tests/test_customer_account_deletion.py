"""Fast, non-authoritative customer deletion contract and adapter checks."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.adapters import customer_account_deletion as adapter
from app.services import customer_account_deletion as owner
from app.services import web_customer_actions as web
from app.services.api_key_administration import (
    CustomerKeyDeletionCommand,
    stage_customer_key_deletion,
)
from app.services.audit_adapter import AuditActor
from app.services.credential_administration_mutation import (
    CustomerAuthDeletionCommand,
    stage_customer_auth_deletion,
)
from app.services.owner_commands import CommandContext

ACTOR = AuditActor.user("test-administrator")


def _context() -> CommandContext:
    return CommandContext.system(
        actor=f"user:{ACTOR.actor_id}", scope=owner.SCOPE, reason="Contract test"
    )


def test_owner_rejects_untrusted_actor_before_executor(db_session, monkeypatch):
    monkeypatch.setattr(
        owner,
        "execute_owner_command",
        lambda *a, **kw: pytest.fail("invalid command entered executor"),
    )
    command = owner.DeleteCustomerAccountCommand(
        context=_context(),
        actor=AuditActor.user("other"),
        subscriber_id=uuid4(),
        policy=owner.CustomerDeletionPolicy.JSON,
    )
    with pytest.raises(owner.CustomerAccountDeletionError, match="Trusted"):
        owner.delete_customer_account(db_session, command=command)


@pytest.mark.parametrize("participant", ["keys", "auth"])
def test_auth_participants_refuse_absent_owner(db_session, participant):
    if participant == "keys":
        action = stage_customer_key_deletion
        command = CustomerKeyDeletionCommand(
            context=_context(), actor=ACTOR, subscriber_id=uuid4(), locked_key_ids=()
        )
    else:
        action = stage_customer_auth_deletion
        command = CustomerAuthDeletionCommand(
            context=_context(), actor=ACTOR, subscriber_id=uuid4()
        )
    with pytest.raises(owner.CustomerAccountDeletionError, match="Exact"):
        action(db_session, command=command)


def test_bulk_fresh_sessions_continues_failure_and_reports_duplicate_unknown(
    monkeypatch,
):
    first, middle, last = (uuid4() for _ in range(3))
    sessions = []
    commands = []
    deleted = set()

    @contextmanager
    def fresh_session():
        marker = object()
        sessions.append(marker)
        yield marker

    def execute(db, *, command):
        assert db is sessions[-1]
        commands.append(command)
        if command.subscriber_id == middle:
            raise owner.CustomerAccountDeletionError(
                code=f"{owner.OWNER}.blocked",
                message="Cannot delete customer. Linked records exist.",
            )
        status = (
            owner.CustomerDeletionStatus.MISSING
            if command.subscriber_id in deleted
            else owner.CustomerDeletionStatus.DELETED
        )
        deleted.add(command.subscriber_id)
        return owner.CustomerAccountDeletionOutcome(command.subscriber_id, status)

    monkeypatch.setattr(
        adapter.db_session_adapter, "owner_command_session", fresh_session
    )
    monkeypatch.setattr(adapter, "delete_customer_account", execute)
    result = web.bulk_delete_customers(
        object(),
        [
            {"id": str(first), "type": "person"},
            {"id": str(middle), "type": "business"},
            {"id": str(first), "type": "subscriber"},
            {"id": str(last), "type": "unknown"},
            {"id": str(last), "type": "business"},
        ],
        actor=ACTOR,
        expectations=tuple(
            owner.CustomerDeletionExpectation(item, False, False)
            for item in (first, middle, last)
        ),
    )
    assert result["deleted_count"] == 2
    assert [row["id"] for row in result["skipped"]] == [
        str(middle),
        str(first),
        str(last),
    ]
    assert [row["reason"] for row in result["skipped"]] == [
        "Cannot delete customer. Linked records exist.",
        "Subscriber not found",
        "Unknown customer type",
    ]
    assert len(sessions) == len({id(db) for db in sessions}) == 4
    assert [command.subscriber_id for command in commands] == [
        first,
        middle,
        first,
        last,
    ]
    assert all(command.actor == ACTOR for command in commands)


def test_adapter_maps_busy_after_session_closed(monkeypatch):
    closed = False

    @contextmanager
    def fresh_session():
        nonlocal closed
        try:
            yield object()
        finally:
            closed = True

    def execute(*args, **kwargs):
        raise owner.CustomerAccountDeletionError(
            code=f"{owner.OWNER}.busy", message="Busy"
        )

    monkeypatch.setattr(
        adapter.db_session_adapter, "owner_command_session", fresh_session
    )
    monkeypatch.setattr(adapter, "delete_customer_account", execute)
    with pytest.raises(HTTPException) as caught:
        adapter.delete_customer_from_adapter(
            customer_id=str(uuid4()),
            actor=ACTOR,
            policy=owner.CustomerDeletionPolicy.JSON,
        )
    assert closed
    assert caught.value.status_code == 503
    assert caught.value.headers == {"Retry-After": "5"}


@pytest.mark.parametrize("mutation", ["name", "concern", "context", "actor", "scope"])
def test_participant_requires_exact_definition_and_context(
    db_session, monkeypatch, mutation
):
    import app.services.owner_commands as boundary

    context = _context()
    definition = owner.CUSTOMER_ACCOUNT_DELETION_DEFINITION
    if mutation == "name":
        definition = boundary.OwnerCommandDefinition(
            owner=owner.OWNER, concern=owner.CONCERN, name="other_command"
        )
    if mutation == "concern":
        definition = boundary.OwnerCommandDefinition(
            owner=owner.OWNER, concern="other concern", name="delete_customer_account"
        )
    monkeypatch.setattr(boundary, "owner_command_active", lambda *a, **kw: True)
    monkeypatch.setattr(boundary, "current_command_definition", lambda db: definition)
    monkeypatch.setattr(
        boundary,
        "current_command_context",
        lambda db: _context() if mutation == "context" else context,
    )
    actor = AuditActor.user("other") if mutation == "actor" else ACTOR
    if mutation == "scope":
        context = CommandContext.system(
            actor=f"user:{ACTOR.actor_id}", scope="other", reason="test"
        )
    with pytest.raises(owner.CustomerAccountDeletionError):
        owner.require_customer_deletion_participant(
            db_session, context=context, actor=actor
        )


@pytest.mark.parametrize(
    "principal_type,scopes,expected_status",
    [
        ("api_key", ["customer:delete"], 204),
        ("system_user", ["customer:delete"], 204),
        ("api_key", ["customer:read"], 403),
    ],
)
def test_json_route_enforces_permission_and_forwards_authenticated_actor(
    db_session, monkeypatch, principal_type, scopes, expected_status
):
    from app.api import subscribers
    from app.db import get_db
    from app.models.audit import AuditActorType
    from app.services import auth_dependencies

    principal_id, customer_id = uuid4(), uuid4()
    commands: list[owner.DeleteCustomerAccountCommand] = []

    def authenticated(request: Request) -> dict[str, object]:
        # Authentication is upstream; retain the real route permission check.
        auth = {
            "principal_id": str(principal_id),
            "principal_type": principal_type,
            "roles": [],
            "scopes": scopes,
        }
        request.state.auth = auth
        return auth

    @contextmanager
    def fresh_session():
        yield object()

    def execute(db, *, command: owner.DeleteCustomerAccountCommand):
        commands.append(command)
        return owner.CustomerAccountDeletionOutcome(
            command.subscriber_id, owner.CustomerDeletionStatus.DELETED
        )

    application = FastAPI()
    application.include_router(subscribers.router)
    application.dependency_overrides[get_db] = lambda: db_session
    application.dependency_overrides[auth_dependencies._get_db] = lambda: db_session
    application.dependency_overrides[auth_dependencies.require_user_auth] = (
        authenticated
    )
    monkeypatch.setattr(
        adapter.db_session_adapter, "owner_command_session", fresh_session
    )
    monkeypatch.setattr(adapter, "delete_customer_account", execute)
    with TestClient(application) as client:
        response = client.delete(f"/subscribers/{customer_id}")
    assert response.status_code == expected_status
    if expected_status == 403:
        assert commands == []
    else:
        assert len(commands) == 1
        command = commands[0]
        assert command.subscriber_id == customer_id
        assert command.policy is owner.CustomerDeletionPolicy.JSON
        assert command.actor.actor_id == str(principal_id)
        assert command.actor.actor_type is (
            AuditActorType.api_key
            if principal_type == "api_key"
            else AuditActorType.user
        )
        assert (
            command.context.actor == f"{command.actor.actor_type.value}:{principal_id}"
        )
