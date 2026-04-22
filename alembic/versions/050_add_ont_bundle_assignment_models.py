"""add ONT bundle assignment and override models

Revision ID: 050_add_ont_bundle_assignment_models
Revises: 049_scope_ont_vlan_refs_to_olt
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "050_add_ont_bundle_assignment_models"
down_revision = "049_scope_ont_vlan_refs_to_olt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    ontbundleassignmentstatus = postgresql.ENUM(
        "draft",
        "planned",
        "applying",
        "applied",
        "drifted",
        "failed",
        "superseded",
        name="ontbundleassignmentstatus",
        create_type=False,
    )
    ontconfigoverridesource = postgresql.ENUM(
        "operator",
        "workflow",
        "subscriber_data",
        name="ontconfigoverridesource",
        create_type=False,
    )
    ontbundlekind = postgresql.ENUM(
        "residential",
        "business",
        "voice",
        "bridge",
        "custom",
        name="ontbundlekind",
        create_type=False,
    )
    ontbundleassignmentstatus.create(bind, checkfirst=True)
    ontconfigoverridesource.create(bind, checkfirst=True)
    ontbundlekind.create(bind, checkfirst=True)

    onu_type_columns = {column["name"] for column in inspector.get_columns("onu_types")}
    if "vendor_model_capability_id" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "vendor_model_capability_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
        op.create_foreign_key(
            "fk_onu_types_vendor_model_capability_id",
            "onu_types",
            "vendor_model_capabilities",
            ["vendor_model_capability_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "default_bundle_id" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column("default_bundle_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_onu_types_default_bundle_id",
            "onu_types",
            "ont_provisioning_profiles",
            ["default_bundle_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "supports_bundle_overrides" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "supports_bundle_overrides",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )

    profile_columns = {
        column["name"] for column in inspector.get_columns("ont_provisioning_profiles")
    }
    if "bundle_kind" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("bundle_kind", ontbundlekind, nullable=True),
        )
    if "ont_type_id" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("ont_type_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_ont_provisioning_profiles_ont_type_id",
            "ont_provisioning_profiles",
            "onu_types",
            ["ont_type_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "cloned_from_bundle_id" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("cloned_from_bundle_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_ont_provisioning_profiles_cloned_from_bundle_id",
            "ont_provisioning_profiles",
            "ont_provisioning_profiles",
            ["cloned_from_bundle_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "execution_policy" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("execution_policy", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        )
    if "required_capabilities" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column(
                "required_capabilities",
                postgresql.JSON(astext_type=sa.Text()),
                nullable=True,
            ),
        )
    if "supports_manual_override" not in profile_columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column(
                "supports_manual_override",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )

    op.execute(
        """
        UPDATE ont_provisioning_profiles
        SET bundle_kind = CASE profile_type
            WHEN 'residential' THEN 'residential'::ontbundlekind
            WHEN 'business' THEN 'business'::ontbundlekind
            ELSE 'custom'::ontbundlekind
        END
        WHERE bundle_kind IS NULL
        """
    )

    if "ont_bundle_assignments" not in inspector.get_table_names():
        op.create_table(
            "ont_bundle_assignments",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("bundle_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "assigned_by_subscriber_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column(
                "status",
                ontbundleassignmentstatus,
                nullable=False,
                server_default="draft",
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("assigned_reason", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["ont_unit_id"], ["ont_units.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["bundle_id"],
                ["ont_provisioning_profiles.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["assigned_by_subscriber_id"],
                ["subscribers.id"],
                ondelete="SET NULL",
            ),
        )
        op.create_index(
            "ix_ont_bundle_assignments_ont_status",
            "ont_bundle_assignments",
            ["ont_unit_id", "status"],
        )
        op.create_index(
            "ix_ont_bundle_assignments_bundle_status",
            "ont_bundle_assignments",
            ["bundle_id", "status"],
        )
        op.create_index(
            "uq_ont_bundle_assignments_active_ont",
            "ont_bundle_assignments",
            ["ont_unit_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
        )

    if "ont_config_overrides" not in inspector.get_table_names():
        op.create_table(
            "ont_config_overrides",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("field_name", sa.String(length=120), nullable=False),
            sa.Column(
                "value_json",
                postgresql.JSON(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column(
                "source",
                ontconfigoverridesource,
                nullable=False,
                server_default="operator",
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["ont_unit_id"], ["ont_units.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "ont_unit_id",
                "field_name",
                name="uq_ont_config_overrides_ont_field",
            ),
        )
        op.create_index(
            "ix_ont_config_overrides_field_name",
            "ont_config_overrides",
            ["field_name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "ont_config_overrides" in inspector.get_table_names():
        op.drop_index(
            "ix_ont_config_overrides_field_name",
            table_name="ont_config_overrides",
        )
        op.drop_table("ont_config_overrides")

    if "ont_bundle_assignments" in inspector.get_table_names():
        op.drop_index(
            "uq_ont_bundle_assignments_active_ont",
            table_name="ont_bundle_assignments",
        )
        op.drop_index(
            "ix_ont_bundle_assignments_bundle_status",
            table_name="ont_bundle_assignments",
        )
        op.drop_index(
            "ix_ont_bundle_assignments_ont_status",
            table_name="ont_bundle_assignments",
        )
        op.drop_table("ont_bundle_assignments")

    profile_columns = {
        column["name"] for column in inspector.get_columns("ont_provisioning_profiles")
    }
    foreign_keys = {
        fk["name"] for fk in inspector.get_foreign_keys("ont_provisioning_profiles")
    }
    if (
        "fk_ont_provisioning_profiles_cloned_from_bundle_id" in foreign_keys
        and "cloned_from_bundle_id" in profile_columns
    ):
        op.drop_constraint(
            "fk_ont_provisioning_profiles_cloned_from_bundle_id",
            "ont_provisioning_profiles",
            type_="foreignkey",
        )
    if (
        "fk_ont_provisioning_profiles_ont_type_id" in foreign_keys
        and "ont_type_id" in profile_columns
    ):
        op.drop_constraint(
            "fk_ont_provisioning_profiles_ont_type_id",
            "ont_provisioning_profiles",
            type_="foreignkey",
        )
    for column_name in (
        "supports_manual_override",
        "required_capabilities",
        "execution_policy",
        "cloned_from_bundle_id",
        "ont_type_id",
        "bundle_kind",
    ):
        if column_name in profile_columns:
            op.drop_column("ont_provisioning_profiles", column_name)

    onu_type_columns = {column["name"] for column in inspector.get_columns("onu_types")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("onu_types")}
    if "fk_onu_types_default_bundle_id" in foreign_keys and "default_bundle_id" in onu_type_columns:
        op.drop_constraint(
            "fk_onu_types_default_bundle_id",
            "onu_types",
            type_="foreignkey",
        )
    if (
        "fk_onu_types_vendor_model_capability_id" in foreign_keys
        and "vendor_model_capability_id" in onu_type_columns
    ):
        op.drop_constraint(
            "fk_onu_types_vendor_model_capability_id",
            "onu_types",
            type_="foreignkey",
        )
    for column_name in (
        "supports_bundle_overrides",
        "default_bundle_id",
        "vendor_model_capability_id",
    ):
        if column_name in onu_type_columns:
            op.drop_column("onu_types", column_name)

    postgresql.ENUM(name="ontbundlekind").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ontconfigoverridesource").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ontbundleassignmentstatus").drop(bind, checkfirst=True)
