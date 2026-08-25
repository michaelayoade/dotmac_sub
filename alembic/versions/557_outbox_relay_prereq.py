"""Host the kernel outbox relay contract in Sub's own lineage.

Sub cannot run or stamp the kernel root, so every effect a composed module
declares has to be supplied here and bound in ``app/migration_bindings.py``.
This revision supplies ``outbox_relay.v1``: the two relay planes, their
claim/settle pairs and the privilege boundary that makes the cross-tenant drain
safe.

The implementation is ported product-first from the production-used ERP
provider at commit ``dc10b24af22b1452b9954d4c33ff87a5916a4afe``
(``alembic/versions/20260824_outbox_relay.py``), then tightened here to verify
that both dispatcher identities are real LOGIN roles.

## This is NOT Sub's existing business-event outboxes

Sub's ``event_store``, owner-output, notification, network-operation and
field-ERP outboxes retain their current authorities, vocabularies and
dispatchers. The tables created here carry MODULE events only — what
``dotmac_kernel.messaging`` enqueues and drains for the composed commercial
modules. Each mechanism keeps one authority; neither reads another's rows, and
no incumbent Sub event, delivery or money consequence moves onto this relay in
this slice.

**Storage is not delivery.** These tables accumulate until something drains
them. Deploying a dispatcher for each plane is a named follow-up, and until it
exists a composed module that enqueues an event has produced a durable row and
no side effect.

## Why this migration verifies roles instead of creating them

The kernel's own ``0011``/``0012`` create ``outbox_dispatcher`` and
``platform_outbox_dispatcher`` with a ``DO`` block. Sub does not create new
cluster roles in this migration: ``CREATE ROLE`` needs privileges an ordinary
``alembic upgrade`` must never hold. Creation belongs to the explicitly
elevated ``scripts/bootstrap_outbox_dispatcher_roles.py``. This revision fails
closed before DDL when either dispatcher role is absent or wrong-shaped.

The closing ``require_prerequisites`` call proves the result against the exact
pinned kernel contract — the shape, the indexes, both planes' postures, the
definer owner, the empty ``search_path``, and that EXECUTE did not reach PUBLIC.

Revision ID: 557_outbox_relay_prereq
Revises: 556_idempotency_ledger_prereq
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from dotmac_kernel.migrations.verify import require_prerequisites
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "557_outbox_relay_prereq"
down_revision: str | None = "556_idempotency_ledger_prereq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRES = ("outbox_relay.v1",)

TENANT_TABLE = "outbox_events"
PLATFORM_TABLE = "platform_outbox_events"

TENANT_DISPATCHER = "outbox_dispatcher"
PLATFORM_DISPATCHER = "platform_outbox_dispatcher"

#: Point-in-time copy of `app.outbox_dispatcher_roles.RELAY_DISPATCHER_CONTRACT`,
#: as ``(rolcanlogin, rolbypassrls, rolsuper)``. Copied rather than imported:
#: a migration is a snapshot of an accepted decision, and importing a mutable
#: runtime value would let a later edit change what an applied revision meant.
#: The architecture test pins the two together.
DISPATCHER_CONTRACT: dict[str, tuple[bool, bool, bool]] = {
    TENANT_DISPATCHER: (True, False, False),
    PLATFORM_DISPATCHER: (True, False, False),
}

TENANT_CLAIM_SIG = "public.claim_outbox_batch(text, integer, integer)"
TENANT_SETTLE_SIG = (
    "public.settle_outbox_event(uuid, text, text, timestamptz, integer, text)"
)
PLATFORM_CLAIM_SIG = "public.claim_platform_outbox_batch(text, integer, integer)"
PLATFORM_SETTLE_SIG = (
    "public.settle_platform_outbox_event(uuid, text, text, timestamptz, integer, text)"
)

#: Every column of the platform ledger, for the column-level revoke below.
#: `has_any_column_privilege` sees a column grant that `has_table_privilege`
#: cannot, and the kernel verifier checks both — so revoking at table level
#: alone would leave the platform plane reachable by tenant request traffic
#: through a grant nobody looked at.
_RELAY_COLUMNS = (
    "id",
    "event_type",
    "payload",
    "status",
    "attempts",
    "available_at",
    "correlation_id",
    "sent_at",
    "last_error",
    "leased_by",
    "leased_at",
    "created_at",
    "updated_at",
)


def _relay_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("correlation_id", sa.String(length=200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("leased_by", sa.String(length=200), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
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


def _assert_dispatcher_roles_exist() -> None:
    """Fail closed before any DDL if the drain identities are wrong.

    A missing role is not a nuisance here. Every grant below names it, so the
    migration would fail anyway — but halfway through, having already created
    two tables and four functions on a cluster whose privilege boundary does
    not exist.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    observed = {
        str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3]))
        for row in bind.execute(
            sa.text(
                "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname = ANY(:names)"
            ),
            {"names": list(DISPATCHER_CONTRACT)},
        ).all()
    }
    problems: list[str] = []
    for role, expected in DISPATCHER_CONTRACT.items():
        actual = observed.get(role)
        if actual is None:
            problems.append(f"database role {role!r} is missing")
        elif actual != expected:
            problems.append(
                f"{role} has (rolcanlogin, rolbypassrls, rolsuper)={actual!r}, "
                f"contract requires {expected!r}"
            )
    if problems:
        raise RuntimeError(
            "the relay's drain identities do not satisfy their contract: "
            + "; ".join(problems)
            + ". Run scripts/bootstrap_outbox_dispatcher_roles.py with elevated "
            "credentials; a migration must not create a role."
        )


