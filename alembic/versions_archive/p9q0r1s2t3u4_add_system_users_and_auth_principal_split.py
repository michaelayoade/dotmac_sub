"""Add system users and split auth principals.

Revision ID: p9q0r1s2t3u4
Revises: n4p5q6r7s8t9
Create Date: 2026-02-25 12:10:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

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
    # subscriber_id may still be named person_id if the rename migration
    # hasn't run yet (fresh DB ordering). Handle both cases.
    bind = op.get_bind()
    uc_cols = {c["name"] for c in sa.inspect(bind).get_columns("user_credentials")}
    if "subscriber_id" in uc_cols:
        op.alter_column("user_credentials", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        principal_col = "subscriber_id"
    elif "person_id" in uc_cols:
        op.alter_column("user_credentials", "person_id", new_column_name="subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        principal_col = "subscriber_id"
    else:
        principal_col = None

    if principal_col:
        op.create_check_constraint(
            "ck_user_credentials_exactly_one_principal",
            "user_credentials",
            f"({principal_col} IS NOT NULL) <> (system_user_id IS NOT NULL)",
        )

    # Fix mfa_methods — may still have person_id instead of subscriber_id
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
    mfa_cols = {c["name"] for c in sa.inspect(bind).get_columns("mfa_methods")}
    if "subscriber_id" in mfa_cols:
        op.alter_column("mfa_methods", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        mfa_principal = "subscriber_id"
    elif "person_id" in mfa_cols:
        op.alter_column("mfa_methods", "person_id", new_column_name="subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        mfa_principal = "subscriber_id"
    else:
        mfa_principal = None
    op.create_index(
        "ix_mfa_methods_primary_per_system_user",
        "mfa_methods",
        ["system_user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    if mfa_principal:
        op.create_check_constraint(
            "ck_mfa_methods_exactly_one_principal",
            "mfa_methods",
            f"({mfa_principal} IS NOT NULL) <> (system_user_id IS NOT NULL)",
        )

    # Fix sessions — may still have person_id instead of subscriber_id
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
    sess_cols = {c["name"] for c in sa.inspect(bind).get_columns("sessions")}
    if "subscriber_id" in sess_cols:
        op.alter_column("sessions", "subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        sess_principal = "subscriber_id"
    elif "person_id" in sess_cols:
        op.alter_column("sessions", "person_id", new_column_name="subscriber_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        sess_principal = "subscriber_id"
    else:
        sess_principal = None
    if sess_principal:
        op.create_check_constraint(
            "ck_sessions_exactly_one_principal",
            "sessions",
            f"({sess_principal} IS NOT NULL) <> (system_user_id IS NOT NULL)",
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
