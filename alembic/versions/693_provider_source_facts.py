"""Freeze first verified provider source facts without RLS-dependent reads.

Revision ID: 693_provider_source_facts
Revises: 692_module_invoice_cash

Integrity prerequisite only: source classification does not qualify cash.
No historical rewrite or insert restriction; legacy admission remains owned
by the provider-event service. Processing and raw transport payload may change.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "693_provider_source_facts"
down_revision: str = "692_module_invoice_cash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("LOCK TABLE public.payment_provider_events IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        CREATE FUNCTION public.guard_verified_provider_source_facts() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                RAISE EXCEPTION 'provider observations cannot be deleted or truncated'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.source IN ('verified_webhook', 'gateway_reconciliation') AND
                ROW(NEW.id, NEW.provider_id, NEW.event_type, NEW.external_id,
                    NEW.idempotency_key, NEW.source, NEW.observation_digest,
                    NEW.observed_payment_status, NEW.amount, NEW.provider_fee,
                    NEW.net_amount, NEW.provider_reference, NEW.currency,
                    NEW.financial_effect, NEW.occurred_at, NEW.received_at)
                IS DISTINCT FROM
                ROW(OLD.id, OLD.provider_id, OLD.event_type, OLD.external_id,
                    OLD.idempotency_key, OLD.source, OLD.observation_digest,
                    OLD.observed_payment_status, OLD.amount, OLD.provider_fee,
                    OLD.net_amount, OLD.provider_reference, OLD.currency,
                    OLD.financial_effect, OLD.occurred_at, OLD.received_at)
            THEN
                RAISE EXCEPTION 'verified provider source facts are immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER verified_provider_source_facts_immutable
        BEFORE UPDATE OR DELETE ON public.payment_provider_events
        FOR EACH ROW EXECUTE FUNCTION public.guard_verified_provider_source_facts()
    """)
    op.execute("""
        CREATE TRIGGER provider_source_facts_no_truncate
        BEFORE TRUNCATE ON public.payment_provider_events
        FOR EACH STATEMENT EXECUTE FUNCTION public.guard_verified_provider_source_facts()
    """)
    for trigger in (
        "verified_provider_source_facts_immutable",
        "provider_source_facts_no_truncate",
    ):
        op.execute(
            f"ALTER TABLE public.payment_provider_events ENABLE ALWAYS TRIGGER {trigger}"
        )


def downgrade() -> None:
    raise RuntimeError(
        "Provider source fact integrity is forward-only; use a reviewed forward migration"
    )
