from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.custom_fields import CustomFieldDefinition, CustomFieldValue
from app.services import custom_field_capabilities, custom_fields
from app.services.owner_commands import CommandContext

TENANT_ID = UUID("8c7ae830-51fc-52ae-9818-d84b2a35e568")


def _context(reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:test-admin",
        scope="custom-fields-test",
        reason=reason,
        idempotency_key=f"custom-fields-test:{command_id}",
    )


def _permissions(*extra: str) -> frozenset[str]:
    return frozenset(
        {
            "customer:read",
            "customer:update",
            custom_fields.DEFINITION_CREATE_PERMISSION,
            custom_fields.DEFINITION_UPDATE_PERMISSION,
            custom_fields.DEFINITION_ACTIVATE_PERMISSION,
            custom_fields.DEFINITION_RETIRE_PERMISSION,
            custom_fields.VALUE_READ_PERMISSION,
            custom_fields.VALUE_WRITE_PERMISSION,
            *extra,
        }
    )


@pytest.fixture(autouse=True)
def no_external_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        custom_fields, "stage_audit_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(custom_fields, "emit_event", lambda *_args, **_kwargs: None)


def test_subscriber_is_the_initial_registered_target() -> None:
    assert custom_field_capabilities.capability_registry_errors() == ()
    target = custom_field_capabilities.target_capability("subscriber")
    assert target.read_permission == "customer:read"
    assert target.write_permission == "customer:update"
    assert "{target_id}" in target.detail_path_template


def test_definition_lifecycle_and_typed_value_are_owned_atomically(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = custom_fields.create_definition(
        db_session,
        custom_fields.CreateCustomFieldDefinitionCommand(
            tenant_id=TENANT_ID,
            target_type="subscriber",
            key="installation_reference",
            definition=custom_fields.CustomFieldDefinitionInput(
                label="Installation reference",
                description="Reviewed external installation identity.",
                field_type=custom_fields.CustomFieldType.select,
                options=("pending", "verified"),
                required=True,
                section="Installation",
            ),
            permission_keys=_permissions(),
            context=_context("create field"),
        ),
    )
    assert created.status is custom_fields.CustomFieldDefinitionStatus.draft

    activated = custom_fields.change_definition_status(
        db_session,
        custom_fields.ChangeCustomFieldDefinitionStatusCommand(
            tenant_id=TENANT_ID,
            definition_id=created.definition_id,
            operation=custom_fields.CustomFieldDefinitionOperation.activate,
            permission_keys=_permissions(),
            context=_context("activate field"),
        ),
    )
    assert activated.status is custom_fields.CustomFieldDefinitionStatus.active

    target_id = uuid4()
    monkeypatch.setattr(
        custom_fields.custom_field_targets,
        "target_exists",
        lambda *_args, **_kwargs: True,
    )
    outcome = custom_fields.set_value(
        db_session,
        custom_fields.SetCustomFieldValueCommand(
            tenant_id=TENANT_ID,
            definition_id=created.definition_id,
            target_type="subscriber",
            target_id=target_id,
            value="verified",
            permission_keys=_permissions(),
            context=_context("set field value"),
        ),
    )
    assert outcome.cleared is False
    stored = db_session.scalar(
        select(CustomFieldValue).where(CustomFieldValue.target_id == target_id)
    )
    assert stored is not None
    assert stored.value == "verified"


def test_active_definition_rejects_structural_change(db_session) -> None:
    created = custom_fields.create_definition(
        db_session,
        custom_fields.CreateCustomFieldDefinitionCommand(
            tenant_id=TENANT_ID,
            target_type="subscriber",
            key="site_note",
            definition=custom_fields.CustomFieldDefinitionInput(
                label="Site note",
                description=None,
                field_type=custom_fields.CustomFieldType.text,
            ),
            permission_keys=_permissions(),
            context=_context("create field"),
        ),
    )
    custom_fields.change_definition_status(
        db_session,
        custom_fields.ChangeCustomFieldDefinitionStatusCommand(
            tenant_id=TENANT_ID,
            definition_id=created.definition_id,
            operation=custom_fields.CustomFieldDefinitionOperation.activate,
            permission_keys=_permissions(),
            context=_context("activate field"),
        ),
    )
    with pytest.raises(custom_fields.CustomFieldError) as exc_info:
        custom_fields.update_definition(
            db_session,
            custom_fields.UpdateCustomFieldDefinitionCommand(
                tenant_id=TENANT_ID,
                definition_id=created.definition_id,
                definition=custom_fields.CustomFieldDefinitionInput(
                    label="Site note",
                    description=None,
                    field_type=custom_fields.CustomFieldType.integer,
                ),
                permission_keys=_permissions(),
                context=_context("change active type"),
            ),
        )
    assert exc_info.value.code == "custom_fields.records.active_definition_locked"
    row = db_session.get(CustomFieldDefinition, created.definition_id)
    assert row is not None
    assert row.field_type == "text"


def test_sensitive_value_requires_dedicated_permission(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = custom_fields.create_definition(
        db_session,
        custom_fields.CreateCustomFieldDefinitionCommand(
            tenant_id=TENANT_ID,
            target_type="subscriber",
            key="private_reference",
            definition=custom_fields.CustomFieldDefinitionInput(
                label="Private reference",
                description=None,
                field_type=custom_fields.CustomFieldType.text,
                sensitive=True,
            ),
            permission_keys=_permissions(custom_fields.SENSITIVE_WRITE_PERMISSION),
            context=_context("create sensitive field"),
        ),
    )
    custom_fields.change_definition_status(
        db_session,
        custom_fields.ChangeCustomFieldDefinitionStatusCommand(
            tenant_id=TENANT_ID,
            definition_id=created.definition_id,
            operation=custom_fields.CustomFieldDefinitionOperation.activate,
            permission_keys=_permissions(),
            context=_context("activate sensitive field"),
        ),
    )
    monkeypatch.setattr(
        custom_fields.custom_field_targets,
        "target_exists",
        lambda *_args, **_kwargs: True,
    )
    with pytest.raises(custom_fields.CustomFieldError) as exc_info:
        custom_fields.set_value(
            db_session,
            custom_fields.SetCustomFieldValueCommand(
                tenant_id=TENANT_ID,
                definition_id=created.definition_id,
                target_type="subscriber",
                target_id=uuid4(),
                value="secret",
                permission_keys=_permissions(),
                context=_context("set sensitive value"),
            ),
        )
    assert exc_info.value.code == "custom_fields.records.permission_denied"
