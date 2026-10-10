"""Add user_credentials.credential_version and its standing trigger.

Revision ID: 669_user_credential_version
Revises: 668_sole_approver_adjudication_evidence
Create Date: 2026-10-10

## Why

Password success, MFA completion and session issuance used to be separate
transactions that never re-checked the credential, so a stale verification
could mint a session after a reset. ``credential_version`` names the secret
and its standing; the login commit gate and MFA challenges bind to it.

``password_hash`` is only a *representation* of the secret. A
representation-only rehash (same secret, new scheme/params; a later change)
must not invalidate a parallel login or a pending MFA challenge, so it does
not bump the version.

## What it does

1. ``user_credentials.credential_version BIGINT NOT NULL DEFAULT 1`` (metadata
   only on PostgreSQL 11+; existing rows read as 1).
2. ``CHECK (credential_version >= 1)`` added ``NOT VALID`` then validated.
3. ``BEFORE UPDATE`` trigger, the backstop for every writer that does not bump
   explicitly (the admin API ``setattr`` loop, web helpers, scripts, rolling
   deploys): it refuses a decrease and AUTO-BUMPS when standing changed
   without a bump. Standing = password_hash (unless the transaction-local GUC
   ``app.credential_change_kind`` is ``representation``), ``is_active``,
   ``must_change_password`` false->true, ``provider``, ``username`` and the
   principal FKs. It auto-bumps instead of raising so old code stays
   compatible and any unknown change fails SAFE (challenges invalidated).

## Budgets / deploy order

``lock_timeout = 5s`` so a busy ``user_credentials`` fails the upgrade
cleanly; retry is safe. Deploy order: migration, then app. Roll back the app
first; the column and trigger are harmless to old code. Downgrade drops
exactly what upgrade created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "669_user_credential_version"
down_revision: str | None = "668_sole_approver_adjudication_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_user_credentials_credential_version_positive"
_FUNCTION = "user_credentials_credential_version_guard"
_TRIGGER = "trg_user_credentials_credential_version"

_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.credential_version < OLD.credential_version THEN
    RAISE EXCEPTION 'credential_version must not decrease' USING ERRCODE = '23514';
  END IF;
  IF NEW.credential_version = OLD.credential_version AND (
       (NEW.password_hash IS DISTINCT FROM OLD.password_hash
          AND coalesce(current_setting('app.credential_change_kind', true), '')
              <> 'representation')
    OR NEW.is_active IS DISTINCT FROM OLD.is_active
    OR (NEW.must_change_password IS TRUE AND OLD.must_change_password IS NOT TRUE)
    OR NEW.provider IS DISTINCT FROM OLD.provider
    OR NEW.username IS DISTINCT FROM OLD.username
    OR NEW.subscriber_id IS DISTINCT FROM OLD.subscriber_id
    OR NEW.system_user_id IS DISTINCT FROM OLD.system_user_id
    OR NEW.reseller_user_id IS DISTINCT FROM OLD.reseller_user_id)
  THEN
    NEW.credential_version := OLD.credential_version + 1;
  END IF;
  RETURN NEW;
END
$fn$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "user_credentials",
        sa.Column(
            "credential_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        f"ALTER TABLE user_credentials ADD CONSTRAINT {_CHECK} "
        "CHECK (credential_version >= 1) NOT VALID"
    )
    op.execute(f"ALTER TABLE user_credentials VALIDATE CONSTRAINT {_CHECK}")
    op.execute(_FUNCTION_SQL)
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON user_credentials "
        f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON user_credentials")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    op.execute(f"ALTER TABLE user_credentials DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.drop_column("user_credentials", "credential_version")
