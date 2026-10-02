"""Real migrated PostgreSQL proof for Sub 639 and installed Kernel a97."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import machine_auth
from dotmac_kernel.exceptions import UnauthorizedError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@contextmanager
def _rollback_connection(engine: Engine) -> Iterator[sa.Connection]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _insert_credential(
    connection: sa.Connection,
    *,
    tenant_id: UUID,
    key_hash: str,
    source_application: str | None,
    next_key_hash: str | None = None,
    rotation_started_at: datetime | None = None,
) -> UUID:
    credential_id = uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO public.machine_credentials "
            "(id, tenant_id, label, key_hash, scopes, source_application, "
            "next_key_hash, rotation_started_at) VALUES "
            "(:id, :tenant_id, :label, :key_hash, CAST(:scopes AS json), "
            ":source_application, :next_key_hash, :rotation_started_at)"
        ),
        {
            "id": str(credential_id),
            "tenant_id": str(tenant_id),
            "label": f"machine-639-{credential_id.hex}",
            "key_hash": key_hash,
            "scopes": '["billing:invoice:read"]',
            "source_application": source_application,
            "next_key_hash": next_key_hash,
            "rotation_started_at": rotation_started_at,
        },
    )
    return credential_id


def test_migrated_machine_schema_rls_authentication_and_constraints(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert engine.dialect.name == "postgresql"
    monkeypatch.setattr(
        machine_auth, "get_secret", lambda name: "test-only-held-machine-material"
    )
    first_tenant, second_tenant = uuid4(), uuid4()
    first_raw, second_raw, anonymous_raw = "first-canary", "second-canary", "old-canary"
    first_hash = machine_auth.hash_machine_key(first_raw)
    second_hash = machine_auth.hash_machine_key(second_raw)
    anonymous_hash = machine_auth.hash_machine_key(anonymous_raw)
    shared_next_hash = machine_auth.hash_machine_key("shared-incoming-canary")
    now = datetime(2026, 10, 1, tzinfo=UTC)

    # Reuse the root fixture's Alembic-migrated engine. All writes roll back.
    with _rollback_connection(engine) as connection:
        assert connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM alembic_version WHERE version_num = '639_machine_attribution')"
            )
        )
        columns = dict(
            connection.execute(
                sa.text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'machine_credentials' "
                    "AND column_name IN ('source_application', 'next_key_hash', "
                    "'rotation_started_at', 'rotated_at')"
                )
            ).all()
        )
        assert columns == {
            "source_application": "YES",
            "next_key_hash": "YES",
            "rotation_started_at": "YES",
            "rotated_at": "YES",
        }
        assert connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'public.machine_credentials'::regclass"
            )
        ).one() == (True, True)

        for tenant_id in (first_tenant, second_tenant):
            connection.execute(
                sa.text(
                    "INSERT INTO public.tenants (id, slug, name, is_active) "
                    "VALUES (:id, :slug, 'Machine 639 canary', true)"
                ),
                {"id": str(tenant_id), "slug": f"machine-639-{tenant_id.hex}"},
            )
        first_id = _insert_credential(
            connection,
            tenant_id=first_tenant,
            key_hash=first_hash,
            source_application="dotmac_erp",
            next_key_hash=shared_next_hash,
            rotation_started_at=now,
        )
        second_id = _insert_credential(
            connection,
            tenant_id=second_tenant,
            key_hash=second_hash,
            source_application="dotmac_erp",
            next_key_hash=shared_next_hash,
            rotation_started_at=now,
        )
        # The pre-639 INSERT shape remains valid. No default invents a caller.
        anonymous_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO public.machine_credentials "
                "(id, tenant_id, label, key_hash, scopes) VALUES "
                "(:id, :tenant_id, :label, :key_hash, CAST(:scopes AS json))"
            ),
            {
                "id": str(anonymous_id),
                "tenant_id": str(first_tenant),
                "label": f"machine-639-{anonymous_id.hex}",
                "key_hash": anonymous_hash,
                "scopes": '["billing:invoice:read"]',
            },
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT source_application FROM public.machine_credentials WHERE id = :id"
                ),
                {"id": str(anonymous_id)},
            )
            is None
        )

        invalid_rows = (
            (
                "sha256:weak",
                now,
                "dotmac_erp",
                "ck_machine_credentials_next_key_hash_scheme",
            ),
            (
                "hmac-sha256:orphan",
                None,
                "dotmac_erp",
                "ck_machine_credentials_rotation_pair",
            ),
            (
                "same-as-current",
                now,
                "dotmac_erp",
                "ck_machine_credentials_next_key_hash_differs",
            ),
            (
                None,
                None,
                " bad ",
                "ck_machine_credentials_source_application_shape",
            ),
            (
                shared_next_hash,
                now,
                "dotmac_erp",
                "uq_machine_credentials_tenant_next_key_hash",
            ),
        )
        for next_hash, started, source, expected_constraint in invalid_rows:
            current_hash = machine_auth.hash_machine_key(str(uuid4()))
            with pytest.raises(IntegrityError) as failure:
                with connection.begin_nested():
                    _insert_credential(
                        connection,
                        tenant_id=first_tenant,
                        key_hash=current_hash,
                        source_application=source,
                        next_key_hash=current_hash
                        if next_hash == "same-as-current"
                        else next_hash,
                        rotation_started_at=started,
                    )
            assert failure.value.orig.diag.constraint_name == expected_constraint

        connection.execute(sa.text("SET LOCAL ROLE app_user"))
        posture = connection.execute(
            sa.text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        assert posture == ("app_user", False, False)
        with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
            # Establish the ORM transaction first: Sub's after_begin hook
            # installs its operator tenant. This isolation canary then sets
            # each synthetic tenant inside that same active transaction.
            db.connection()
            for tenant_id, visible in (
                (first_tenant, {first_id, anonymous_id}),
                (second_tenant, {second_id}),
            ):
                connection.execute(
                    sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
                    {"tenant": str(tenant_id)},
                )
                found = set(
                    connection.execute(
                        sa.text(
                            "SELECT id FROM public.machine_credentials WHERE id IN (:a, :b, :c)"
                        ),
                        {
                            "a": str(first_id),
                            "b": str(second_id),
                            "c": str(anonymous_id),
                        },
                    ).scalars()
                )
                assert found == visible
                if tenant_id == first_tenant:
                    assert (
                        machine_auth.authenticate_machine(db, first_raw).application
                        == "dotmac_erp"
                    )
                    with pytest.raises(
                        UnauthorizedError, match="machine credential is not valid"
                    ):
                        machine_auth.authenticate_machine(db, anonymous_raw)
                else:
                    assert (
                        machine_auth.authenticate_machine(db, second_raw).application
                        == "dotmac_erp"
                    )
                    with pytest.raises(
                        UnauthorizedError, match="machine credential is not valid"
                    ):
                        machine_auth.authenticate_machine(db, first_raw)