def _create_tenant_plane() -> None:
    op.create_table(
        TENANT_TABLE,
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_relay_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["public.tenants.id"],
            ondelete="CASCADE",
            name="fk_outbox_events_tenant",
        ),
        schema="public",
    )
    op.create_index(
        "ix_outbox_events_status_available_at",
        TENANT_TABLE,
        ["status", "available_at"],
        schema="public",
    )
    op.create_index(
        "ix_outbox_events_status_leased_at",
        TENANT_TABLE,
        ["status", "leased_at"],
        schema="public",
    )
    op.create_index(
        "ix_outbox_events_tenant_id", TENANT_TABLE, ["tenant_id"], schema="public"
    )
    op.execute("ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY outbox_events_tenant_isolation
            ON public.outbox_events
            USING (tenant_id = public.app_current_tenant_id())
            WITH CHECK (tenant_id = public.app_current_tenant_id())
        """
    )
    # app_admin owns the SECURITY DEFINER functions, so it needs the privilege
    # they borrow. It is already the BYPASSRLS migrator, so this widens nothing
    # for an online role.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "public.outbox_events TO app_user, platform_api, app_admin"
    )


def _create_platform_plane() -> None:
    op.create_table(PLATFORM_TABLE, *_relay_columns(), schema="public")
    op.create_index(
        "ix_platform_outbox_events_status_available_at",
        PLATFORM_TABLE,
        ["status", "available_at"],
        schema="public",
    )
    op.create_index(
        "ix_platform_outbox_events_status_leased_at",
        PLATFORM_TABLE,
        ["status", "leased_at"],
        schema="public",
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "public.platform_outbox_events TO platform_api, app_admin"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE public.platform_outbox_events FROM app_user"
    )
    columns = ", ".join(_RELAY_COLUMNS)
    for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
        op.execute(
            f"REVOKE {privilege} ({columns}) ON TABLE "
            "public.platform_outbox_events FROM app_user"
        )


def _create_relay_functions() -> None:
    """The claim/settle pair per plane, mechanical by design.

    Neither function decides anything. The retry, backoff and dead-letter
    policy is computed by the Python caller in `dotmac_kernel.messaging`, which
    keeps the SECURITY DEFINER bodies small enough to read in one sitting — the
    only kind of body it is reasonable to run with a BYPASSRLS owner's
    privilege.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.claim_outbox_batch(
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
                       AND leased_at < now()
                                       - make_interval(secs => p_stale_seconds))
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
        CREATE OR REPLACE FUNCTION public.settle_outbox_event(
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.claim_platform_outbox_batch(
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
                       AND leased_at < now()
                                       - make_interval(secs => p_stale_seconds))
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
        CREATE OR REPLACE FUNCTION public.settle_platform_outbox_event(
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


def _harden_relay_functions() -> None:
    """Own as app_admin, revoke from PUBLIC, then grant EXECUTE per plane.

    Order matters. `CREATE FUNCTION` grants EXECUTE to PUBLIC by default, so a
    revision that granted the dispatcher and stopped would have handed every
    login role in the cluster a SECURITY DEFINER path past row-level security.
    """
    for signature in (
        TENANT_CLAIM_SIG,
        TENANT_SETTLE_SIG,
        PLATFORM_CLAIM_SIG,
        PLATFORM_SETTLE_SIG,
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO app_admin")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    for signature in (TENANT_CLAIM_SIG, TENANT_SETTLE_SIG):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {TENANT_DISPATCHER}")
    for signature in (PLATFORM_CLAIM_SIG, PLATFORM_SETTLE_SIG):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {PLATFORM_DISPATCHER}")
    for dispatcher in (TENANT_DISPATCHER, PLATFORM_DISPATCHER):
        op.execute(f"GRANT USAGE ON SCHEMA public TO {dispatcher}")


def upgrade() -> None:
    _assert_dispatcher_roles_exist()
    _create_tenant_plane()
    _create_platform_plane()
    if op.get_bind().dialect.name == "postgresql":
        _create_relay_functions()
        _harden_relay_functions()
    require_prerequisites(op.get_bind(), REQUIRES)


def downgrade() -> None:
    for signature in (
        TENANT_CLAIM_SIG,
        TENANT_SETTLE_SIG,
        PLATFORM_CLAIM_SIG,
        PLATFORM_SETTLE_SIG,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for dispatcher in (TENANT_DISPATCHER, PLATFORM_DISPATCHER):
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {dispatcher}")
    op.drop_table(PLATFORM_TABLE, schema="public")
    op.execute(
        "DROP POLICY IF EXISTS outbox_events_tenant_isolation ON public.outbox_events"
    )
    op.drop_table(TENANT_TABLE, schema="public")
