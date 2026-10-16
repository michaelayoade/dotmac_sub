"""Prospective invoice intent correlation and persistent legacy-writer fence.

Revision ID: 691_payments_intent_correlation
Revises: 690_backdated_invoice_permissions

Source preparation only: installation requires reviewed writer drain and image
recovery barriers. Historical rows are neither linked nor financially changed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "691_payments_intent_correlation"
down_revision: str = "690_backdated_invoice_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str = "pm_0003_confirmation_immutable"

# Keep this exact body bound to the read-only installed-schema floor digest.
GUARD_BODY = """
DECLARE
    invoice_gateway boolean;
    old_invoice_gateway boolean;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        old_invoice_gateway := coalesce(OLD.provider_type IS DISTINCT FROM 'direct_bank_transfer'
            AND OLD.purpose IS DISTINCT FROM 'prepaid_period_purchase'
            AND (OLD.invoice_id IS NOT NULL
                 OR OLD.metadata->>'payment_flow' = 'invoice_payment'
                 OR OLD.metadata->>'invoice_id' IS NOT NULL), false);
    END IF;
    IF TG_OP <> 'INSERT' AND (OLD.module_intent_id IS NOT NULL OR old_invoice_gateway) THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'gateway invoice intent cannot be deleted' USING ERRCODE = 'restrict_violation';
        END IF;
        IF ROW(OLD.id, OLD.module_intent_id, OLD.module_intent_tenant_id,
               OLD.account_id, OLD.billing_account_id, OLD.invoice_id,
               OLD.reference, OLD.provider_type, OLD.provider_id,
               OLD.capability_binding_id, OLD.currency, OLD.requested_amount,
               OLD.expires_at, OLD.created_at, OLD.channel, OLD.created_by, OLD.purpose,
               OLD.metadata->>'payment_flow', OLD.metadata->>'invoice_id',
               OLD.metadata->>'account_id', OLD.metadata->>'payment_method_id',
               OLD.metadata->>'provider_id', OLD.metadata->>'capability_binding_id')
           IS DISTINCT FROM
           ROW(NEW.id, NEW.module_intent_id, NEW.module_intent_tenant_id,
               NEW.account_id, NEW.billing_account_id, NEW.invoice_id,
               NEW.reference, NEW.provider_type, NEW.provider_id,
               NEW.capability_binding_id, NEW.currency, NEW.requested_amount,
               NEW.expires_at, NEW.created_at, NEW.channel, NEW.created_by, NEW.purpose,
               NEW.metadata->>'payment_flow', NEW.metadata->>'invoice_id',
               NEW.metadata->>'account_id', NEW.metadata->>'payment_method_id',
               NEW.metadata->>'provider_id', NEW.metadata->>'capability_binding_id')
        THEN
            RAISE EXCEPTION 'gateway invoice intent binding is immutable' USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    invoice_gateway := coalesce(NEW.provider_type IS DISTINCT FROM 'direct_bank_transfer'
        AND NEW.purpose IS DISTINCT FROM 'prepaid_period_purchase'
        AND (NEW.invoice_id IS NOT NULL
             OR NEW.metadata->>'payment_flow' = 'invoice_payment'
             OR NEW.metadata->>'invoice_id' IS NOT NULL), false);
    -- Historical unlinked rows retain their lifecycle. Linking history is not
    -- an activation/backfill mechanism, including by an UPDATE after install.
    IF TG_OP = 'UPDATE' AND OLD.module_intent_id IS NULL THEN
        IF NEW.module_intent_id IS NOT NULL OR NEW.module_intent_tenant_id IS NOT NULL THEN
            RAISE EXCEPTION 'historical invoice intent cannot be linked' USING ERRCODE = 'restrict_violation';
        END IF;
        IF invoice_gateway AND NOT old_invoice_gateway THEN
            RAISE EXCEPTION 'unlinked intent cannot become gateway invoice intent' USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND invoice_gateway AND NEW.module_intent_id IS NULL THEN
        RAISE EXCEPTION 'gateway invoice intent requires Payments correlation' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.module_intent_id IS NOT NULL THEN
        IF NOT coalesce(invoice_gateway, false) OR NEW.invoice_id IS NULL
            OR NEW.account_id IS NULL OR NEW.module_intent_tenant_id IS NULL
            OR NEW.purpose IS NOT NULL
            OR NEW.module_intent_tenant_id IS DISTINCT FROM public.app_current_tenant_id()
            OR NEW.module_intent_tenant_id <> '8c7ae830-51fc-52ae-9818-d84b2a35e568'::uuid
        THEN
            RAISE EXCEPTION 'invalid Payments invoice intent scope' USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("LOCK TABLE public.topup_intents IN ACCESS EXCLUSIVE MODE")
    # Current ORM metadata is used by Sub's squashed base; these two columns
    # can already exist there. The cross-schema constraints remain DDL-owned.
    for column in ("module_intent_id", "module_intent_tenant_id"):
        op.execute(
            f"ALTER TABLE public.topup_intents ADD COLUMN IF NOT EXISTS {column} uuid"
        )
    op.create_check_constraint(
        "ck_topup_module_intent_pair",
        "topup_intents",
        "(module_intent_id IS NULL) = (module_intent_tenant_id IS NULL)",
        schema="public",
    )
    op.create_foreign_key(
        "fk_topup_module_intent_scope",
        "topup_intents",
        "payment_intents",
        ["module_intent_tenant_id", "module_intent_id"],
        ["tenant_id", "id"],
        source_schema="public",
        referent_schema="mod_payments",
        ondelete="RESTRICT",
    )
    op.execute(
        "CREATE FUNCTION public.guard_payments_invoice_intent() RETURNS trigger "
        "LANGUAGE plpgsql SET search_path = pg_catalog AS $$" + GUARD_BODY + "$$"
    )
    op.execute(
        "CREATE TRIGGER guard_payments_invoice_intent BEFORE INSERT OR UPDATE OR DELETE "
        "ON public.topup_intents FOR EACH ROW EXECUTE FUNCTION public.guard_payments_invoice_intent()"
    )
    # The caller stages module, projection and event in one runtime transaction.
    # The module's released migration already grants its own DML/schema usage.
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.topup_intents TO app_user")
    op.execute("GRANT SELECT, INSERT ON public.event_store TO app_user")
    # PostgreSQL locking SELECT requires UPDATE on at least one column. This
    # is a real identity-DML privilege, not a read-only grant; its temporary
    # necessity ends when receivables callers and the global legacy fence move.
    op.execute("GRANT SELECT ON public.subscribers, public.invoices TO app_user")
    op.execute("GRANT UPDATE (id) ON public.subscribers TO app_user")
    op.execute("GRANT UPDATE (id) ON public.invoices TO app_user")
    op.execute(
        "GRANT SELECT ON public.integration_capability_bindings, "
        "public.integration_installations, public.domain_settings TO app_user"
    )


def downgrade() -> None:
    # Installing the fence raises the image floor even before the first row.
    # A row-count-conditioned downgrade would reopen incumbent writes.
    raise RuntimeError(
        "Payments invoice intent floor is permanent; use a reviewed forward migration"
    )
