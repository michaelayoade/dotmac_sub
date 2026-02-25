"""Backfill system users from legacy subscriber records.

Revision ID: t1u2v3w4x5y6
Revises: p9q0r1s2t3u4, r6s7t8u9v0w1
Create Date: 2026-02-25 12:55:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "t1u2v3w4x5y6"
down_revision = ("p9q0r1s2t3u4", "r6s7t8u9v0w1")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Materialize legacy admin principals into system_users.
    op.execute(
        """
        INSERT INTO system_users (
            id,
            first_name,
            last_name,
            display_name,
            email,
            user_type,
            phone,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            s.id,
            s.first_name,
            s.last_name,
            s.display_name,
            s.email,
            'system_user'::usertype,
            s.phone,
            s.is_active,
            COALESCE(s.created_at, now()),
            COALESCE(s.updated_at, now())
        FROM subscribers s
        WHERE s.user_type = 'system_user'::usertype
        ON CONFLICT (id) DO UPDATE SET
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            display_name = EXCLUDED.display_name,
            email = EXCLUDED.email,
            user_type = 'system_user'::usertype,
            phone = EXCLUDED.phone,
            is_active = EXCLUDED.is_active,
            updated_at = EXCLUDED.updated_at
        """
    )

    # 2) Copy legacy role assignments to system_user_roles.
    op.execute(
        """
        INSERT INTO system_user_roles (id, system_user_id, role_id, assigned_at)
        SELECT
            sr.id,
            sr.subscriber_id,
            sr.role_id,
            sr.assigned_at
        FROM subscriber_roles sr
        JOIN subscribers s ON s.id = sr.subscriber_id
        WHERE s.user_type = 'system_user'::usertype
        ON CONFLICT (system_user_id, role_id) DO NOTHING
        """
    )

    # 3) Copy legacy direct permissions to system_user_permissions.
    op.execute(
        """
        INSERT INTO system_user_permissions (
            id,
            system_user_id,
            permission_id,
            granted_at,
            granted_by_system_user_id
        )
        SELECT
            sp.id,
            sp.subscriber_id,
            sp.permission_id,
            sp.granted_at,
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM system_users su
                    WHERE su.id = sp.granted_by_subscriber_id
                ) THEN sp.granted_by_subscriber_id
                ELSE NULL
            END
        FROM subscriber_permissions sp
        JOIN subscribers s ON s.id = sp.subscriber_id
        WHERE s.user_type = 'system_user'::usertype
        ON CONFLICT (system_user_id, permission_id) DO NOTHING
        """
    )

    # 4) Re-link auth/session/API entities to system_user_id for legacy system users.
    op.execute(
        """
        UPDATE user_credentials uc
        SET
            system_user_id = uc.subscriber_id,
            subscriber_id = NULL
        FROM subscribers s
        WHERE uc.subscriber_id = s.id
          AND s.user_type = 'system_user'::usertype
          AND uc.system_user_id IS NULL
        """
    )

    op.execute(
        """
        UPDATE mfa_methods m
        SET
            system_user_id = m.subscriber_id,
            subscriber_id = NULL
        FROM subscribers s
        WHERE m.subscriber_id = s.id
          AND s.user_type = 'system_user'::usertype
          AND m.system_user_id IS NULL
        """
    )

    op.execute(
        """
        UPDATE sessions x
        SET
            system_user_id = x.subscriber_id,
            subscriber_id = NULL
        FROM subscribers s
        WHERE x.subscriber_id = s.id
          AND s.user_type = 'system_user'::usertype
          AND x.system_user_id IS NULL
        """
    )

    op.execute(
        """
        UPDATE api_keys k
        SET
            system_user_id = k.subscriber_id,
            subscriber_id = NULL
        FROM subscribers s
        WHERE k.subscriber_id = s.id
          AND s.user_type = 'system_user'::usertype
          AND k.system_user_id IS NULL
        """
    )


def downgrade() -> None:
    # Data backfill is intentionally non-destructive and not auto-reverted.
    pass

