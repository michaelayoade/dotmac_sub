from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.customer_connection_type import CustomerConnectionTypeDefinition
from app.services.customer_connection_types import (
    CustomerConnectionTypeError,
    SaveConnectionTypeCommand,
    connection_type_options,
    save_definition,
    validate_assignment,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="test:admin",
        scope="system:settings:write",
        reason="Test managed customer connection types",
        idempotency_key=f"customer-connection-type:test:{command_id}",
    )


def test_managed_connection_type_is_created_and_offered(db_session):
    db_session_adapter.release_read_transaction(db_session)
    outcome = save_definition(
        db_session,
        SaveConnectionTypeCommand(
            context=_context(),
            definition_id=None,
            name="Both",
            description="Wired and wireless service",
            sort_order=30,
            is_active=True,
        ),
    )

    assert outcome.code == "both"
    assert [(item.code, item.name) for item in connection_type_options(db_session)] == [
        ("both", "Both")
    ]
    assert validate_assignment(db_session, connection_type="both") == "both"


def test_inactive_type_is_retained_only_for_its_current_customer(db_session):
    definition = CustomerConnectionTypeDefinition(
        code="legacy-radio",
        name="Legacy Radio",
        is_active=False,
    )
    db_session.add(definition)
    db_session.commit()

    options = connection_type_options(db_session, current_code="legacy-radio")

    assert [(item.code, item.is_active) for item in options] == [
        ("legacy-radio", False)
    ]
    assert (
        validate_assignment(
            db_session,
            connection_type="legacy-radio",
            current_code="legacy-radio",
        )
        == "legacy-radio"
    )
    with pytest.raises(CustomerConnectionTypeError, match="inactive"):
        validate_assignment(db_session, connection_type="legacy-radio")


def test_customer_ui_uses_managed_options_and_links_to_the_field():
    settings_template = Path(
        "templates/admin/system/customer_connection_types.html"
    ).read_text(encoding="utf-8")
    form_template = Path("templates/admin/customers/form.html").read_text(
        encoding="utf-8"
    )
    detail_template = Path("templates/admin/customers/detail.html").read_text(
        encoding="utf-8"
    )

    assert "Setup Customer Connection Types" in settings_template
    assert "{% for item in customer_connection_types|default([]) %}" in form_template
    assert 'id="connection-type"' in form_template
    assert "edit#connection-type" in detail_template
    assert "detail_connection_type" in detail_template
