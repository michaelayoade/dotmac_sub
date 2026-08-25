"""Provide the shared database effects installable tenant modules require.

Sub owns this application database and does not compose the kernel core
lineage, whose identity/RBAC/audit tables collide with Sub-owned tables.  This
revision supplies only four named, live-verified effects:

* tenant_scope_catalog.v1
* module_database_roles.v1
* idempotency_ledger.v1
* outbox_relay.v1

The timer and campaigns lineages bind to this revision.  They remain separate
owners of their own ``mod_*`` schemas and never name this revision directly.

Revision ID: 544_campaign_module_foundation
Revises: 543_ont_config_unverified
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from dotmac_kernel.migrations.verify import require_prerequisites
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "544_campaign_module_foundation"
down_revision: str | None = "543_ont_config_unverified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDES = (
    "tenant_scope_catalog.v1",
    "module_database_roles.v1",
    "idempotency_ledger.v1",
    "outbox_relay.v1",
)

_TENANT_LEDGER = "idempotency_records"
_PLATFORM_LEDGER = "platform_idempotency_records"
_TENANT_OUTBOX = "outbox_events"
_PLATFORM_OUTBOX = "platform_outbox_events"

_TENANT_CLAIM = "public.claim_outbox_batch(text, integer, integer)"
_TENANT_SETTLE = (
    "public.settle_outbox_event(uuid, text, text, timestamptz, integer, text)"
)
_PLATFORM_CLAIM = "public.claim_platform_outbox_batch(text, integer, integer)"
_PLATFORM_SETTLE = (
    "public.settle_platform_outbox_event"
    "(uuid, text, text, timestamptz, integer, text)"
)


def _ensure_roles() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
                CREATE ROLE app_admin LOGIN NOSUPERUSER BYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user LOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_api') THEN
                CREATE ROLE platform_api LOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'outbox_dispatcher'
            ) THEN
                CREATE ROLE outbox_dispatcher LOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'platform_outbox_dispatcher'
            ) THEN
                CREATE ROLE platform_outbox_dispatcher
                    LOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    op.execute("ALTER ROLE app_admin NOSUPERUSER BYPASSRLS;")
    for role in (
        "app_user",
        "platform_api",
        "outbox_dispatcher",
        "platform_outbox_dispatcher",
    ):
        op.execute(f"ALTER ROLE {role} NOSUPERUSER NOBYPASSRLS;")


def _complete_tenant_scope_catalog() -> None:
    for table, column in (
        ("tenants", "created_at"),
        ("tenants", "updated_at"),
        ("tenant_domains", "created_at"),
        ("tenant_domains", "updated_at"),
    ):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.func.now(),
        )
    op.alter_column(
        "tenants",
        "is_active",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.app_current_tenant_id()
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        AS $fn$
        BEGIN
            RETURN NULLIF(current_setting('app.current_tenant', true), '')::uuid;
        EXCEPTION
            WHEN invalid_text_representation THEN
                RETURN NULL;
        END;
        $fn$;
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.app_current_tenant_id() "
        "TO app_user, platform_api;"
    )
    op.execute(
        "GRANT SELECT ON public.tenants, public.tenant_domains "
        "TO app_user, platform_api;"
    )


def _ledger_columns(*, tenant: bool) -> list[sa.Column[Any]]:
    columns: list[sa.Column[Any]] = [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    ]
    if tenant:
        columns.append(
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)
        )
    columns.extend(
        [
            sa.Column("scope", sa.String(120), nullable=False),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("fingerprint", sa.String(64)),
            sa.Column("operation", sa.String(120), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column(
                "result",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("correlation_id", sa.String(200)),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]
    )
    return columns


def _create_idempotency_ledgers() -> None:
    op.create_table(
        _TENANT_LEDGER,
        *_ledger_columns(tenant=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["public.tenants.id"],
            ondelete="CASCADE",
            name="fk_idempotency_records_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "key",
            name="uq_idempotency_records_tenant_scope_key",
        ),
    )
    op.create_index(
        "ix_idempotency_records_tenant_id", _TENANT_LEDGER, ["tenant_id"]
    )
    op.create_index(
        "ix_idempotency_records_expires_at", _TENANT_LEDGER, ["expires_at"]
    )
    op.execute(f"ALTER TABLE public.{_TENANT_LEDGER} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE public.{_TENANT_LEDGER} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY idempotency_records_tenant_isolation
            ON public.{_TENANT_LEDGER}
            USING (tenant_id = public.app_current_tenant_id())
            WITH CHECK (tenant_id = public.app_current_tenant_id());
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TENANT_LEDGER} "
        "TO app_user, platform_api, app_admin;"
    )

    op.create_table(
        _PLATFORM_LEDGER,
        *_ledger_columns(tenant=False),
        sa.UniqueConstraint(
            "scope",
            "key",
            name="uq_platform_idempotency_records_scope_key",
        ),
    )
    op.create_index(
        "ix_platform_idempotency_records_expires_at",
        _PLATFORM_LEDGER,
        ["expires_at"],
    )
    op.execute(f"REVOKE ALL ON public.{_PLATFORM_LEDGER} FROM PUBLIC;")
    op.execute(f"REVOKE ALL ON public.{_PLATFORM_LEDGER} FROM app_user;")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_PLATFORM_LEDGER} "
        "TO platform_api, app_admin;"
    )


def _outbox_columns(*, tenant: bool) -> list[sa.Column[Any]]:
    columns: list[sa.Column[Any]] = [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    ]
    if tenant:
        columns.append(
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)
        )
    columns.extend(
        [
            sa.Column("event_type", sa.String(120), nullable=False),
            sa.Column(
                "payload",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "available_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("correlation_id", sa.String(200)),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.String(500)),
            sa.Column("leased_by", sa.String(200)),
            sa.Column("leased_at", sa.DateTime(timezone=True)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]
    )
    return columns


def _create_outbox_tables() -> None:
    op.create_table(
        _TENANT_OUTBOX,
        *_outbox_columns(tenant=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["public.tenants.id"],
            ondelete="CASCADE",
            name="fk_outbox_events_tenant",
        ),
    )
    op.create_index("ix_outbox_events_tenant_id", _TENANT_OUTBOX, ["tenant_id"])
    op.create_index(
        "ix_outbox_events_status_available_at",
        _TENANT_OUTBOX,
        ["status", "available_at"],
    )
    op.create_index(
        "ix_outbox_events_status_leased_at",
        _TENANT_OUTBOX,
        ["status", "leased_at"],
    )
    op.execute(f"ALTER TABLE public.{_TENANT_OUTBOX} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE public.{_TENANT_OUTBOX} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY outbox_events_tenant_isolation ON public.{_TENANT_OUTBOX}
            USING (tenant_id = public.app_current_tenant_id())
            WITH CHECK (tenant_id = public.app_current_tenant_id());
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TENANT_OUTBOX} "
        "TO app_user, platform_api, app_admin;"
    )
    op.execute(f"REVOKE ALL ON public.{_TENANT_OUTBOX} FROM outbox_dispatcher;")

    op.create_table(
        _PLATFORM_OUTBOX,
        *_outbox_columns(tenant=False),
    )
    op.create_index(
        "ix_platform_outbox_events_status_available_at",
        _PLATFORM_OUTBOX,
        ["status", "available_at"],
    )
    op.create_index(
        "ix_platform_outbox_events_status_leased_at",
        _PLATFORM_OUTBOX,
        ["status", "leased_at"],
    )
    op.execute(f"REVOKE ALL ON public.{_PLATFORM_OUTBOX} FROM PUBLIC;")
    op.execute(f"REVOKE ALL ON public.{_PLATFORM_OUTBOX} FROM app_user;")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_PLATFORM_OUTBOX} "
        "TO platform_api, app_admin;"
    )
    op.execute(
        f"REVOKE ALL ON public.{_PLATFORM_OUTBOX} "
        "FROM platform_outbox_dispatcher;"
    )


def _create_tenant_relay_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION public.claim_outbox_batch(
            p_worker text, p_batch integer, p_stale_seconds integer)
        RETURNS SETOF public.outbox_events
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = ''
        AS $fn$
            UPDATE public.outbox_events
               SET status = 'claimed', leased_by = p_worker, leased_at = now()
             WHERE id IN (
               SELECT id FROM public.outbox_events
                WHERE (status = 'pending' AND available_at <= now())
                   OR (status = 'claimed'
                       AND leased_at < now() - make_interval(secs => p_stale_seconds))
                ORDER BY available_at
                FOR UPDATE SKIP LOCKED
                LIMIT p_batch
             )
             RETURNING *;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.settle_outbox_event(
            p_id uuid, p_worker text, p_status text, p_available_at timestamptz,
            p_attempts integer, p_last_error text)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $fn$
        DECLARE n integer;
        BEGIN
            UPDATE public.outbox_events
               SET status = p_status,
                   attempts = p_attempts,
                   last_error = p_last_error,
                   available_at = COALESCE(p_available_at, available_at),
                   sent_at = CASE WHEN p_status = 'sent' THEN now() ELSE sent_at END,
                   leased_by = CASE WHEN p_status IN ('sent', 'dead')
                                    THEN NULL ELSE leased_by END,
                   leased_at = CASE WHEN p_status IN ('sent', 'dead')
                                    THEN NULL ELSE leased_at END
             WHERE id = p_id AND leased_by = p_worker AND status = 'claimed';
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN n = 1;
        END
        $fn$;
        """
    )
    for signature in (_TENANT_CLAIM, _TENANT_SETTLE):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO app_admin;")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO outbox_dispatcher;")
    op.execute("GRANT USAGE ON SCHEMA public TO outbox_dispatcher;")


