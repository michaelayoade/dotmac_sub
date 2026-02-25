"""Add system users and split auth principals.

Revision ID: p9q0r1s2t3u4
Revises: n4p5q6r7s8t9
Create Date: 2026-02-25 12:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "p9q0r1s2t3u4"
down_revision = "n4p5q6r7s8t9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    user_type_enum = postgresql.ENUM(
        "system_user",
        "customer",
        "reseller",
        name="usertype",
        create_type=False,
    )

    op.create_table(
        "system_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "user_type",
            user_type_enum,
            nullable=False,
            server_default="system_user",
        ),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_system_users_email"),
    )

    op.create_table(
        "system_user_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.ForeignKeyConstraint(["system_user_id"], ["system_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("system_user_id", "role_id", name="uq_system_user_roles_user_role"),
    )

    op.create_table(
        "system_user_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"]),
        sa.ForeignKeyConstraint(["system_user_id"], ["system_users.id"]),
        sa.ForeignKeyConstraint(["granted_by_system_user_id"], ["system_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "system_user_id",
            "permission_id",
            name="uq_system_user_permissions_user_permission",
        ),
    )

    op.add_column(
        "user_credentials",
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_credentials_system_user_id_system_users",
        "user_credentials",
        "system_users",
        ["system_user_id"],
        ["id"],
    )
    op.alter_column("user_credentials", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        "ck_user_credentials_exactly_one_principal",
        "user_credentials",
        "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
    )

    op.add_column(
        "mfa_methods",
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_mfa_methods_system_user_id_system_users",
        "mfa_methods",
        "system_users",
        ["system_user_id"],
        ["id"],
    )
    op.alter_column("mfa_methods", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_index(
        "ix_mfa_methods_primary_per_system_user",
        "mfa_methods",
        ["system_user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_check_constraint(
        "ck_mfa_methods_exactly_one_principal",
        "mfa_methods",
        "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
    )

    op.add_column(
        "sessions",
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sessions_system_user_id_system_users",
        "sessions",
        "system_users",
        ["system_user_id"],
        ["id"],
    )
    op.alter_column("sessions", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        "ck_sessions_exactly_one_principal",
        "sessions",
        "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
    )

    op.add_column(
        "api_keys",
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_keys_system_user_id_system_users",
        "api_keys",
        "system_users",
        ["system_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_api_keys_system_user_id_system_users", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "system_user_id")

    op.drop_constraint("ck_sessions_exactly_one_principal", "sessions", type_="check")
    op.alter_column("sessions", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_constraint("fk_sessions_system_user_id_system_users", "sessions", type_="foreignkey")
    op.drop_column("sessions", "system_user_id")

    op.drop_constraint("ck_mfa_methods_exactly_one_principal", "mfa_methods", type_="check")
    op.drop_index("ix_mfa_methods_primary_per_system_user", table_name="mfa_methods")
    op.alter_column("mfa_methods", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_constraint("fk_mfa_methods_system_user_id_system_users", "mfa_methods", type_="foreignkey")
    op.drop_column("mfa_methods", "system_user_id")

    op.drop_constraint("ck_user_credentials_exactly_one_principal", "user_credentials", type_="check")
    op.alter_column("user_credentials", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_constraint(
        "fk_user_credentials_system_user_id_system_users",
        "user_credentials",
        type_="foreignkey",
    )
    op.drop_column("user_credentials", "system_user_id")

    op.drop_table("system_user_permissions")
    op.drop_table("system_user_roles")
    op.drop_table("system_users")
