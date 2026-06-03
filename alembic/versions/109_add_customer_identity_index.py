"""Add normalized customer identity lookup table.

Revision ID: 109_add_customer_identity_index
Revises: 108_drop_support_assignment_subscriber_fks
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "109_add_customer_identity_index"
down_revision = "108_drop_support_assignment_subscriber_fks"
branch_labels = None
depends_on = None

_DEFAULT_COUNTRY_CODE = "234"


def _normalize_email(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _normalize_phone(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith("0") and len(digits) >= 10:
        return f"+{_DEFAULT_COUNTRY_CODE}{digits[1:]}"
    if digits.startswith(_DEFAULT_COUNTRY_CODE):
        return f"+{digits}"
    return f"+{digits}"


def upgrade() -> None:
    op.create_table(
        "customer_identity_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_type", sa.String(length=32), nullable=False),
        sa.Column("normalized_value", sa.String(length=255), nullable=False),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscriber_contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriber_contacts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "subscriber_channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriber_channels.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_field", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_identity_index_identity_type_value",
        "customer_identity_index",
        ["identity_type", "normalized_value"],
        unique=False,
    )
    op.create_index(
        "ix_customer_identity_index_subscriber",
        "customer_identity_index",
        ["subscriber_id"],
        unique=False,
    )

    connection = op.get_bind()
    identity_table = sa.table(
        "customer_identity_index",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("identity_type", sa.String(length=32)),
        sa.column("normalized_value", sa.String(length=255)),
        sa.column("subscriber_id", postgresql.UUID(as_uuid=True)),
        sa.column("subscriber_contact_id", postgresql.UUID(as_uuid=True)),
        sa.column("subscriber_channel_id", postgresql.UUID(as_uuid=True)),
        sa.column("source_table", sa.String(length=64)),
        sa.column("source_field", sa.String(length=64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    subscribers = connection.execute(
        sa.text("SELECT id, email, phone FROM subscribers")
    ).mappings()
    contacts_by_subscriber: dict[uuid.UUID, list[dict[str, object]]] = {}
    for row in connection.execute(
        sa.text(
            "SELECT id, subscriber_id, email, phone, whatsapp FROM subscriber_contacts"
        )
    ).mappings():
        contacts_by_subscriber.setdefault(row["subscriber_id"], []).append(dict(row))
    channels_by_subscriber: dict[uuid.UUID, list[dict[str, object]]] = {}
    for row in connection.execute(
        sa.text(
            "SELECT id, subscriber_id, channel_type, address FROM subscriber_channels"
        )
    ).mappings():
        channels_by_subscriber.setdefault(row["subscriber_id"], []).append(dict(row))

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    now = datetime.now(UTC)

    def _append_row(
        *,
        identity_type: str,
        normalized_value: str | None,
        subscriber_id: uuid.UUID,
        source_table: str,
        source_field: str,
        subscriber_contact_id: uuid.UUID | None = None,
        subscriber_channel_id: uuid.UUID | None = None,
    ) -> None:
        if not normalized_value:
            return
        dedupe_key = (
            identity_type,
            normalized_value,
            source_table,
            source_field,
            str(subscriber_contact_id or subscriber_channel_id or subscriber_id),
        )
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        rows.append(
            {
                "id": uuid.uuid4(),
                "identity_type": identity_type,
                "normalized_value": normalized_value,
                "subscriber_id": subscriber_id,
                "subscriber_contact_id": subscriber_contact_id,
                "subscriber_channel_id": subscriber_channel_id,
                "source_table": source_table,
                "source_field": source_field,
                "created_at": now,
                "updated_at": now,
            }
        )

    for subscriber in subscribers:
        subscriber_id = subscriber["id"]
        _append_row(
            identity_type="email",
            normalized_value=_normalize_email(subscriber["email"]),
            subscriber_id=subscriber_id,
            source_table="subscribers",
            source_field="email",
        )
        _append_row(
            identity_type="phone",
            normalized_value=_normalize_phone(subscriber["phone"]),
            subscriber_id=subscriber_id,
            source_table="subscribers",
            source_field="phone",
        )
        for contact in contacts_by_subscriber.get(subscriber_id, []):
            _append_row(
                identity_type="email",
                normalized_value=_normalize_email(contact.get("email")),
                subscriber_id=subscriber_id,
                source_table="subscriber_contacts",
                source_field="email",
                subscriber_contact_id=contact["id"],
            )
            _append_row(
                identity_type="phone",
                normalized_value=_normalize_phone(contact.get("phone")),
                subscriber_id=subscriber_id,
                source_table="subscriber_contacts",
                source_field="phone",
                subscriber_contact_id=contact["id"],
            )
            _append_row(
                identity_type="phone",
                normalized_value=_normalize_phone(contact.get("whatsapp")),
                subscriber_id=subscriber_id,
                source_table="subscriber_contacts",
                source_field="whatsapp",
                subscriber_contact_id=contact["id"],
            )
        for channel in channels_by_subscriber.get(subscriber_id, []):
            channel_type = str(channel.get("channel_type") or "").strip().lower()
            identity_type = "email" if channel_type == "email" else "phone"
            normalized_value = (
                _normalize_email(channel.get("address"))
                if identity_type == "email"
                else _normalize_phone(channel.get("address"))
            )
            _append_row(
                identity_type=identity_type,
                normalized_value=normalized_value,
                subscriber_id=subscriber_id,
                source_table="subscriber_channels",
                source_field=channel_type or "address",
                subscriber_channel_id=channel["id"],
            )

    if rows:
        connection.execute(sa.insert(identity_table), rows)


def downgrade() -> None:
    op.drop_index(
        "ix_customer_identity_index_subscriber",
        table_name="customer_identity_index",
    )
    op.drop_index(
        "ix_customer_identity_index_identity_type_value",
        table_name="customer_identity_index",
    )
    op.drop_table("customer_identity_index")
