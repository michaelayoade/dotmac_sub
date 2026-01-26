"""Unified party model - add party_status, person channels, account roles.

This migration implements the unified party model:
1. Adds party_status and organization_id to people table
2. Creates person_channels table for multi-channel communication
3. Creates person_status_logs table for status transition auditing
4. Creates person_merge_logs table for merge operation auditing
5. Creates account_roles table to link people to subscriber accounts
6. Backfills person_id on crm_leads, crm_quotes, crm_conversations
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ENUM

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = ("cb18f1a3d6c9", "8f802e49c452")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # Create enum types first
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE partystatus AS ENUM ('lead', 'contact', 'customer', 'subscriber');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE personchanneltype AS ENUM ('email', 'phone', 'sms', 'whatsapp', 'facebook_messenger', 'instagram_dm');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE accountroletype AS ENUM ('primary', 'billing', 'technical', 'support');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # 1. Add party_status and organization_id to people table
    op.add_column(
        "people",
        sa.Column(
            "party_status",
            ENUM("lead", "contact", "customer", "subscriber", name="partystatus", create_type=False),
            nullable=False,
            server_default="contact",
        ),
    )
    op.add_column(
        "people",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_people_organization_id",
        "people",
        "organizations",
        ["organization_id"],
        ["id"],
    )

    # 2. Create person_channels table
    op.create_table(
        "person_channels",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel_type",
            ENUM("email", "phone", "sms", "whatsapp", "facebook_messenger", "instagram_dm", name="personchanneltype", create_type=False),
            nullable=False,
        ),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("label", sa.String(60), nullable=True),
        sa.Column("is_primary", sa.Boolean, default=False, nullable=False),
        sa.Column("is_verified", sa.Boolean, default=False, nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], name="fk_person_channels_person_id"),
        sa.UniqueConstraint("person_id", "channel_type", "address", name="uq_person_channels_person_type_address"),
    )
    op.create_index("ix_person_channels_person_id", "person_channels", ["person_id"])
    op.create_index("ix_person_channels_address", "person_channels", ["address"])

    # 3. Create person_status_logs table
    op.create_table(
        "person_status_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "from_status",
            ENUM("lead", "contact", "customer", "subscriber", name="partystatus", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            ENUM("lead", "contact", "customer", "subscriber", name="partystatus", create_type=False),
            nullable=False,
        ),
        sa.Column("changed_by_id", UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], name="fk_person_status_logs_person_id"),
        sa.ForeignKeyConstraint(["changed_by_id"], ["people.id"], name="fk_person_status_logs_changed_by_id"),
    )
    op.create_index("ix_person_status_logs_person_id", "person_status_logs", ["person_id"])

    # 4. Create person_merge_logs table
    op.create_table(
        "person_merge_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("merged_by_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_snapshot", sa.JSON, nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_person_id"], ["people.id"], name="fk_person_merge_logs_target_person_id"),
        sa.ForeignKeyConstraint(["merged_by_id"], ["people.id"], name="fk_person_merge_logs_merged_by_id"),
    )
    op.create_index("ix_person_merge_logs_target_person_id", "person_merge_logs", ["target_person_id"])

    # 5. Create account_roles table
    op.create_table(
        "account_roles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            ENUM("primary", "billing", "technical", "support", name="accountroletype", create_type=False),
            nullable=False,
            server_default="primary",
        ),
        sa.Column("is_primary", sa.Boolean, default=False, nullable=False),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["subscriber_accounts.id"], name="fk_account_roles_account_id"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], name="fk_account_roles_person_id"),
        sa.UniqueConstraint("account_id", "person_id", "role", name="uq_account_roles_account_person_role"),
    )
    op.create_index("ix_account_roles_account_id", "account_roles", ["account_id"])
    op.create_index("ix_account_roles_person_id", "account_roles", ["person_id"])

    # 6. Backfill people for crm_contacts without person_id
    # Step 1: Link crm_contacts to existing people by matching email
    op.execute("""
        UPDATE crm_contacts c
        SET person_id = p.id
        FROM people p
        WHERE c.person_id IS NULL
          AND c.email IS NOT NULL
          AND p.email = c.email
    """)

    # Step 2: Create new people for remaining crm_contacts without person_id
    op.execute("""
        CREATE TEMP TABLE tmp_crm_contact_people (
            contact_id uuid PRIMARY KEY,
            person_id uuid NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO tmp_crm_contact_people (contact_id, person_id)
        SELECT c.id, gen_random_uuid()
        FROM crm_contacts c
        WHERE c.person_id IS NULL
    """)
    op.execute("""
        INSERT INTO people (
            id,
            first_name,
            last_name,
            display_name,
            email,
            email_verified,
            phone,
            gender,
            status,
            is_active,
            marketing_opt_in,
            created_at,
            updated_at,
            party_status,
            organization_id
        )
        SELECT
            tmp.person_id,
            COALESCE(NULLIF(split_part(c.display_name, ' ', 1), ''), 'Contact'),
            COALESCE(NULLIF(split_part(c.display_name, ' ', 2), ''), 'Contact'),
            c.display_name,
            COALESCE(c.email, concat('contact-', c.id, '@placeholder.local')),
            false,
            c.phone,
            'unknown',
            'active',
            true,
            false,
            c.created_at,
            c.updated_at,
            'contact',
            c.organization_id
        FROM tmp_crm_contact_people tmp
        JOIN crm_contacts c ON c.id = tmp.contact_id
    """)
    op.execute("""
        UPDATE crm_contacts c
        SET person_id = tmp.person_id
        FROM tmp_crm_contact_people tmp
        WHERE c.id = tmp.contact_id
    """)
    op.execute("DROP TABLE tmp_crm_contact_people")

    # 7. Drop XOR constraint on subscribers BEFORE backfilling (we'll also remove it permanently later)
    op.drop_constraint("ck_subscribers_person_or_org", "subscribers", type_="check")

    # 8. Backfill people for organization-only subscribers
    op.execute("""
        CREATE TEMP TABLE tmp_subscriber_people (
            subscriber_id uuid PRIMARY KEY,
            person_id uuid NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO tmp_subscriber_people (subscriber_id, person_id)
        SELECT s.id, gen_random_uuid()
        FROM subscribers s
        WHERE s.person_id IS NULL
          AND s.organization_id IS NOT NULL
    """)
    op.execute("""
        INSERT INTO people (
            id,
            first_name,
            last_name,
            display_name,
            email,
            email_verified,
            phone,
            gender,
            status,
            is_active,
            marketing_opt_in,
            created_at,
            updated_at,
            party_status,
            organization_id
        )
        SELECT
            tmp.person_id,
            COALESCE(NULLIF(split_part(o.name, ' ', 1), ''), 'Organization'),
            COALESCE(NULLIF(split_part(o.name, ' ', 2), ''), 'Customer'),
            o.name,
            concat('org-', o.id, '@placeholder.local'),
            false,
            NULL,
            'unknown',
            'active',
            true,
            false,
            s.created_at,
            s.updated_at,
            'subscriber',
            s.organization_id
        FROM tmp_subscriber_people tmp
        JOIN subscribers s ON s.id = tmp.subscriber_id
        JOIN organizations o ON o.id = s.organization_id
    """)
    op.execute("""
        UPDATE subscribers s
        SET person_id = tmp.person_id
        FROM tmp_subscriber_people tmp
        WHERE s.id = tmp.subscriber_id
    """)
    op.execute("DROP TABLE tmp_subscriber_people")

    # 8. Create person_channels from crm_contact_channels
    op.execute("""
        INSERT INTO person_channels (
            id,
            person_id,
            channel_type,
            address,
            is_primary,
            is_verified,
            created_at,
            updated_at,
            metadata
        )
        SELECT
            gen_random_uuid(),
            c.person_id,
            cc.channel_type::text::personchanneltype,
            cc.address,
            cc.is_primary,
            false,
            cc.created_at,
            cc.updated_at,
            cc.metadata
        FROM crm_contact_channels cc
        JOIN crm_contacts c ON c.id = cc.contact_id
        WHERE c.person_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM person_channels pc
              WHERE pc.person_id = c.person_id
                AND pc.channel_type = cc.channel_type::text::personchanneltype
                AND pc.address = cc.address
          )
    """)

    # 9. Backfill person_id on crm_leads from crm_contacts
    # First ensure person_id column exists (it should already, just nullable)
    # Backfill from contact_id -> crm_contacts.person_id
    op.execute("""
        UPDATE crm_leads l
        SET person_id = c.person_id
        FROM crm_contacts c
        WHERE l.contact_id = c.id
          AND l.person_id IS NULL
          AND c.person_id IS NOT NULL
    """)

    # 10. Backfill person_id on crm_quotes from crm_contacts
    op.execute("""
        UPDATE crm_quotes q
        SET person_id = c.person_id
        FROM crm_contacts c
        WHERE q.contact_id = c.id
          AND q.person_id IS NULL
          AND c.person_id IS NOT NULL
    """)

    # 11. Backfill person_id on crm_conversations from crm_contacts
    op.execute("""
        UPDATE crm_conversations conv
        SET person_id = c.person_id
        FROM crm_contacts c
        WHERE conv.contact_id = c.id
          AND conv.person_id IS NULL
          AND c.person_id IS NOT NULL
    """)

    # 12. Create placeholder people for orphaned leads (those still without person_id)
    op.execute("""
        CREATE TEMP TABLE tmp_orphan_lead_people (
            lead_id uuid PRIMARY KEY,
            person_id uuid NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO tmp_orphan_lead_people (lead_id, person_id)
        SELECT l.id, gen_random_uuid()
        FROM crm_leads l
        WHERE l.person_id IS NULL
    """)
    op.execute("""
        INSERT INTO people (
            id, first_name, last_name, display_name, email, email_verified,
            phone, gender, status, is_active, marketing_opt_in,
            created_at, updated_at, party_status
        )
        SELECT
            tmp.person_id,
            'Lead',
            COALESCE(l.title, 'Contact'),
            COALESCE(l.title, concat('Lead ', l.id)),
            concat('lead-', l.id, '@placeholder.local'),
            false,
            NULL,
            'unknown',
            'active',
            true,
            false,
            l.created_at,
            l.updated_at,
            'lead'
        FROM tmp_orphan_lead_people tmp
        JOIN crm_leads l ON l.id = tmp.lead_id
    """)
    op.execute("""
        UPDATE crm_leads l
        SET person_id = tmp.person_id
        FROM tmp_orphan_lead_people tmp
        WHERE l.id = tmp.lead_id
    """)
    op.execute("DROP TABLE tmp_orphan_lead_people")

    # 12b. Create placeholder people for orphaned quotes
    op.execute("""
        CREATE TEMP TABLE tmp_orphan_quote_people (
            quote_id uuid PRIMARY KEY,
            person_id uuid NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO tmp_orphan_quote_people (quote_id, person_id)
        SELECT q.id, gen_random_uuid()
        FROM crm_quotes q
        WHERE q.person_id IS NULL
    """)
    op.execute("""
        INSERT INTO people (
            id, first_name, last_name, display_name, email, email_verified,
            phone, gender, status, is_active, marketing_opt_in,
            created_at, updated_at, party_status
        )
        SELECT
            tmp.person_id,
            'Quote',
            'Contact',
            concat('Quote ', q.id),
            concat('quote-', q.id, '@placeholder.local'),
            false,
            NULL,
            'unknown',
            'active',
            true,
            false,
            q.created_at,
            q.updated_at,
            'contact'
        FROM tmp_orphan_quote_people tmp
        JOIN crm_quotes q ON q.id = tmp.quote_id
    """)
    op.execute("""
        UPDATE crm_quotes q
        SET person_id = tmp.person_id
        FROM tmp_orphan_quote_people tmp
        WHERE q.id = tmp.quote_id
    """)
    op.execute("DROP TABLE tmp_orphan_quote_people")

    # 12c. Create placeholder people for orphaned conversations
    op.execute("""
        CREATE TEMP TABLE tmp_orphan_conv_people (
            conv_id uuid PRIMARY KEY,
            person_id uuid NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO tmp_orphan_conv_people (conv_id, person_id)
        SELECT c.id, gen_random_uuid()
        FROM crm_conversations c
        WHERE c.person_id IS NULL
    """)
    op.execute("""
        INSERT INTO people (
            id, first_name, last_name, display_name, email, email_verified,
            phone, gender, status, is_active, marketing_opt_in,
            created_at, updated_at, party_status
        )
        SELECT
            tmp.person_id,
            'Conversation',
            'Contact',
            COALESCE(c.subject, concat('Conversation ', c.id)),
            concat('conv-', c.id, '@placeholder.local'),
            false,
            NULL,
            'unknown',
            'active',
            true,
            false,
            c.created_at,
            c.updated_at,
            'contact'
        FROM tmp_orphan_conv_people tmp
        JOIN crm_conversations c ON c.id = tmp.conv_id
    """)
    op.execute("""
        UPDATE crm_conversations c
        SET person_id = tmp.person_id
        FROM tmp_orphan_conv_people tmp
        WHERE c.id = tmp.conv_id
    """)
    op.execute("DROP TABLE tmp_orphan_conv_people")

    # 13. Migrate account contacts to account_roles
    # For each contact in the contacts table, create an account_role
    # Note: cast contactrole to text then to accountroletype for enum compatibility
    op.execute("""
        INSERT INTO account_roles (id, account_id, person_id, role, is_primary, title, notes, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            c.account_id,
            p.id,
            c.role::text::accountroletype,
            c.is_primary,
            c.title,
            c.notes,
            c.created_at,
            c.updated_at
        FROM contacts c
        JOIN contact_emails ce ON ce.contact_id = c.id AND ce.is_primary = true
        JOIN people p ON p.email = ce.email
        WHERE NOT EXISTS (
            SELECT 1 FROM account_roles ar
            WHERE ar.account_id = c.account_id
              AND ar.person_id = p.id
              AND ar.role::text = c.role::text
        )
    """)

    # 13. Create person_channels from people's email/phone
    op.execute("""
        INSERT INTO person_channels (id, person_id, channel_type, address, is_primary, is_verified, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            id,
            'email',
            email,
            true,
            email_verified,
            created_at,
            updated_at
        FROM people
        WHERE email IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM person_channels pc
              WHERE pc.person_id = people.id
                AND pc.channel_type = 'email'
                AND pc.address = people.email
          )
    """)

    op.execute("""
        INSERT INTO person_channels (id, person_id, channel_type, address, is_primary, is_verified, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            id,
            'phone',
            phone,
            false,
            false,
            created_at,
            updated_at
        FROM people
        WHERE phone IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM person_channels pc
              WHERE pc.person_id = people.id
                AND pc.channel_type = 'phone'
                AND pc.address = people.phone
          )
    """)

    # 14. Add person_channel_id to crm_messages and backfill from crm_contact_channels
    op.add_column("crm_messages", sa.Column("person_channel_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "crm_messages_person_channel_id_fkey",
        "crm_messages",
        "person_channels",
        ["person_channel_id"],
        ["id"],
    )
    op.execute("""
        UPDATE crm_messages m
        SET person_channel_id = pc.id
        FROM crm_contact_channels cc
        JOIN crm_contacts c ON c.id = cc.contact_id
        JOIN person_channels pc
          ON pc.person_id = c.person_id
         AND pc.channel_type = cc.channel_type::text::personchanneltype
         AND pc.address = cc.address
        WHERE m.contact_channel_id = cc.id
    """)

    # 15. Make person_id required and remove legacy columns
    op.alter_column("crm_leads", "person_id", nullable=False)
    op.alter_column("crm_quotes", "person_id", nullable=False)
    op.alter_column("crm_conversations", "person_id", nullable=False)
    op.alter_column("subscribers", "person_id", nullable=False)

    # XOR constraint already dropped in step 7
    op.drop_column("subscribers", "subscriber_type")
    op.drop_column("subscribers", "organization_id")

    op.drop_column("crm_leads", "contact_id")
    op.drop_column("crm_leads", "organization_id")
    op.drop_column("crm_leads", "subscriber_id")
    op.drop_column("crm_leads", "account_id")

    op.drop_column("crm_quotes", "contact_id")
    op.drop_column("crm_quotes", "organization_id")
    op.drop_column("crm_quotes", "subscriber_id")
    op.drop_column("crm_quotes", "account_id")

    op.drop_column("crm_conversations", "contact_id")
    op.drop_column("crm_conversations", "organization_id")
    op.drop_column("crm_conversations", "subscriber_id")
    op.drop_column("crm_conversations", "account_id")

    op.drop_column("crm_messages", "contact_channel_id")

    # 16. Update service_orders foreign key to people
    # Best-effort remap contact IDs to people before the FK swap.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'contacts')
               AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'contact_emails')
               AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'service_orders')
               AND EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'service_orders' AND column_name = 'requested_by_contact_id'
               ) THEN
                UPDATE service_orders so
                SET requested_by_contact_id = p.id
                FROM contacts c
                JOIN contact_emails ce ON ce.contact_id = c.id AND ce.is_primary = true
                JOIN people p ON p.email = ce.email
                WHERE so.requested_by_contact_id = c.id;
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE service_orders DROP CONSTRAINT IF EXISTS service_orders_requested_by_contact_id_fkey")
    op.create_foreign_key(
        "service_orders_requested_by_contact_id_fkey",
        "service_orders",
        "people",
        ["requested_by_contact_id"],
        ["id"],
    )

    # 17. Drop deprecated tables
    op.drop_table("contact_phones")
    op.drop_table("contact_emails")
    op.drop_table("contacts")
    op.drop_table("crm_contact_channels")
    op.drop_table("crm_contacts")
    op.execute("DROP TYPE IF EXISTS subscribertype")


def downgrade() -> None:
    # Restore legacy columns
    op.add_column(
        "subscribers",
        sa.Column(
            "subscriber_type",
            sa.Enum("person", "organization", name="subscribertype"),
            nullable=False,
            server_default="person",
        ),
    )
    op.add_column("subscribers", sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "subscribers_organization_id_fkey",
        "subscribers",
        "organizations",
        ["organization_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_subscribers_person_or_org",
        "subscribers",
        "(person_id IS NOT NULL AND organization_id IS NULL) OR (person_id IS NULL AND organization_id IS NOT NULL)",
    )
    op.alter_column("subscribers", "person_id", nullable=True)

    op.add_column("crm_leads", sa.Column("contact_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_leads", sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_leads", sa.Column("subscriber_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_leads", sa.Column("account_id", UUID(as_uuid=True), nullable=True))

    op.add_column("crm_quotes", sa.Column("contact_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_quotes", sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_quotes", sa.Column("subscriber_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_quotes", sa.Column("account_id", UUID(as_uuid=True), nullable=True))

    op.add_column("crm_conversations", sa.Column("contact_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_conversations", sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_conversations", sa.Column("subscriber_id", UUID(as_uuid=True), nullable=True))
    op.add_column("crm_conversations", sa.Column("account_id", UUID(as_uuid=True), nullable=True))
    op.alter_column("crm_conversations", "person_id", nullable=True)
    op.alter_column("crm_leads", "person_id", nullable=True)
    op.alter_column("crm_quotes", "person_id", nullable=True)

    op.add_column("crm_messages", sa.Column("contact_channel_id", UUID(as_uuid=True), nullable=True))
    op.drop_constraint("crm_messages_person_channel_id_fkey", "crm_messages", type_="foreignkey")
    op.drop_column("crm_messages", "person_channel_id")

    # Recreate deprecated tables
    op.create_table(
        "crm_contacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(160)),
        sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(40)),
        sa.Column("person_id", UUID(as_uuid=True)),
        sa.Column("organization_id", UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
    )
    op.create_table(
        "crm_contact_channels",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_type", sa.Enum("email", "whatsapp", name="channeltype"), nullable=False),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("metadata", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["crm_contacts.id"]),
        sa.UniqueConstraint("contact_id", "channel_type", "address", name="uq_crm_contact_channels_contact_type_address"),
    )
    op.create_table(
        "contacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("title", sa.String(120)),
        sa.Column("role", sa.Enum("primary", "billing", "technical", "support", name="contactrole"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["subscriber_accounts.id"]),
    )
    op.create_table(
        "contact_emails",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("label", sa.String(60)),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
    )
    op.create_table(
        "contact_phones",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("phone", sa.String(40), nullable=False),
        sa.Column("label", sa.String(60)),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
    )

    op.create_foreign_key("crm_leads_contact_id_fkey", "crm_leads", "crm_contacts", ["contact_id"], ["id"])
    op.create_foreign_key("crm_leads_organization_id_fkey", "crm_leads", "organizations", ["organization_id"], ["id"])
    op.create_foreign_key("crm_leads_subscriber_id_fkey", "crm_leads", "subscribers", ["subscriber_id"], ["id"])
    op.create_foreign_key("crm_leads_account_id_fkey", "crm_leads", "subscriber_accounts", ["account_id"], ["id"])

    op.create_foreign_key("crm_quotes_contact_id_fkey", "crm_quotes", "crm_contacts", ["contact_id"], ["id"])
    op.create_foreign_key("crm_quotes_organization_id_fkey", "crm_quotes", "organizations", ["organization_id"], ["id"])
    op.create_foreign_key("crm_quotes_subscriber_id_fkey", "crm_quotes", "subscribers", ["subscriber_id"], ["id"])
    op.create_foreign_key("crm_quotes_account_id_fkey", "crm_quotes", "subscriber_accounts", ["account_id"], ["id"])

    op.create_foreign_key("crm_conversations_contact_id_fkey", "crm_conversations", "crm_contacts", ["contact_id"], ["id"])
    op.create_foreign_key("crm_conversations_organization_id_fkey", "crm_conversations", "organizations", ["organization_id"], ["id"])
    op.create_foreign_key("crm_conversations_subscriber_id_fkey", "crm_conversations", "subscribers", ["subscriber_id"], ["id"])
    op.create_foreign_key("crm_conversations_account_id_fkey", "crm_conversations", "subscriber_accounts", ["account_id"], ["id"])

    op.create_foreign_key(
        "crm_messages_contact_channel_id_fkey",
        "crm_messages",
        "crm_contact_channels",
        ["contact_channel_id"],
        ["id"],
    )

    op.execute("ALTER TABLE service_orders DROP CONSTRAINT IF EXISTS service_orders_requested_by_contact_id_fkey")
    op.create_foreign_key(
        "service_orders_requested_by_contact_id_fkey",
        "service_orders",
        "contacts",
        ["requested_by_contact_id"],
        ["id"],
    )

    # Drop new tables/columns in reverse order
    op.drop_index("ix_account_roles_person_id", table_name="account_roles")
    op.drop_index("ix_account_roles_account_id", table_name="account_roles")
    op.drop_table("account_roles")

    op.drop_index("ix_person_merge_logs_target_person_id", table_name="person_merge_logs")
    op.drop_table("person_merge_logs")

    op.drop_index("ix_person_status_logs_person_id", table_name="person_status_logs")
    op.drop_table("person_status_logs")

    op.drop_index("ix_person_channels_address", table_name="person_channels")
    op.drop_index("ix_person_channels_person_id", table_name="person_channels")
    op.drop_table("person_channels")

    # Remove columns from people
    op.drop_constraint("fk_people_organization_id", "people", type_="foreignkey")
    op.drop_column("people", "organization_id")
    op.drop_column("people", "party_status")

    # Drop enums
    op.execute("DROP TYPE IF EXISTS accountroletype")
    op.execute("DROP TYPE IF EXISTS personchanneltype")
    op.execute("DROP TYPE IF EXISTS partystatus")
