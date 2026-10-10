"""Backfill and verify the captive access policy (backfill + verify steps).

Revision ID: 667_captive_access_policy_backfill
Revises: 666_captive_access_policy_schema
Create Date: 2026-10-10

## Why

``access.captive_access_policy`` becomes the captive decision source in the
same release. Without a backfill, every account that opted in through
``subscribers.captive_redirect_enabled`` would silently lose captive access,
and every existing lock would carry no request evidence.

## What it does

1. **Opt-ins -> account rules.** Every subscriber with
   ``captive_redirect_enabled = true`` gets exactly one ``account`` ``allow``
   rule, created by ``migration:667_captive_access_policy_backfill``. The rule
   carries the conditions the old eligibility check hard-coded
   (``subscriber_categories = ["residential"]``, ``reseller_condition =
   "house"``), so the backfill changes NO decision: an opted-in business or
   reseller-owned account stays hard reject until an operator deliberately
   widens its rule. Re-running inserts nothing (``NOT EXISTS`` guard).
2. **Lock request evidence.** ``enforcement_locks.requested_access_mode`` is
   derived, most restrictive first, from structured evidence only:
   * a lock whose effective ``access_mode`` is ``captive`` requested captive;
   * a lock linked by ``financial_access_consequence_evidence``
     (``lock_created``) to a ``reject`` consequence requested hard reject;
     linked only to ``suspend`` consequences it requested captive;
   * every other lock (admin, fraud, FUP without evidence, legacy) stays
     ``NULL`` = no structured request evidence, which the policy treats as a
     hard-reject request (fail closed, never upgraded).
3. **Verify.** Fails the upgrade unless every opted-in account has exactly one
   backfilled rule and no captive lock lacks a captive request. Then adds
   ``ck_enforcement_locks_effective_within_request`` ``NOT VALID`` and
   validates it.

Counts are recorded in one ``audit_events`` row
(``access.captive_access_policy_backfilled``) without customer identity.

## Cut over and contract

The application reads the policy from this release on;
``captive_redirect_enabled`` is no longer read by any decision path and its
admin writer is removed, but the column stays readable. Dropping it is a
later contract revision once rollback to a pre-policy image is no longer
required.

## Budgets

One INSERT ... SELECT bounded by the opted-in population (hundreds of rows in
production), two UPDATEs over active and historical locks keyed by primary
key, and one constraint validation scan of ``enforcement_locks``.
``lock_timeout = 5s`` and ``statement_timeout = 120s`` for this revision. A
timeout fails cleanly; every step is idempotent, so retry is safe.

## Downgrade

Deletes only the rules this revision created (by ``created_by``) and drops the
constraint; request evidence is cleared back to ``NULL``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "667_captive_access_policy_backfill"
down_revision: str | None = "666_captive_access_policy_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_ACTOR = "migration:667_captive_access_policy_backfill"
BACKFILL_REASON = (
    "Converted from Subscriber.captive_redirect_enabled opt-in; conditions "
    "preserve the former direct-house residential eligibility."
)
_CONSTRAINT = "ck_enforcement_locks_effective_within_request"


def _scalar(bind: sa.engine.Connection, sql: str) -> int:
    return int(bind.execute(sa.text(sql)).scalar_one() or 0)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    inserted = bind.execute(
        sa.text(
            "INSERT INTO captive_access_rules ("
            "id, scope, effect, subscriber_id, customer_set_id, plan_family, "
            "offer_ids, subscriber_categories, reseller_condition, reseller_ids, "
            "enabled, created_by, reason, created_at, updated_at"
            ") "
            "SELECT gen_random_uuid(), 'account', 'allow', s.id, NULL, NULL, "
            "NULL, CAST('[\"residential\"]' AS JSON), 'house', NULL, "
            "true, CAST(:actor AS VARCHAR(255)), CAST(:reason AS TEXT), now(), now() "
            "FROM subscribers s "
            "WHERE s.captive_redirect_enabled IS TRUE "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM captive_access_rules r "
            "  WHERE r.scope = 'account' AND r.subscriber_id = s.id "
            "  AND r.created_by = CAST(:actor AS VARCHAR(255))"
            ")"
        ),
        {"actor": BACKFILL_ACTOR, "reason": BACKFILL_REASON},
    ).rowcount

    captive_requests = bind.execute(
        sa.text(
            "UPDATE enforcement_locks SET requested_access_mode = 'captive' "
            "WHERE access_mode = 'captive' AND requested_access_mode IS NULL"
        )
    ).rowcount
    evidenced = bind.execute(
        sa.text(
            "UPDATE enforcement_locks l "
            "SET requested_access_mode = CASE WHEN e.any_reject "
            "  THEN 'hard_reject'::accessrestrictionmode "
            "  ELSE 'captive'::accessrestrictionmode END "
            "FROM ("
            "  SELECT ev.enforcement_lock_id AS lock_id, "
            "         bool_or(c.action::text = 'reject') AS any_reject "
            "  FROM financial_access_consequence_evidence ev "
            "  JOIN financial_access_consequences c ON c.id = ev.consequence_id "
            "  WHERE ev.enforcement_lock_id IS NOT NULL "
            "    AND ev.operation::text = 'lock_created' "
            "    AND c.action::text IN ('suspend', 'reject') "
            "  GROUP BY ev.enforcement_lock_id"
            ") e "
            "WHERE e.lock_id = l.id AND l.requested_access_mode IS NULL "
            "  AND l.access_mode = 'hard_reject'"
        )
    ).rowcount

    missing_rules = _scalar(
        bind,
        "SELECT count(*) FROM subscribers s "
        "WHERE s.captive_redirect_enabled IS TRUE AND ("
        "  SELECT count(*) FROM captive_access_rules r "
        "  WHERE r.scope = 'account' AND r.subscriber_id = s.id "
        f"  AND r.created_by = '{BACKFILL_ACTOR}') <> 1",
    )
    if missing_rules:
        raise RuntimeError(
            f"captive policy backfill verification failed: {missing_rules} "
            "opted-in accounts do not have exactly one backfilled rule"
        )
    captive_without_request = _scalar(
        bind,
        "SELECT count(*) FROM enforcement_locks "
        "WHERE access_mode = 'captive' "
        "AND requested_access_mode IS DISTINCT FROM 'captive'",
    )
    if captive_without_request:
        raise RuntimeError(
            "captive policy backfill verification failed: "
            f"{captive_without_request} captive locks lack a captive request"
        )
    # A fresh baseline (001_squashed builds from current model metadata)
    # already carries the constraint; an upgraded deployment does not.
    constraint_present = _scalar(
        bind,
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'enforcement_locks'::regclass "
        f"AND conname = '{_CONSTRAINT}'",
    )
    if not constraint_present:
        op.execute(
            f"ALTER TABLE enforcement_locks ADD CONSTRAINT {_CONSTRAINT} "
            "CHECK (access_mode <> 'captive' OR requested_access_mode = 'captive') "
            "NOT VALID"
        )
    op.execute(f"ALTER TABLE enforcement_locks VALIDATE CONSTRAINT {_CONSTRAINT}")

    unevidenced_active = _scalar(
        bind,
        "SELECT count(*) FROM enforcement_locks "
        "WHERE is_active AND requested_access_mode IS NULL",
    )
    bind.execute(
        sa.text(
            "INSERT INTO audit_events ("
            "id, occurred_at, actor_type, actor_id, actor_label, action, "
            "entity_type, entity_id, status_code, is_success, is_active, "
            "metadata, created_at"
            ") VALUES ("
            "CAST(:id AS UUID), now(), 'system', :actor_id, :actor_label, "
            "'access.captive_access_policy_backfilled', "
            "'captive_access_policy', NULL, 200, true, true, "
            "CAST(:metadata AS JSON), now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "actor_id": BACKFILL_ACTOR,
            "actor_label": BACKFILL_ACTOR,
            "metadata": json.dumps(
                {
                    "account_rules_inserted": int(inserted or 0),
                    "locks_marked_captive_request": int(captive_requests or 0),
                    "locks_from_consequence_evidence": int(evidenced or 0),
                    "active_locks_without_request_evidence": unevidenced_active,
                }
            ),
        },
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE enforcement_locks DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute("UPDATE enforcement_locks SET requested_access_mode = NULL")
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM captive_access_rules WHERE created_by = :actor"),
        {"actor": BACKFILL_ACTOR},
    )
