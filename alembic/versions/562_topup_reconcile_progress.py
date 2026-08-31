"""Add typed gateway reconciliation progress to top-up intents.

Revision ID: 562_topup_reconcile_progress
Revises: 561_customer_comm_send_perms
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "562_topup_reconcile_progress"
down_revision: str | None = "561_customer_comm_send_perms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns("topup_intents")}


def _add_columns(bind) -> None:
    columns = _column_names(bind)
    if "gateway_last_observed_at" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column("gateway_last_observed_at", sa.DateTime(timezone=True)),
        )
    if "gateway_last_outcome" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column("gateway_last_outcome", sa.String(length=40)),
        )
    if "gateway_last_reason_code" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column("gateway_last_reason_code", sa.String(length=80)),
        )
    if "gateway_next_reconcile_at" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column("gateway_next_reconcile_at", sa.DateTime(timezone=True)),
        )
    if "gateway_observation_count" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column(
                "gateway_observation_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def _backfill_postgresql(bind) -> None:
    bind.execute(
        sa.text(
            """
            WITH gateway_evidence AS (
                SELECT
                    id,
                    NULLIF("metadata"->'gateway_verification'->>'observed_at', '')
                        AS observed_at_text,
                    NULLIF("metadata"->'gateway_verification'->>'outcome', '')
                        AS outcome,
                    NULLIF("metadata"->'gateway_verification'->>'reason_code', '')
                        AS reason_code
                FROM topup_intents
                WHERE "metadata" ? 'gateway_verification'
            ),
            normalized AS (
                SELECT
                    id,
                    CASE
                        WHEN observed_at_text ~
                            '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                        THEN observed_at_text::timestamptz
                        ELSE NULL
                    END AS observed_at,
                    outcome,
                    reason_code
                FROM gateway_evidence
            )
            UPDATE topup_intents AS intent
            SET
                gateway_last_observed_at = normalized.observed_at,
                gateway_last_outcome = normalized.outcome,
                gateway_last_reason_code = normalized.reason_code,
                gateway_observation_count = CASE
                    WHEN normalized.observed_at IS NOT NULL
                        OR normalized.outcome IS NOT NULL
                        OR normalized.reason_code IS NOT NULL
                    THEN GREATEST(intent.gateway_observation_count, 1)
                    ELSE intent.gateway_observation_count
                END,
                gateway_next_reconcile_at = CASE
                    WHEN intent.completed_payment_id IS NOT NULL THEN NULL
                    WHEN normalized.observed_at IS NULL THEN NULL
                    WHEN intent.status IN ('failed', 'abandoned', 'expired')
                        OR normalized.outcome IN ('failed', 'abandoned')
                    THEN normalized.observed_at + INTERVAL '24 hours'
                    WHEN normalized.outcome IN (
                        'processing',
                        'awaiting_confirmation'
                    )
                    THEN normalized.observed_at + INTERVAL '30 minutes'
                    WHEN normalized.outcome IN ('unavailable', 'unknown')
                    THEN normalized.observed_at + INTERVAL '60 minutes'
                    ELSE intent.gateway_next_reconcile_at
                END
            FROM normalized
            WHERE intent.id = normalized.id
            """
        )
    )


def _backfill_sqlite(bind) -> None:
    bind.execute(
        sa.text(
            """
            UPDATE topup_intents
            SET
                gateway_last_observed_at = json_extract(
                    "metadata", '$.gateway_verification.observed_at'
                ),
                gateway_last_outcome = json_extract(
                    "metadata", '$.gateway_verification.outcome'
                ),
                gateway_last_reason_code = json_extract(
                    "metadata", '$.gateway_verification.reason_code'
                ),
                gateway_observation_count = CASE
                    WHEN json_extract(
                        "metadata", '$.gateway_verification.observed_at'
                    ) IS NOT NULL
                    THEN 1
                    ELSE gateway_observation_count
                END,
                gateway_next_reconcile_at = CASE
                    WHEN completed_payment_id IS NOT NULL THEN NULL
                    WHEN json_extract(
                        "metadata", '$.gateway_verification.observed_at'
                    ) IS NULL THEN NULL
                    WHEN status IN ('failed', 'abandoned', 'expired')
                        OR json_extract(
                            "metadata", '$.gateway_verification.outcome'
                        ) IN ('failed', 'abandoned')
                    THEN datetime(
                        json_extract(
                            "metadata", '$.gateway_verification.observed_at'
                        ),
                        '+24 hours'
                    )
                    WHEN json_extract(
                        "metadata", '$.gateway_verification.outcome'
                    ) IN ('processing', 'awaiting_confirmation')
                    THEN datetime(
                        json_extract(
                            "metadata", '$.gateway_verification.observed_at'
                        ),
                        '+30 minutes'
                    )
                    WHEN json_extract(
                        "metadata", '$.gateway_verification.outcome'
                    ) IN ('unavailable', 'unknown')
                    THEN datetime(
                        json_extract(
                            "metadata", '$.gateway_verification.observed_at'
                        ),
                        '+60 minutes'
                    )
                    ELSE gateway_next_reconcile_at
                END
            WHERE json_valid("metadata")
              AND json_extract("metadata", '$.gateway_verification') IS NOT NULL
            """
        )
    )


def _backfill_progress(bind) -> None:
    if bind.dialect.name == "postgresql":
        _backfill_postgresql(bind)
    elif bind.dialect.name == "sqlite":
        _backfill_sqlite(bind)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "topup_intents" not in tables:
        return
    _add_columns(bind)
    _backfill_progress(bind)
    op.create_index(
        "ix_topup_intents_gateway_reconcile_due",
        "topup_intents",
        [
            "provider_type",
            "status",
            "completed_payment_id",
            "gateway_next_reconcile_at",
            "created_at",
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "topup_intents" not in tables:
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("topup_intents")}
    if "ix_topup_intents_gateway_reconcile_due" in indexes:
        op.drop_index(
            "ix_topup_intents_gateway_reconcile_due",
            table_name="topup_intents",
        )
    columns = _column_names(bind)
    for column in (
        "gateway_observation_count",
        "gateway_next_reconcile_at",
        "gateway_last_reason_code",
        "gateway_last_outcome",
        "gateway_last_observed_at",
    ):
        if column in columns:
            op.drop_column("topup_intents", column)
