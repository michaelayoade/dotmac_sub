"""Add admin dashboard What's New items.

Revision ID: 103_add_admin_whats_new_items
Revises: 102_add_splynx_credit_note_id
Create Date: 2026-05-23
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "103_add_admin_whats_new_items"
down_revision = "102_add_splynx_credit_note_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_whats_new_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("benefit_one", sa.String(length=255), nullable=True),
        sa.Column("benefit_two", sa.String(length=255), nullable=True),
        sa.Column("benefit_three", sa.String(length=255), nullable=True),
        sa.Column("button_text", sa.String(length=80), nullable=False),
        sa.Column("button_link", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_admin_whats_new_items_status",
        "admin_whats_new_items",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_admin_whats_new_items_starts_at",
        "admin_whats_new_items",
        ["starts_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_whats_new_items_ends_at",
        "admin_whats_new_items",
        ["ends_at"],
        unique=False,
    )

    now = datetime.now(UTC)
    op.bulk_insert(
        sa.table(
            "admin_whats_new_items",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("title", sa.String()),
            sa.column("message", sa.Text()),
            sa.column("benefit_one", sa.String()),
            sa.column("benefit_two", sa.String()),
            sa.column("benefit_three", sa.String()),
            sa.column("button_text", sa.String()),
            sa.column("button_link", sa.String()),
            sa.column("status", sa.String()),
            sa.column("starts_at", sa.DateTime(timezone=True)),
            sa.column("ends_at", sa.DateTime(timezone=True)),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "id": uuid.uuid4(),
                "title": "Unconfigured ONTs",
                "message": "You can now view unconfigured ONTs on the ONT list page. Use the button group in the page header to switch views.",
                "benefit_one": None,
                "benefit_two": None,
                "benefit_three": None,
                "button_text": "Open ONTs",
                "button_link": "/admin/network/onts?view=unconfigured",
                "status": "featured",
                "starts_at": None,
                "ends_at": None,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": uuid.uuid4(),
                "title": "Admin Page Tips",
                "message": "You can now use guided tips to learn key areas of the admin dashboard faster.",
                "benefit_one": None,
                "benefit_two": None,
                "benefit_three": None,
                "button_text": "Start tour",
                "button_link": "/admin/dashboard?tour=1",
                "status": "active",
                "starts_at": None,
                "ends_at": None,
                "created_at": now - timedelta(minutes=1),
                "updated_at": now - timedelta(minutes=1),
            },
            {
                "id": uuid.uuid4(),
                "title": "Quick Tour",
                "message": "You can now take a quick tour to learn where key tools and pages are.",
                "benefit_one": None,
                "benefit_two": None,
                "benefit_three": None,
                "button_text": "Start tour",
                "button_link": "/admin/dashboard?tour=1",
                "status": "active",
                "starts_at": None,
                "ends_at": None,
                "created_at": now - timedelta(minutes=2),
                "updated_at": now - timedelta(minutes=2),
            },
            {
                "id": uuid.uuid4(),
                "title": "One Configuration at a Time",
                "message": "You can now apply only one part of an ONT configuration without changing everything else.",
                "benefit_one": None,
                "benefit_two": None,
                "benefit_three": None,
                "button_text": "Try it now",
                "button_link": "/admin/network/onts",
                "status": "active",
                "starts_at": None,
                "ends_at": None,
                "created_at": now - timedelta(minutes=3),
                "updated_at": now - timedelta(minutes=3),
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_whats_new_items_ends_at", table_name="admin_whats_new_items")
    op.drop_index(
        "ix_admin_whats_new_items_starts_at", table_name="admin_whats_new_items"
    )
    op.drop_index("ix_admin_whats_new_items_status", table_name="admin_whats_new_items")
    op.drop_table("admin_whats_new_items")
