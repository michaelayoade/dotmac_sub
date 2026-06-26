"""Force billing automation ON and arm the drift alarm.

Post-cutover DotMac is the biller of record, so the local billing engine must
stay on. ``ensure_by_key`` is insert-if-missing, so a seed/deploy never flips an
existing row — this migration authoritatively sets:

- ``billing.billing_enabled = true``           (master switch ON)
- ``billing.billing_enabled_expected = true``   (so check_billing_switch alarms
                                                 CRITICAL hourly if it ever drifts
                                                 OFF — i.e. it can't be silently
                                                 switched off)

value_json is cleared and value_text set to "true" to match how booleans are
stored (resolver reads value_json ?? value_text), guaranteeing TRUE regardless
of any prior value. Idempotent via ON CONFLICT.

Revision ID: 179_billing_always_on
Revises: 178_ipv6_delegated_prefixes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "179_billing_always_on"
down_revision = "178_ipv6_delegated_prefixes"
branch_labels = None
depends_on = None

_KEYS = ("billing_enabled", "billing_enabled_expected")


_UPSERT = sa.text(
    """
    INSERT INTO domain_settings (
        id, domain, key, value_type, value_text, value_json,
        is_secret, is_active, created_at, updated_at
    )
    VALUES (
        gen_random_uuid(), :domain, :key, 'boolean', 'true', NULL,
        false, true, now(), now()
    )
    ON CONFLICT (domain, key)
    DO UPDATE SET
        value_text = 'true',
        value_json = NULL,
        value_type = 'boolean',
        is_active = true,
        updated_at = now()
    """
)


def upgrade() -> None:
    # Master switch + drift-expectation (billing domain).
    for key in _KEYS:
        op.execute(_UPSERT.bindparams(domain="billing", key=key))
    # Billing MODULE on (modules domain) — the single control plane reads this.
    op.execute(_UPSERT.bindparams(domain="modules", key="module_billing_enabled"))


def downgrade() -> None:
    # Intentional no-op: a downgrade must never silently disable live billing.
    # Remove the pinned-expected row only, so the drift guard reverts to its
    # pre-migration (unpinned) behavior without touching the master switch.
    op.execute(
        "DELETE FROM domain_settings "
        "WHERE domain = 'billing' AND key = 'billing_enabled_expected'"
    )
