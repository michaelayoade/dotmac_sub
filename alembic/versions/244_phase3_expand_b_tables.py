"""Phase 3 expand B: all Phase 3 native tables + indexes + FK-drops.

One Alembic set (Phase 3 §6 PR 2) creating the native tables for the four
CRM verticals per the §1.1 table map — projects (9 tables), leads/pipeline
(3), quotes (2), sales orders (2), referrals (2), plus ``work_links`` — and
promoting ``subscribers.sales_order_id`` (added as a plain UUID by expand A)
to a real FK now that ``sales_orders`` exists.

Schema conventions (§1.7/§1.8):

* every CRM PG enum lands as a String column + app-level enum;
* customer-party person FKs re-point at sub ``subscribers.id``
  (leads/quotes/sales_orders ``subscriber_id``, referral subscriber columns);
* staff person FKs, Phase 4 agent/campaign FKs, and Phase 5 inventory FKs
  are dropped — plain UUID columns;
* ``project_tasks.work_order_id`` stays a plain UUID until the Phase 2
  work-order flip adds the FK;
* the CRM partial uniques are recreated on the re-pointed columns:
  ``uq_leads_one_open_per_subscriber_pipeline`` (expression index, PG only)
  and ``uq_referrals_active_referred_subscriber``.

Deferred on purpose: the ``support_tickets.lead_id`` FK is added NOT VALID +
VALIDATE by the backfill only after the leads import (§1.10/§3.5 step 3 —
the column already carries CRM lead UUIDs that would dangle against the
empty table created here), and the ``document_sequences``
``sales_order_number`` row is inserted by the backfill (§1.5). Mirror tables
(project_mirror/quote_mirror/referral_mirror) are untouched — native tables
coexist until the contract PR (§3.3).

Revision ID: 244_phase3_expand_b_tables
Revises: 243_phase3_organizations_party
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "244_phase3_expand_b_tables"
down_revision = "243_phase3_organizations_party"
branch_labels = None
depends_on = None

_LEADS_OPEN_UNIQUE = "uq_leads_one_open_per_subscriber_pipeline"
_UUID_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in _inspector().get_indexes(table_name))


def _column_fk_names(table_name: str, column_name: str) -> list[str]:
    """Names of FK constraints constraining ``column_name`` on ``table_name``.

    Matched by column rather than name: fresh databases build the schema from
    model metadata via 001's ``create_all``, which auto-names the constraint
    (``subscribers_sales_order_id_fkey``) — the guards must recognize it too.
    """
    return [
        fk["name"]
        for fk in _inspector().get_foreign_keys(table_name)
        if fk["constrained_columns"] == [column_name] and fk["name"]
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _create_pipelines() -> None:
    if _has_table("pipelines"):
        return
    op.create_table(
        "pipelines",
        _uuid_pk(),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", sa.JSON()),
        *_timestamps(),
    )


def _create_pipeline_stages() -> None:
    if _has_table("pipeline_stages"):
        return
    op.create_table(
        "pipeline_stages",
        _uuid_pk(),
        sa.Column(
            "pipeline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipelines.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "default_probability", sa.Integer(), nullable=False, server_default="50"
        ),
        sa.Column("metadata", sa.JSON()),
        *_timestamps(),
    )


def _create_leads() -> None:
    if _has_table("leads"):
        return
    op.create_table(
        "leads",
        _uuid_pk(),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column(
            "pipeline_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("pipelines.id")
        ),
        sa.Column(
            "stage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_stages.id"),
        ),
        # Phase 4 CrmAgent — FK dropped, plain UUID (§1.3).
        sa.Column("owner_agent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title", sa.String(length=200)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="new"),
        sa.Column("estimated_value", sa.Numeric(12, 2)),
        sa.Column("currency", sa.String(length=3)),
        sa.Column("probability", sa.Integer()),
        sa.Column("expected_close_date", sa.Date()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("lost_reason", sa.String(length=200)),
        sa.Column("lead_source", sa.String(length=40)),
        # Phase 4 campaign attribution — FKs dropped, plain UUIDs (§1.3).
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True)),
        sa.Column("campaign_recipient_id", postgresql.UUID(as_uuid=True)),
        sa.Column("region", sa.String(length=80)),
        sa.Column("address", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index("ix_leads_campaign_id", "leads", ["campaign_id"])


def _create_leads_open_unique() -> None:
    # Partial expression unique index — the DB-level backstop for the
    # app-level lead dedup (§1.3, recreated from CRM ld2026062900 on
    # subscriber_id). Postgres-only: COALESCE-to-uuid expression.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {_LEADS_OPEN_UNIQUE}
            ON leads (subscriber_id, COALESCE(pipeline_id, '{_UUID_SENTINEL}'::uuid))
            WHERE is_active AND status NOT IN ('won', 'lost')
            """
        )
    )


