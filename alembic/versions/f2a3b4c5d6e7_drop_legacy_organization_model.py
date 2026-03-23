"""Drop legacy organization model and columns.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name: str) -> set[str]:
    if not _has_table(inspector, table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _fk_names(inspector, table_name: str) -> set[str]:
    if not _has_table(inspector, table_name):
        return set()
    return {fk["name"] for fk in inspector.get_foreign_keys(table_name)}


def _index_names(inspector, table_name: str) -> set[str]:
    if not _has_table(inspector, table_name):
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _unique_names(inspector, table_name: str) -> set[str]:
    if not _has_table(inspector, table_name):
        return set()
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
    }


def _assert_no_legacy_gaps(bind) -> None:
    checks = [
        (
            "stored_files",
            """
            SELECT count(*)
            FROM stored_files
            WHERE organization_id IS NOT NULL
              AND owner_subscriber_id IS NULL
            """,
            "stored_files.organization_id still has rows without owner_subscriber_id",
        ),
        (
            "pop_sites",
            """
            SELECT count(*)
            FROM pop_sites
            WHERE organization_id IS NOT NULL
              AND owner_subscriber_id IS NULL
            """,
            "pop_sites.organization_id still has rows without owner_subscriber_id",
        ),
        (
            "ont_provisioning_profiles",
            """
            SELECT count(*)
            FROM ont_provisioning_profiles
            WHERE organization_id IS NOT NULL
              AND owner_subscriber_id IS NULL
            """,
            "ont_provisioning_profiles.organization_id still has rows without owner_subscriber_id",
        ),
    ]
    inspector = inspect(bind)
    for table_name, sql, message in checks:
        if not _has_table(inspector, table_name):
            continue
        count = bind.execute(text(sql)).scalar() or 0
        if count:
            raise RuntimeError(f"{message}: {count}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    _assert_no_legacy_gaps(bind)

    if _has_table(inspector, "stored_files"):
        foreign_keys = _fk_names(inspector, "stored_files")
        indexes = _index_names(inspector, "stored_files")
        columns = _column_names(inspector, "stored_files")
        if "stored_files_organization_id_fkey" in foreign_keys:
            op.drop_constraint(
                "stored_files_organization_id_fkey",
                "stored_files",
                type_="foreignkey",
            )
        if "ix_stored_files_org_active" in indexes:
            op.drop_index("ix_stored_files_org_active", table_name="stored_files")
        if "organization_id" in columns:
            op.drop_column("stored_files", "organization_id")

    inspector = inspect(bind)
    if _has_table(inspector, "pop_sites"):
        foreign_keys = _fk_names(inspector, "pop_sites")
        columns = _column_names(inspector, "pop_sites")
        if "fk_pop_sites_organization_id" in foreign_keys:
            op.drop_constraint(
                "fk_pop_sites_organization_id",
                "pop_sites",
                type_="foreignkey",
            )
        elif "pop_sites_organization_id_fkey" in foreign_keys:
            op.drop_constraint(
                "pop_sites_organization_id_fkey",
                "pop_sites",
                type_="foreignkey",
            )
        if "organization_id" in columns:
            op.drop_column("pop_sites", "organization_id")

    inspector = inspect(bind)
    if _has_table(inspector, "ont_provisioning_profiles"):
        foreign_keys = _fk_names(inspector, "ont_provisioning_profiles")
        unique_constraints = _unique_names(inspector, "ont_provisioning_profiles")
        columns = _column_names(inspector, "ont_provisioning_profiles")
        if "uq_ont_prov_profiles_org_name" in unique_constraints:
            op.drop_constraint(
                "uq_ont_prov_profiles_org_name",
                "ont_provisioning_profiles",
                type_="unique",
            )
        if "ont_provisioning_profiles_organization_id_fkey" in foreign_keys:
            op.drop_constraint(
                "ont_provisioning_profiles_organization_id_fkey",
                "ont_provisioning_profiles",
                type_="foreignkey",
            )
        if "organization_id" in columns:
            op.drop_column("ont_provisioning_profiles", "organization_id")

    inspector = inspect(bind)
    if _has_table(inspector, "subscribers"):
        foreign_keys = _fk_names(inspector, "subscribers")
        columns = _column_names(inspector, "subscribers")
        if "subscribers_organization_id_fkey" in foreign_keys:
            op.drop_constraint(
                "subscribers_organization_id_fkey",
                "subscribers",
                type_="foreignkey",
            )
        if "organization_id" in columns:
            op.drop_column("subscribers", "organization_id")

    inspector = inspect(bind)
    if _has_table(inspector, "organizations"):
        foreign_keys = _fk_names(inspector, "organizations")
        if "organizations_primary_login_subscriber_id_fkey" in foreign_keys:
            op.drop_constraint(
                "organizations_primary_login_subscriber_id_fkey",
                "organizations",
                type_="foreignkey",
            )
        op.drop_table("organizations")


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade is not supported for the legacy organization model drop."
    )
