"""one active primary IPv4 assignment per exact service

``uq_ip_assignments_ipv4_active`` (migration 177) stops two services sharing one
address. Nothing stopped one service holding two, so a consumer asking "which
address does this service own?" had no answer and
``radius_population.populate()`` answered it by unordered query position — an
ownership decision the projection does not hold, and one that could differ
between two runs over identical data.

This adds the missing half of the invariant, so the question becomes
unanswerable-by-construction rather than answered by accident.

PREREQUISITE — this migration FAILS CLOSED. A duplicate active exact-service
IPv4 assignment is an ownership question with a customer-visible answer (which
address is that service actually served on?). It is not something a schema
migration may silently resolve by deleting or deactivating a row, so this
refuses to proceed and names the offenders instead. Adjudicate each one through
``network.ip_assignment_lifecycle``'s reviewed repair command, then re-run.

Revision ID: 452_one_active_ipv4_assignment_per_service
Revises: 451_add_nas_local_secret_retire_operation_type
Create Date: 2026-08-01
"""

import sqlalchemy as sa

from alembic import op

revision = "452_one_active_ipv4_assignment_per_service"
down_revision = "451_add_nas_local_secret_retire_operation_type"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_ip_assignments_subscription_ipv4_active"
_PREDICATE = "is_active AND ipv4_address_id IS NOT NULL AND subscription_id IS NOT NULL"


def _violations(conn) -> list[tuple[str, int, str]]:
    return list(
        conn.execute(
            sa.text(
                """
                SELECT a.subscription_id::text,
                       count(*) AS active_count,
                       string_agg(v.address, ', ' ORDER BY v.address) AS addresses
                FROM ip_assignments a
                JOIN ipv4_addresses v ON v.id = a.ipv4_address_id
                WHERE a.is_active
                  AND a.ipv4_address_id IS NOT NULL
                  AND a.subscription_id IS NOT NULL
                GROUP BY a.subscription_id
                HAVING count(*) > 1
                ORDER BY count(*) DESC
                """
            )
        )
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name.startswith("postgres"):
        offenders = _violations(conn)
        if offenders:
            listing = "\n  ".join(
                f"subscription {row[0]}: {row[1]} active assignments ({row[2]})"
                for row in offenders
            )
            raise RuntimeError(
                f"Refusing to add {INDEX_NAME}: {len(offenders)} subscription(s) "
                "hold more than one active IPv4 assignment. Which address the "
                "service is served on is an ownership decision, so this "
                "migration will not pick one.\n  "
                + listing
                + "\n\nAdjudicate each through the reviewed assignment repair "
                "command (network.ip_assignment_lifecycle), then re-run."
            )
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
            f"ON ip_assignments (subscription_id) WHERE {_PREDICATE}"
        )
    else:
        op.create_index(
            INDEX_NAME,
            "ip_assignments",
            ["subscription_id"],
            unique=True,
            sqlite_where=sa.text(_PREDICATE),
        )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