def _create_quotes() -> None:
    if _has_table("quotes"):
        return
    op.create_table(
        "quotes",
        _uuid_pk(),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id")),
        # Staff person (quote owner) — FK dropped, plain UUID (§1.4/§1.8).
        sa.Column("owner_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="draft"
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_rate", sa.Numeric(5, 2)),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )


def _create_quote_line_items() -> None:
    if _has_table("quote_line_items"):
        return
    op.create_table(
        "quote_line_items",
        _uuid_pk(),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("quotes.id"),
            nullable=False,
        ),
        # Phase 5 inventory — FK dropped, plain UUID (§1.4).
        sa.Column("inventory_item_id", postgresql.UUID(as_uuid=True)),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column(
            "discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_sales_orders() -> None:
    if _has_table("sales_orders"):
        return
    op.create_table(
        "sales_orders",
        _uuid_pk(),
        sa.Column(
            "quote_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("quotes.id")
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        # Phase 4 CrmAgent — FK dropped, plain UUID (§1.5).
        sa.Column("owner_agent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source", sa.String(length=80)),
        sa.Column("order_number", sa.String(length=80)),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="draft"
        ),
        sa.Column(
            "payment_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount_paid", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("balance_due", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("payment_due_date", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column(
            "deposit_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "deposit_paid", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "contract_signed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("signed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("order_number", name="uq_sales_orders_order_number"),
        sa.UniqueConstraint("quote_id", name="uq_sales_orders_quote_id"),
    )


def _create_sales_order_lines() -> None:
    if _has_table("sales_order_lines"):
        return
    op.create_table(
        "sales_order_lines",
        _uuid_pk(),
        sa.Column(
            "sales_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id"),
            nullable=False,
        ),
        # Phase 5 inventory — FK dropped, plain UUID (§1.5).
        sa.Column("inventory_item_id", postgresql.UUID(as_uuid=True)),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )


def _create_project_templates() -> None:
    if _has_table("project_templates"):
        return
    op.create_table(
        "project_templates",
        _uuid_pk(),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("project_type", sa.String(length=60)),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("project_type", name="uq_project_templates_project_type"),
    )


def _create_project_template_tasks() -> None:
    if _has_table("project_template_tasks"):
        return
    op.create_table(
        "project_template_tasks",
        _uuid_pk(),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_templates.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(length=40)),
        sa.Column("priority", sa.String(length=40)),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effort_hours", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )


def _create_project_template_task_dependency() -> None:
    # Table name is singular in CRM — kept verbatim (§1.1).
    if _has_table("project_template_task_dependency"):
        return
    op.create_table(
        "project_template_task_dependency",
        _uuid_pk(),
        sa.Column(
            "template_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_template_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "depends_on_template_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_template_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "dependency_type",
            sa.String(length=40),
            nullable=False,
            server_default="finish_to_start",
        ),
        sa.Column("lag_days", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "template_task_id",
            "depends_on_template_task_id",
            name="uq_project_template_task_dependency",
        ),
        sa.CheckConstraint(
            "template_task_id <> depends_on_template_task_id",
            name="ck_project_template_task_dependency_no_self",
        ),
    )


def _create_projects() -> None:
    if _has_table("projects"):
        return
    op.create_table(
        "projects",
        _uuid_pk(),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("code", sa.String(length=80)),
        # Non-unique in CRM — kept non-unique (§1.2).
        sa.Column("number", sa.String(length=40)),
        sa.Column("erpnext_id", sa.String(length=100)),
        sa.Column("description", sa.Text()),
        sa.Column("customer_address", sa.Text()),
        sa.Column("project_type", sa.String(length=60)),
        sa.Column(
            "project_template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_templates.id"),
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="open"
        ),
        sa.Column(
            "priority", sa.String(length=40), nullable=False, server_default="normal"
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
        ),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id")),
        # Five staff roles — FKs dropped, plain UUIDs (§1.2/§1.8).
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("owner_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("manager_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("project_manager_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("assistant_manager_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "service_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id"),
        ),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("region", sa.String(length=80)),
        sa.Column("tags", sa.JSON()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index("ix_projects_erpnext_id", "projects", ["erpnext_id"], unique=True)


def _create_project_tasks() -> None:
    if _has_table("project_tasks"):
        return
    op.create_table(
        "project_tasks",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "parent_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_tasks.id"),
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("number", sa.String(length=40)),
        sa.Column("erpnext_id", sa.String(length=100)),
        sa.Column("description", sa.Text()),
        sa.Column(
            "template_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_template_tasks.id"),
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="todo"
        ),
        sa.Column(
            "priority", sa.String(length=40), nullable=False, server_default="normal"
        ),
        # Staff persons — FKs dropped, plain UUIDs (§1.2/§1.8).
        sa.Column("assigned_to_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
        # Phase 1 native tickets (backfill applies the re-key map, §1.2).
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_tickets.id"),
        ),
        # Plain UUID until the Phase 2 work-order flip adds the FK (§1.2).
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("effort_hours", sa.Integer()),
        sa.Column("tags", sa.JSON()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index(
        "ix_project_tasks_erpnext_id", "project_tasks", ["erpnext_id"], unique=True
    )


def _create_project_task_assignees() -> None:
    if _has_table("project_task_assignees"):
        return
    op.create_table(
        "project_task_assignees",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Staff person UUID, half of the composite PK — FK dropped (§1.8).
        sa.Column("person_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_project_task_dependencies() -> None:
    if _has_table("project_task_dependencies"):
        return
    op.create_table(
        "project_task_dependencies",
        _uuid_pk(),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "depends_on_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "dependency_type",
            sa.String(length=40),
            nullable=False,
            server_default="finish_to_start",
        ),
        sa.Column("lag_days", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "task_id",
            "depends_on_task_id",
            name="uq_project_task_dependencies",
        ),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_project_task_dependencies_no_self",
        ),
    )


def _create_project_task_comments() -> None:
    if _has_table("project_task_comments"):
        return
    op.create_table(
        "project_task_comments",
        _uuid_pk(),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_tasks.id"),
            nullable=False,
        ),
        # Staff person — FK dropped, plain UUID (§1.8).
        sa.Column("author_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON()),
        # Provenance metadata — not in CRM, added per §1.2.
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_project_comments() -> None:
    if _has_table("project_comments"):
        return
    op.create_table(
        "project_comments",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        # Staff person — FK dropped, plain UUID (§1.8).
        sa.Column("author_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON()),
        # Provenance metadata — not in CRM, added per §1.2.
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_referral_codes() -> None:
    if _has_table("referral_codes"):
        return
    op.create_table(
        "referral_codes",
        _uuid_pk(),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_referral_codes_subscriber_id", "referral_codes", ["subscriber_id"]
    )
    op.create_index("ix_referral_codes_code", "referral_codes", ["code"], unique=True)


def _create_referrals() -> None:
    if _has_table("referrals"):
        return
    op.create_table(
        "referrals",
        _uuid_pk(),
        sa.Column(
            "referrer_subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column(
            "referral_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("referral_codes.id"),
        ),
        # Collapses CRM referred_person_id + referred_subscriber_id (§1.6).
        sa.Column(
            "referred_subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
        ),
        sa.Column(
            "referred_lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id")
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("reward_amount", sa.Numeric(12, 2)),
        sa.Column(
            "reward_currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column(
            "reward_status",
            sa.String(length=20),
            nullable=False,
            server_default="none",
        ),
        sa.Column("reward_issued_at", sa.DateTime(timezone=True)),
        sa.Column("qualified_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(length=40)),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index(
        "ix_referrals_referrer_subscriber_id", "referrals", ["referrer_subscriber_id"]
    )
    op.create_index(
        "ix_referrals_referred_subscriber_id", "referrals", ["referred_subscriber_id"]
    )
    op.create_index("ix_referrals_status", "referrals", ["status"])
    op.create_index(
        "ix_referrals_referrer", "referrals", ["referrer_subscriber_id", "status"]
    )
    # Idempotent-capture guard, recreated from CRM's partial unique on
    # referred_person_id (§1.6).
    op.create_index(
        "uq_referrals_active_referred_subscriber",
        "referrals",
        ["referred_subscriber_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND referred_subscriber_id IS NOT NULL"),
    )


def _create_work_links() -> None:
    if _has_table("work_links"):
        return
    op.create_table(
        "work_links",
        _uuid_pk(),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("link_type", sa.String(length=40), nullable=False),
        sa.Column("contract_name", sa.String(length=120)),
        # Staff person — FK dropped, plain UUID (§1.8).
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "link_type",
            name="uq_work_links_source_target_link_type",
        ),
    )
    op.create_index("ix_work_links_source", "work_links", ["source_type", "source_id"])
    op.create_index("ix_work_links_target", "work_links", ["target_type", "target_id"])
    op.create_index("ix_work_links_contract", "work_links", ["contract_name"])


def _add_subscriber_sales_order_fk() -> None:
    # Expand A added subscribers.sales_order_id as a plain UUID with the FK
    # explicitly deferred to this migration (§1.5, doc 02 account matrix).
    if not _column_fk_names("subscribers", "sales_order_id"):
        op.create_foreign_key(
            "fk_subscribers_sales_order_id",
            "subscribers",
            "sales_orders",
            ["sales_order_id"],
            ["id"],
        )
    if not _has_index("subscribers", "ix_subscribers_sales_order_id"):
        op.create_index(
            "ix_subscribers_sales_order_id", "subscribers", ["sales_order_id"]
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    # Leads/pipeline → quotes → sales orders (FK order).
    _create_pipelines()
    _create_pipeline_stages()
    _create_leads()
    _create_leads_open_unique()
    _create_quotes()
    _create_quote_line_items()
    _create_sales_orders()
    _create_sales_order_lines()

    # Projects vertical (templates before projects before tasks).
    _create_project_templates()
    _create_project_template_tasks()
    _create_project_template_task_dependency()
    _create_projects()
    _create_project_tasks()
    _create_project_task_assignees()
    _create_project_task_dependencies()
    _create_project_task_comments()
    _create_project_comments()

    # Referrals + cross-vertical links.
    _create_referral_codes()
    _create_referrals()
    _create_work_links()

    _add_subscriber_sales_order_fk()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if _has_index("subscribers", "ix_subscribers_sales_order_id"):
        op.drop_index("ix_subscribers_sales_order_id", table_name="subscribers")
    # Drop whichever FK constrains the column — 001's create_all auto-names it
    # subscribers_sales_order_id_fkey on fresh databases.
    for fk_name in _column_fk_names("subscribers", "sales_order_id"):
        op.drop_constraint(fk_name, "subscribers", type_="foreignkey")

    # Reverse FK order; DROP TABLE drops each table's indexes with it.
    for table_name in (
        "work_links",
        "referrals",
        "referral_codes",
        "project_comments",
        "project_task_comments",
        "project_task_dependencies",
        "project_task_assignees",
        "project_tasks",
        "projects",
        "project_template_task_dependency",
        "project_template_tasks",
        "project_templates",
        "sales_order_lines",
        "sales_orders",
        "quote_line_items",
        "quotes",
        "leads",
        "pipeline_stages",
        "pipelines",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
