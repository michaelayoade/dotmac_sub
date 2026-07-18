"""Add Party-first lead identity and immutable origin capture.

Lead becomes capable of representing a prospect before a Subscriber account
exists. Existing subscriber-backed rows remain valid compatibility state.
Structured origin capture distinguishes native Sub campaign responses from
external advertising provider identifiers. Deferred NOT VALID foreign keys
protect new campaign/ticket writes without pretending legacy rows are clean.

This migration performs no backfill, lifecycle transition, Party/Subscriber
creation, attribution inference, campaign import, ticket rewrite, or reader
cutover.

Revision ID: 355_party_customer_lifecycle
Revises: 354_party_contact_inbox_projections
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "355_party_customer_lifecycle"
down_revision = "354_party_contact_inbox_projections"
branch_labels = None
depends_on = None

_PARTY_OPEN_LEAD_INDEX = "uq_leads_one_open_per_party_pipeline"
_UUID_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _create_deferred_fk(
    name: str,
    table_name: str,
    local_column: str,
    remote_table: str,
    *,
    ondelete: str = "RESTRICT",
) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({local_column}) REFERENCES {remote_table} (id) "
                f"ON DELETE {ondelete} NOT VALID"
            )
        )
        return
    op.create_foreign_key(
        name,
        table_name,
        remote_table,
        [local_column],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.alter_column(
        "leads", "subscriber_id", existing_type=postgresql.UUID(), nullable=True
    )
    op.add_column(
        "leads",
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "leads", sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "leads", sa.Column("party_binding_source", sa.String(length=80), nullable=True)
    )
    op.add_column("leads", sa.Column("party_binding_reason", sa.Text(), nullable=True))
    op.add_column(
        "leads",
        sa.Column("subscriber_linked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("subscriber_link_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "leads", sa.Column("subscriber_link_reason", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "fk_leads_party_id",
        "leads",
        "parties",
        ["party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_leads_party_id", "leads", ["party_id"])
    op.create_check_constraint(
        "ck_leads_party_or_subscriber",
        "leads",
        "party_id IS NOT NULL OR subscriber_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_leads_party_binding_evidence",
        "leads",
        "(party_id IS NULL AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL "
        "AND length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)",
    )
    op.create_check_constraint(
        "ck_leads_subscriber_link_evidence",
        "leads",
        "(subscriber_id IS NULL AND subscriber_linked_at IS NULL AND "
        "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
        "(subscriber_id IS NOT NULL AND subscriber_linked_at IS NULL AND "
        "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
        "(subscriber_id IS NOT NULL AND subscriber_linked_at IS NOT NULL AND "
        "subscriber_link_source IS NOT NULL AND subscriber_link_reason IS NOT "
        "NULL AND length(trim(subscriber_link_source)) > 0 AND "
        "length(trim(subscriber_link_reason)) > 0)",
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX {_PARTY_OPEN_LEAD_INDEX} ON leads "
                f"(party_id, COALESCE(pipeline_id, '{_UUID_SENTINEL}'::uuid)) "
                "WHERE party_id IS NOT NULL AND is_active "
                "AND status NOT IN ('won', 'lost')"
            )
        )

    op.create_table(
        "lead_origin_captures",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capture_method", sa.String(length=40), nullable=False),
        sa.Column("source_platform", sa.String(length=40), nullable=False),
        sa.Column("lead_source", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "campaign_recipient_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("external_campaign_id", sa.String(length=200), nullable=True),
        sa.Column("external_ad_set_id", sa.String(length=200), nullable=True),
        sa.Column("external_ad_id", sa.String(length=200), nullable=True),
        sa.Column("external_form_id", sa.String(length=200), nullable=True),
        sa.Column("external_click_id", sa.String(length=255), nullable=True),
        sa.Column("utm_source", sa.String(length=200), nullable=True),
        sa.Column("utm_medium", sa.String(length=200), nullable=True),
        sa.Column("utm_campaign", sa.String(length=200), nullable=True),
        sa.Column("utm_content", sa.String(length=200), nullable=True),
        sa.Column("utm_term", sa.String(length=200), nullable=True),
        sa.Column("landing_path", sa.String(length=500), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_source", sa.String(length=80), nullable=False),
        sa.Column("capture_reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "capture_method IN ('ad_lead_form_webhook', 'landing_page', 'portal', "
            "'agent_declared', 'campaign_response', 'referral', "
            "'reviewed_import')",
            name="ck_lead_origin_captures_method",
        ),
        sa.CheckConstraint(
            "source_platform IN ('meta', 'google', 'website', 'portal', 'agent', "
            "'referral', 'sub_campaign', 'legacy_import')",
            name="ck_lead_origin_captures_platform",
        ),
        sa.CheckConstraint(
            "campaign_recipient_id IS NULL OR campaign_id IS NOT NULL",
            name="ck_lead_origin_captures_recipient_campaign",
        ),
        sa.CheckConstraint(
            "capture_method <> 'campaign_response' OR "
            "(campaign_id IS NOT NULL AND campaign_recipient_id IS NOT NULL AND "
            "source_platform = 'sub_campaign')",
            name="ck_lead_origin_captures_campaign_response",
        ),
        sa.CheckConstraint(
            "capture_method <> 'ad_lead_form_webhook' OR "
            "(source_platform IN ('meta', 'google') AND "
            "external_campaign_id IS NOT NULL AND "
            "length(trim(external_campaign_id)) > 0)",
            name="ck_lead_origin_captures_ad_webhook",
        ),
        sa.CheckConstraint(
            "(capture_method <> 'landing_page' OR source_platform = 'website') AND "
            "(capture_method <> 'portal' OR source_platform = 'portal') AND "
            "(capture_method <> 'agent_declared' OR source_platform = 'agent') AND "
            "(capture_method <> 'referral' OR source_platform = 'referral') AND "
            "(capture_method <> 'reviewed_import' OR "
            "source_platform = 'legacy_import')",
            name="ck_lead_origin_captures_method_platform",
        ),
        sa.CheckConstraint(
            "(source_platform <> 'meta' OR lead_source IN "
            "('Facebook Ads', 'Instagram Ads')) AND "
            "(source_platform <> 'google' OR lead_source = 'Google') AND "
            "(source_platform <> 'website' OR lead_source = 'Website') AND "
            "(source_platform <> 'portal' OR lead_source = 'Portal') AND "
            "(source_platform <> 'referral' OR lead_source = 'Referrer')",
            name="ck_lead_origin_captures_platform_source",
        ),
        sa.CheckConstraint(
            "length(trim(capture_source)) > 0 AND length(trim(capture_reason)) > 0",
            name="ck_lead_origin_captures_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_lead_origin_captures_lead_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_lead_origin_captures_campaign_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_recipient_id"],
            ["campaign_recipients.id"],
            name="fk_lead_origin_captures_campaign_recipient_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lead_origin_captures"),
        sa.UniqueConstraint("lead_id", name="uq_lead_origin_captures_lead_id"),
    )
    op.create_index(
        "ix_lead_origin_captures_campaign",
        "lead_origin_captures",
        ["campaign_id"],
    )
    op.create_index(
        "ix_lead_origin_captures_external_campaign",
        "lead_origin_captures",
        ["source_platform", "external_campaign_id"],
    )

    _create_deferred_fk(
        "fk_leads_campaign_id",
        "leads",
        "campaign_id",
        "campaigns",
    )
    _create_deferred_fk(
        "fk_leads_campaign_recipient_id",
        "leads",
        "campaign_recipient_id",
        "campaign_recipients",
    )
    _create_deferred_fk(
        "fk_support_tickets_lead_id",
        "support_tickets",
        "lead_id",
        "leads",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_support_tickets_lead_id", "support_tickets", type_="foreignkey"
    )
    op.drop_constraint("fk_leads_campaign_recipient_id", "leads", type_="foreignkey")
    op.drop_constraint("fk_leads_campaign_id", "leads", type_="foreignkey")
    op.drop_index(
        "ix_lead_origin_captures_external_campaign",
        table_name="lead_origin_captures",
    )
    op.drop_index("ix_lead_origin_captures_campaign", table_name="lead_origin_captures")
    op.drop_table("lead_origin_captures")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(_PARTY_OPEN_LEAD_INDEX, table_name="leads")
    op.drop_constraint("ck_leads_subscriber_link_evidence", "leads", type_="check")
    op.drop_constraint("ck_leads_party_binding_evidence", "leads", type_="check")
    op.drop_constraint("ck_leads_party_or_subscriber", "leads", type_="check")
    op.drop_index("ix_leads_party_id", table_name="leads")
    op.drop_constraint("fk_leads_party_id", "leads", type_="foreignkey")
    op.drop_column("leads", "subscriber_link_reason")
    op.drop_column("leads", "subscriber_link_source")
    op.drop_column("leads", "subscriber_linked_at")
    op.drop_column("leads", "party_binding_reason")
    op.drop_column("leads", "party_binding_source")
    op.drop_column("leads", "party_bound_at")
    op.drop_column("leads", "party_id")
    op.alter_column(
        "leads", "subscriber_id", existing_type=postgresql.UUID(), nullable=False
    )
