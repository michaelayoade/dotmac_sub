"""Collect locally only; hosted real-Alembic PostgreSQL is authoritative."""

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.schemas.auth import ApiKeyCreate, ApiKeyGenerateRequest
from app.services import api_key_administration as owner
from tests.api_key_issuance_fixtures import create_command, generate_command
from tests.integration import test_oidc_atomic_session_concurrency as races

engine = races.engine


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_issuance_atomic_rows_on_migrated_schema(
    engine: Engine, generated: bool, fail_commit: bool
) -> None:
    def refuse(session):
        raise RuntimeError("inert commit refusal")

    with Session(engine) as db:
        if fail_commit:
            event.listen(db, "before_commit", refuse)
        execute = owner.generate if generated else owner.create
        command = (
            generate_command(ApiKeyGenerateRequest())
            if generated
            else create_command(ApiKeyCreate(key_hash="inert-pg-preimage"))
        )
        if fail_commit:
            with pytest.raises(RuntimeError, match="inert commit refusal"):
                execute(db, command=command)
        else:
            execute(db, command=command)
    with Session(engine) as observer:
        rows = observer.scalars(select(ApiKey)).all()
        audits = observer.scalars(
            select(AuditEvent).where(
                AuditEvent.action.in_(
                    ("auth.api_key.created", "auth.api_key.generated")
                )
            )
        ).all()
        assert len(rows) == len(audits) == (0 if fail_commit else 1)
        if rows:
            assert audits[0].entity_id == str(rows[0].id)


def test_unique_preimage_backstop_keeps_one_audit(engine: Engine) -> None:
    command = create_command(ApiKeyCreate(key_hash="inert-pg-unique-preimage"))
    with Session(engine) as first:
        owner.create(first, command=command)
    with Session(engine) as second:
        with pytest.raises(owner.ApiKeyIssuanceError) as error:
            owner.create(second, command=command)
        assert error.value.code.endswith(".storage_unavailable")
    with Session(engine) as observer:
        assert len(observer.scalars(select(ApiKey)).all()) == 1
        assert (
            len(
                observer.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "auth.api_key.created"
                    )
                ).all()
            )
            == 1
        )