def _create_platform_relay_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION public.claim_platform_outbox_batch(
            p_worker text, p_batch integer, p_stale_seconds integer)
        RETURNS SETOF public.platform_outbox_events
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = ''
        AS $fn$
            UPDATE public.platform_outbox_events
               SET status = 'claimed', leased_by = p_worker, leased_at = now()
             WHERE id IN (
               SELECT id FROM public.platform_outbox_events
                WHERE (status = 'pending' AND available_at <= now())
                   OR (status = 'claimed'
                       AND leased_at < now() - make_interval(secs => p_stale_seconds))
                ORDER BY available_at
                FOR UPDATE SKIP LOCKED
                LIMIT p_batch
             )
             RETURNING *;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.settle_platform_outbox_event(
            p_id uuid, p_worker text, p_status text, p_available_at timestamptz,
            p_attempts integer, p_last_error text)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $fn$
        DECLARE n integer;
        BEGIN
            UPDATE public.platform_outbox_events
               SET status = p_status,
                   attempts = p_attempts,
                   last_error = p_last_error,
                   available_at = COALESCE(p_available_at, available_at),
                   sent_at = CASE WHEN p_status = 'sent' THEN now() ELSE sent_at END,
                   leased_by = CASE WHEN p_status IN ('sent', 'dead')
                                    THEN NULL ELSE leased_by END,
                   leased_at = CASE WHEN p_status IN ('sent', 'dead')
                                    THEN NULL ELSE leased_at END
             WHERE id = p_id AND leased_by = p_worker AND status = 'claimed';
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN n = 1;
        END
        $fn$;
        """
    )
    for signature in (_PLATFORM_CLAIM, _PLATFORM_SETTLE):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO app_admin;")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {signature} "
            "TO platform_outbox_dispatcher;"
        )
    op.execute("GRANT USAGE ON SCHEMA public TO platform_outbox_dispatcher;")


def upgrade() -> None:
    _ensure_roles()
    _complete_tenant_scope_catalog()
    _create_idempotency_ledgers()
    _create_outbox_tables()
    _create_tenant_relay_functions()
    _create_platform_relay_functions()
    require_prerequisites(op.get_bind(), PROVIDES)


def downgrade() -> None:
    for signature in (
        _PLATFORM_SETTLE,
        _PLATFORM_CLAIM,
        _TENANT_SETTLE,
        _TENANT_CLAIM,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature};")
    for table in (
        _PLATFORM_OUTBOX,
        _TENANT_OUTBOX,
        _PLATFORM_LEDGER,
        _TENANT_LEDGER,
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS public.app_current_tenant_id();")
    op.alter_column(
        "tenants",
        "is_active",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=None,
    )
    for table, column in (
        ("tenants", "created_at"),
        ("tenants", "updated_at"),
        ("tenant_domains", "created_at"),
        ("tenant_domains", "updated_at"),
    ):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=None,
        )
