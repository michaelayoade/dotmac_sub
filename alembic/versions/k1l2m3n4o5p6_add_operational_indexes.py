"""Add operational indexes for audit, billing, subscriber, network, and auth tables.

These indexes cover the main query patterns used by dashboards, listings,
reports, billing automation, and session management.  Every CREATE is
guarded by an existence check so the migration is idempotent.

Revision ID: k1l2m3n4o5p7
Revises: i9j0k1l2m3n4
Create Date: 2026-03-22
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "k1l2m3n4o5p7"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def _ix(conn, table_name: str, name: str, ddl: str) -> None:
    """Create index only if it does not already exist."""
    inspector = inspect(conn)
    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    exists = name in existing_indexes
    if not exists:
        conn.execute(text(ddl))


def upgrade() -> None:
    conn = op.get_bind()

    # ── audit_events ─────────────────────────────────────────────────
    # Every listing/feed query sorts by occurred_at DESC.
    _ix(conn, "audit_events", "ix_audit_events_occurred_at_desc",
        'CREATE INDEX ix_audit_events_occurred_at_desc ON audit_events (occurred_at DESC)')

    # Activity feeds filter is_active + sort occurred_at.
    _ix(conn, "audit_events", "ix_audit_events_active_occurred",
        'CREATE INDEX ix_audit_events_active_occurred ON audit_events (is_active, occurred_at DESC)')

    # Subscriber/entity detail pages: lookup by entity.
    _ix(conn, "audit_events", "ix_audit_events_entity",
        'CREATE INDEX ix_audit_events_entity ON audit_events (entity_type, entity_id)')

    # Actor-scoped queries (user audit trail).
    _ix(conn, "audit_events", "ix_audit_events_actor",
        'CREATE INDEX ix_audit_events_actor ON audit_events (actor_type, actor_id)')

    # Request correlation.
    _ix(conn, "audit_events", "ix_audit_events_request_id",
        'CREATE INDEX ix_audit_events_request_id ON audit_events (request_id)')

    # ── invoices ─────────────────────────────────────────────────────
    # Most invoice queries filter by account + active + status.
    _ix(conn, "invoices", "ix_invoices_account_active_status",
        'CREATE INDEX ix_invoices_account_active_status ON invoices (account_id, is_active, status)')

    # Billing overview, AR aging, collections: status + is_active.
    _ix(conn, "invoices", "ix_invoices_status_active",
        'CREATE INDEX ix_invoices_status_active ON invoices (status, is_active)')

    # Due-date ordering for auto-allocation and AR aging.
    _ix(conn, "invoices", "ix_invoices_due_at",
        'CREATE INDEX ix_invoices_due_at ON invoices (due_at)')

    # Report queries filter/group by issued_at.
    _ix(conn, "invoices", "ix_invoices_issued_at",
        'CREATE INDEX ix_invoices_issued_at ON invoices (issued_at)')

    # Dashboard recent invoices, trend charts.
    _ix(conn, "invoices", "ix_invoices_created_at",
        'CREATE INDEX ix_invoices_created_at ON invoices (created_at)')

    # Billing cycle idempotency: find existing invoice for period.
    _ix(conn, "invoices", "ix_invoices_account_period",
        'CREATE INDEX ix_invoices_account_period ON invoices (account_id, billing_period_start, billing_period_end)')

    # ── invoice_lines ────────────────────────────────────────────────
    # Billing cycle idempotency checks subscription+invoice.
    _ix(conn, "invoice_lines", "ix_invoice_lines_subscription",
        'CREATE INDEX ix_invoice_lines_subscription ON invoice_lines (subscription_id)')

    _ix(conn, "invoice_lines", "ix_invoice_lines_invoice",
        'CREATE INDEX ix_invoice_lines_invoice ON invoice_lines (invoice_id)')

    # ── payments ─────────────────────────────────────────────────────
    # Payment listings filter by account + status + active.
    _ix(conn, "payments", "ix_payments_account_status",
        'CREATE INDEX ix_payments_account_status ON payments (account_id, status, is_active)')

    # Dashboard charts use coalesce(paid_at, created_at) but a plain
    # paid_at index still helps the ORDER BY and range scans.
    _ix(conn, "payments", "ix_payments_paid_at",
        'CREATE INDEX ix_payments_paid_at ON payments (paid_at)')

    _ix(conn, "payments", "ix_payments_created_at",
        'CREATE INDEX ix_payments_created_at ON payments (created_at)')

    # ── payment_allocations ──────────────────────────────────────────
    # Joins from invoice side (void, credit-note application).
    _ix(conn, "payment_allocations", "ix_payment_allocations_invoice",
        'CREATE INDEX ix_payment_allocations_invoice ON payment_allocations (invoice_id)')

    # ── credit_notes ─────────────────────────────────────────────────
    _ix(conn, "credit_notes", "ix_credit_notes_account_status",
        'CREATE INDEX ix_credit_notes_account_status ON credit_notes (account_id, status, is_active)')

    _ix(conn, "credit_notes", "ix_credit_notes_invoice",
        'CREATE INDEX ix_credit_notes_invoice ON credit_notes (invoice_id)')

    # ── subscribers ──────────────────────────────────────────────────
    # Customer lists, billing automation, reseller dashboards.
    _ix(conn, "subscribers", "ix_subscribers_status",
        'CREATE INDEX ix_subscribers_status ON subscribers (status)')

    _ix(conn, "subscribers", "ix_subscribers_is_active",
        'CREATE INDEX ix_subscribers_is_active ON subscribers (is_active)')

    _ix(conn, "subscribers", "ix_subscribers_reseller",
        'CREATE INDEX ix_subscribers_reseller ON subscribers (reseller_id)')

    _ix(conn, "subscribers", "ix_subscribers_organization",
        'CREATE INDEX ix_subscribers_organization ON subscribers (organization_id)')

    _ix(conn, "subscribers", "ix_subscribers_user_type",
        'CREATE INDEX ix_subscribers_user_type ON subscribers (user_type)')

    _ix(conn, "subscribers", "ix_subscribers_created_at",
        'CREATE INDEX ix_subscribers_created_at ON subscribers (created_at)')

    # Reseller portal: filter by reseller + exclude system users.
    _ix(conn, "subscribers", "ix_subscribers_reseller_type",
        'CREATE INDEX ix_subscribers_reseller_type ON subscribers (reseller_id, user_type)')

    # ── ont_assignments ──────────────────────────────────────────────
    # PON port views, outage impact queries.
    _ix(conn, "ont_assignments", "ix_ont_assignments_pon_port_active",
        'CREATE INDEX ix_ont_assignments_pon_port_active ON ont_assignments (pon_port_id, active)')

    _ix(conn, "ont_assignments", "ix_ont_assignments_subscriber",
        'CREATE INDEX ix_ont_assignments_subscriber ON ont_assignments (subscriber_id)')

    _ix(conn, "ont_assignments", "ix_ont_assignments_subscription",
        'CREATE INDEX ix_ont_assignments_subscription ON ont_assignments (subscription_id)')

    # ── radius_active_sessions ───────────────────────────────────────
    # Session lookups by subscriber + NAS, ordered by start.
    _ix(conn, "radius_active_sessions", "ix_radius_sessions_subscriber_start",
        'CREATE INDEX ix_radius_sessions_subscriber_start ON radius_active_sessions (subscriber_id, session_start DESC)')

    _ix(conn, "radius_active_sessions", "ix_radius_sessions_nas_start",
        'CREATE INDEX ix_radius_sessions_nas_start ON radius_active_sessions (nas_device_id, session_start DESC)')

    # Stale-session cleanup filters by last_update.
    _ix(conn, "radius_active_sessions", "ix_radius_sessions_last_update",
        'CREATE INDEX ix_radius_sessions_last_update ON radius_active_sessions (last_update)')

    # ── sessions (auth) ──────────────────────────────────────────────
    # Active-session lookups for a principal.
    _ix(conn, "sessions", "ix_sessions_subscriber_active",
        'CREATE INDEX ix_sessions_subscriber_active ON sessions (subscriber_id, status, revoked_at)')

    _ix(conn, "sessions", "ix_sessions_system_user_active",
        'CREATE INDEX ix_sessions_system_user_active ON sessions (system_user_id, status, revoked_at)')

    # ── event_store ──────────────────────────────────────────────────
    # Retry sweep: status + created_at for failed events.
    _ix(conn, "event_store", "ix_event_store_status_created",
        'CREATE INDEX ix_event_store_status_created ON event_store (status, created_at)')

    # Stale-processing detection: status + updated_at.
    _ix(conn, "event_store", "ix_event_store_status_updated",
        'CREATE INDEX ix_event_store_status_updated ON event_store (status, updated_at)')


def downgrade() -> None:
    conn = op.get_bind()
    indexes = [
        # event_store
        "ix_event_store_status_updated",
        "ix_event_store_status_created",
        # sessions
        "ix_sessions_system_user_active",
        "ix_sessions_subscriber_active",
        # radius_active_sessions
        "ix_radius_sessions_last_update",
        "ix_radius_sessions_nas_start",
        "ix_radius_sessions_subscriber_start",
        # ont_assignments
        "ix_ont_assignments_subscription",
        "ix_ont_assignments_subscriber",
        "ix_ont_assignments_pon_port_active",
        # subscribers
        "ix_subscribers_reseller_type",
        "ix_subscribers_created_at",
        "ix_subscribers_user_type",
        "ix_subscribers_organization",
        "ix_subscribers_reseller",
        "ix_subscribers_is_active",
        "ix_subscribers_status",
        # credit_notes
        "ix_credit_notes_invoice",
        "ix_credit_notes_account_status",
        # payment_allocations
        "ix_payment_allocations_invoice",
        # payments
        "ix_payments_created_at",
        "ix_payments_paid_at",
        "ix_payments_account_status",
        # invoice_lines
        "ix_invoice_lines_invoice",
        "ix_invoice_lines_subscription",
        # invoices
        "ix_invoices_account_period",
        "ix_invoices_created_at",
        "ix_invoices_issued_at",
        "ix_invoices_due_at",
        "ix_invoices_status_active",
        "ix_invoices_account_active_status",
        # audit_events
        "ix_audit_events_request_id",
        "ix_audit_events_actor",
        "ix_audit_events_entity",
        "ix_audit_events_active_occurred",
        "ix_audit_events_occurred_at_desc",
    ]
    for name in indexes:
        conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
