"""Field-mobile OIDC ceremony records and the subject-uniqueness ratchet.

Revision ID: 560_oidc_mobile_federation
Revises: 559_upcoming_charges_indexes
Create Date: 2026-08-27

Two additive objects, no authority moves. Sub remains the session authority;
the identity provider becomes an assertion transport and nothing more.

1. ``oidc_mobile_ceremonies`` — the short-lived, single-use server half of one
   ceremony. It stores a nonce HASH and never the nonce, and there is no column
   for a PKCE verifier or an authorization code because Sub never receives
   either. ``uq_oidc_mobile_ceremonies_nonce_hash`` is what makes "one nonce,
   one ceremony, ever" a database fact rather than an application convention.

2. ``ux_user_credentials_external_subject`` — a PARTIAL UNIQUE index over
   ``(authentication_binding_id, username)`` for non-local credentials. The
   existing uniqueness is ``(tenant_id, party_id, authentication_binding_id)``,
   which says a party holds at most one credential per verifier. It does NOT
   say a SUBJECT maps to at most one party, so without this index two parties
   could both claim external subject ``S`` at one issuer and the exchange's
   lookup would pick between them arbitrarily. The resolver additionally
   refuses a multi-row match, but a resolver check is a convention and this is
   the constraint.

The index is deliberately keyed on the binding rather than the issuer string:
two issuers are two bindings (``authentication_bindings`` docstring), so
per-binding uniqueness is per-issuer uniqueness without duplicating the issuer
onto every credential row.

No table here carries ``tenant_id`` or an RLS policy, matching every other Sub
table: Sub is a single-operator-tenant deployment (ADR-0009) and its
authoritative inputs — ``user_credentials``, ``authentication_bindings``,
``system_users`` — carry none either.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "560_oidc_mobile_federation"
down_revision: str | None = "559_upcoming_charges_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "oidc_mobile_ceremonies"
_SUBJECT_INDEX = "ux_user_credentials_external_subject"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("binding_key", sa.String(length=80), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("deployment_id", sa.String(length=120), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("nonce_hash", name="uq_oidc_mobile_ceremonies_nonce_hash"),
        sa.CheckConstraint(
            "length(trim(nonce_hash)) = 64",
            name="ck_oidc_mobile_ceremonies_nonce_hash_shape",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_mobile_ceremonies_expiry_after_start",
        ),
        sa.CheckConstraint(
            "(outcome = 'pending' AND consumed_at IS NULL) "
            "OR (outcome <> 'pending' AND consumed_at IS NOT NULL)",
            name="ck_oidc_mobile_ceremonies_outcome_alignment",
        ),
    )
    op.create_index(
        "ix_oidc_mobile_ceremonies_expiry_sweep",
        _TABLE,
        ["outcome", "expires_at"],
    )
    op.create_index(
        "ix_oidc_mobile_ceremonies_device",
        _TABLE,
        ["device_id", "created_at"],
    )

    bind = op.get_bind()
    predicate = (
        "provider <> 'local' AND authentication_binding_id IS NOT NULL "
        "AND username IS NOT NULL"
    )
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_SUBJECT_INDEX} "
                "ON user_credentials (authentication_binding_id, username) "
                f"WHERE {predicate}"
            )
        return
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_SUBJECT_INDEX} "
        "ON user_credentials (authentication_binding_id, username) "
        f"WHERE {predicate}"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_SUBJECT_INDEX}")
    else:
        op.execute(f"DROP INDEX IF EXISTS {_SUBJECT_INDEX}")
    op.drop_index("ix_oidc_mobile_ceremonies_device", table_name=_TABLE)
    op.drop_index("ix_oidc_mobile_ceremonies_expiry_sweep", table_name=_TABLE)
    op.drop_table(_TABLE)
