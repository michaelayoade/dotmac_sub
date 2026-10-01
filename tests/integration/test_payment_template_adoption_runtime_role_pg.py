"""Migrated PostgreSQL proof for payment email adoption's runtime RLS role gate."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import CommandContext
from app.services.payment_template_adoption import (
    ReviewedPaymentEmailTemplates,
    _require_rls_runtime_role,
    adopt_payment_email_templates,
    payment_email_parity_report,
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="test:operator",
        scope=str(operator_tenant_id()),
        reason="runtime role posture proof",
    )


def test_migrated_postgres_refuses_elevated_role_before_content_access(
    db_session: Session,
) -> None:
    connection = db_session.get_bind()
    assert isinstance(connection, Connection)
    posture = connection.execute(
        sa.text(
            "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user"
        )
    ).one()
    assert posture.rolsuper or posture.rolbypassrls, (
        "PostgreSQL integration connection must use an elevated migration role"
    )

    statements: list[str] = []

    @sa.event.listens_for(connection, "before_cursor_execute")
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    try:
        with pytest.raises(DomainError) as failure:
            payment_email_parity_report(db_session)
        assert failure.value.code == "payment_template_adoption.unsafe_runtime_role"
        db_session.rollback()

        with pytest.raises(DomainError) as failure:
            adopt_payment_email_templates(
                db_session,
                context=_context(),
                reviewed=ReviewedPaymentEmailTemplates(uuid4(), uuid4()),
            )
        assert failure.value.code == "payment_template_adoption.unsafe_runtime_role"
    finally:
        sa.event.remove(connection, "before_cursor_execute", record)

    assert sum("pg_catalog.pg_roles" in sql for sql in statements) == 2
    assert not any(
        "notification_templates" in sql or "mod_tstudio" in sql for sql in statements
    )


def test_migrated_postgres_app_user_passes_runtime_role_guard(
    db_session: Session,
) -> None:
    connection = db_session.get_bind()
    assert isinstance(connection, Connection)
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(operator_tenant_id())},
    )
    assert connection.scalar(sa.text("SELECT current_user")) == "app_user"
    posture = connection.execute(
        sa.text(
            "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user"
        )
    ).one()
    assert not posture.rolsuper
    assert not posture.rolbypassrls

    _require_rls_runtime_role(db_session)
