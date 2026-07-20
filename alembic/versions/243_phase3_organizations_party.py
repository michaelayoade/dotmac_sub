"""Phase 3 expand A: organizations + subscriber party columns.

Ports the CRM ``organizations`` / ``organization_memberships`` party model
(doc 02 §3.3, Phase 3 §1.9 — COPY verbatim, person FKs carried as plain
UUIDs) and adds the three Phase 3 subscriber columns:

* ``party_status`` — String(20) app enum (lead|contact|customer|subscriber),
  stamped by ``scripts/migration/backfill_party_status.py``.
* ``organization_id`` — FK ``organizations.id``.
* ``sales_order_id`` — plain UUID; the FK to ``sales_orders`` is added by the
  Phase 3 expand-B migration once that table exists.

Revision ID: 243_phase3_organizations_party
Revises: 242_field_note_metadata
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "243_phase3_organizations_party"
down_revision = "242_field_note_metadata"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in _inspector().get_indexes(table_name))


def _has_foreign_key(table_name: str, fk_name: str) -> bool:
    return any(
        fk["name"] == fk_name for fk in _inspector().get_foreign_keys(table_name)
    )


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _create_organizations() -> None:
    if _has_table("organizations"):
        return
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("legal_name", sa.String(length=200)),
        sa.Column("tax_id", sa.String(length=80)),
        sa.Column("domain", sa.String(length=120)),
        sa.Column("website", sa.String(length=255)),
        sa.Column("phone", sa.String(length=40)),
        sa.Column("email", sa.String(length=255)),
        sa.Column(
            "account_type",
            sa.String(length=40),
            nullable=False,
            server_default="prospect",
        ),
        sa.Column(
            "account_status",
            sa.String(length=40),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
        ),
        sa.Column("primary_contact_id", postgresql.UUID(as_uuid=True)),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True)),
        sa.Column("industry", sa.String(length=100)),
        sa.Column("employee_count", sa.String(length=40)),
        sa.Column("annual_revenue", sa.String(length=60)),
        sa.Column("source", sa.String(length=100)),
        sa.Column("address_line1", sa.String(length=120)),
        sa.Column("address_line2", sa.String(length=120)),
        sa.Column("city", sa.String(length=80)),
        sa.Column("region", sa.String(length=80)),
        sa.Column("postal_code", sa.String(length=20)),
        sa.Column("country_code", sa.String(length=2)),
        sa.Column("erp_id", sa.String(length=100)),
        sa.Column("erpnext_id", sa.String(length=100)),
        sa.Column("notes", sa.Text()),
        sa.Column("tags", sa.JSON()),
        sa.Column("commission_rate", sa.Numeric(5, 2)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("erp_id", name="uq_organizations_erp_id"),
    )
    op.create_index("ix_organizations_parent", "organizations", ["parent_id"])
    op.create_index("ix_organizations_account_type", "organizations", ["account_type"])
    op.create_index("ix_organizations_status", "organizations", ["account_status"])
    op.create_index("ix_organizations_owner", "organizations", ["owner_id"])
    op.create_index("ix_organizations_erp", "organizations", ["erp_id"])
    op.create_index(
        "ix_organizations_erpnext_id", "organizations", ["erpnext_id"], unique=True
    )


def _create_organization_memberships() -> None:
    if _has_table("organization_memberships"):
        return
    op.create_table(
        "organization_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role", sa.String(length=20), nullable=False, server_default="member"
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "person_id",
            name="uq_organization_memberships_org_person",
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    _create_organizations()
    _create_organization_memberships()

    _add_column_if_missing("subscribers", sa.Column("party_status", sa.String(20)))
    _add_column_if_missing(
        "subscribers", sa.Column("organization_id", postgresql.UUID(as_uuid=True))
    )
    _add_column_if_missing(
        "subscribers", sa.Column("sales_order_id", postgresql.UUID(as_uuid=True))
    )

    if not _has_foreign_key("subscribers", "fk_subscribers_organization_id"):
        op.create_foreign_key(
            "fk_subscribers_organization_id",
            "subscribers",
            "organizations",
            ["organization_id"],
            ["id"],
        )
    if not _has_index("subscribers", "ix_subscribers_organization_id"):
        op.create_index(
            "ix_subscribers_organization_id", "subscribers", ["organization_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if _has_index("subscribers", "ix_subscribers_organization_id"):
        op.drop_index("ix_subscribers_organization_id", table_name="subscribers")
    if _has_foreign_key("subscribers", "fk_subscribers_organization_id"):
        op.drop_constraint(
            "fk_subscribers_organization_id", "subscribers", type_="foreignkey"
        )
    for column_name in ("sales_order_id", "organization_id", "party_status"):
        _drop_column_if_present("subscribers", column_name)

    if _has_table("organization_memberships"):
        op.drop_table("organization_memberships")
    if _has_table("organizations"):
        for index_name in (
            "ix_organizations_erpnext_id",
            "ix_organizations_erp",
            "ix_organizations_owner",
            "ix_organizations_status",
            "ix_organizations_account_type",
            "ix_organizations_parent",
        ):
            if _has_index("organizations", index_name):
                op.drop_index(index_name, table_name="organizations")
        op.drop_table("organizations")
