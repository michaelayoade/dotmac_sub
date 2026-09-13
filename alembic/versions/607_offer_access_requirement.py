"""Add offer_versions.access_requirement (Release 1 of an expand/contract rollout).

service_intent.offer_access_requirement is the sole owner of this field:
admitted only at OfferVersion creation and immutable thereafter. The server
default of ``unclassified`` exists ONLY to initialize historical rows created
before this migration; ``app/services/catalog/offer_access_requirement.py``
requires the value explicitly on every new admission and never relies on this
default. Release 2 (rejecting ``unclassified`` at admission and dropping the
default) is separate, later work.

Also adds ``offer_access_requirement_classifications``, the reviewed
classification command's own append-only record: at most one row per offer
version, used to distinguish an exact idempotent replay from a genuine
real-to-real or real-to-unclassified refusal.

Downgrade note: dropping this column after real classifications exist would
destroy them irrecoverably. This migration's downgrade refuses (fails closed)
whenever any row has left ``unclassified`` or any classification row exists;
repair forward instead of downgrading past real data.

Revision ID: 607_offer_access_requirement
Revises: 606_project_task_subtasks
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "607_offer_access_requirement"
down_revision: str | None = "606_project_task_subtasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_NAME = "access_requirement"
_ENUM_VALUES = ("network_access", "no_network_access", "unclassified")
_ENUM_TYPE = postgresql.ENUM(*_ENUM_VALUES, name=_ENUM_NAME, create_type=False)


class DowngradeRefused(RuntimeError):
    """Raised when downgrading would silently destroy real classifications."""


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '15min'")
    try:
        if is_postgres:
            postgresql.ENUM(*_ENUM_VALUES, name=_ENUM_NAME).create(
                bind, checkfirst=True
            )
        op.add_column(
            "offer_versions",
            sa.Column(
                "access_requirement",
                _ENUM_TYPE if is_postgres else sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME),
                nullable=False,
                server_default="unclassified",
            ),
        )
    finally:
        if is_postgres:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")

    op.create_table(
        "offer_access_requirement_classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "offer_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("offer_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "previous_access_requirement",
            _ENUM_TYPE if is_postgres else sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME),
            nullable=False,
        ),
        sa.Column(
            "new_access_requirement",
            _ENUM_TYPE if is_postgres else sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME),
            nullable=False,
        ),
        sa.Column("review_reference", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("classified_by", sa.String(length=120), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "offer_version_id",
            name="uq_offer_access_requirement_classifications_one_per_version",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_offer_access_requirement_classifications_idempotency_key",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())

    if "offer_access_requirement_classifications" in table_names:
        classified_count = bind.execute(
            sa.text("SELECT count(*) FROM offer_access_requirement_classifications")
        ).scalar()
        if classified_count:
            raise DowngradeRefused(
                f"{classified_count} reviewed classification(s) exist; "
                "downgrading would destroy them irrecoverably. Repair "
                "forward instead of downgrading past real data."
            )
        op.drop_table("offer_access_requirement_classifications")

    if "offer_versions" in table_names:
        columns = {
            column["name"] for column in sa.inspect(bind).get_columns("offer_versions")
        }
        if "access_requirement" in columns:
            real_count = bind.execute(
                sa.text(
                    "SELECT count(*) FROM offer_versions "
                    "WHERE access_requirement <> 'unclassified'"
                )
            ).scalar()
            if real_count:
                raise DowngradeRefused(
                    f"{real_count} offer version(s) already carry a real "
                    "access_requirement; downgrading would destroy that "
                    "classification irrecoverably. Repair forward instead of "
                    "downgrading past real data."
                )
            op.drop_column("offer_versions", "access_requirement")

    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name=_ENUM_NAME).drop(bind, checkfirst=True)
