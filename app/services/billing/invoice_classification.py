"""Shared invoice classification for AR and prepaid accounting rows."""

from __future__ import annotations

from sqlalchemy import or_, select

from app.models.billing import Invoice, InvoiceLine
from app.models.catalog import BillingMode, Subscription
from app.models.subscriber import Subscriber
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES


def prepaid_non_ar_invoice_ids():
    """Invoice ids that must not behave as postpaid receivables.

    Prepaid renewal rows normally have active subscription lines. Migrated or
    imported rows can be line-less, so those are only classified as prepaid
    non-AR when they have import provenance, belong to a prepaid-capable account,
    and either carry an explicit prepaid metadata tag or the account has no
    collectible postpaid service. Untagged mixed-mode rows stay collectible so
    legitimate postpaid debt is not hidden.
    """
    active_line_invoice_ids = select(InvoiceLine.invoice_id).where(
        InvoiceLine.is_active.is_(True)
    )
    prepaid_account_ids = select(Subscriber.id).where(
        or_(
            Subscriber.billing_mode == BillingMode.prepaid,
            Subscriber.id.in_(
                select(Subscription.subscriber_id).where(
                    Subscription.billing_mode == BillingMode.prepaid
                )
            ),
        )
    )
    postpaid_collectible_account_ids = select(Subscription.subscriber_id).where(
        Subscription.billing_mode == BillingMode.postpaid,
        Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
    )
    explicit_prepaid_metadata = or_(
        Invoice.metadata_["billing_mode"].as_string() == BillingMode.prepaid.value,
        Invoice.metadata_["invoice_billing_mode"].as_string()
        == BillingMode.prepaid.value,
        Invoice.metadata_["source_billing_mode"].as_string()
        == BillingMode.prepaid.value,
    )
    import_provenance = or_(
        Invoice.splynx_invoice_id.is_not(None),
        Invoice.metadata_["imported_via"].as_string() == "system_import_wizard",
    )
    prepaid_line_invoice_ids = (
        select(InvoiceLine.invoice_id)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .where(InvoiceLine.is_active.is_(True))
        .where(Subscription.billing_mode == BillingMode.prepaid)
    )
    legacy_line_less_invoice_ids = (
        select(Invoice.id)
        .where(~Invoice.id.in_(active_line_invoice_ids))
        .where(Invoice.account_id.in_(prepaid_account_ids))
        .where(import_provenance)
        .where(
            or_(
                explicit_prepaid_metadata,
                ~Invoice.account_id.in_(postpaid_collectible_account_ids),
            )
        )
    )
    return prepaid_line_invoice_ids.union(legacy_line_less_invoice_ids)


def collectible_ar_invoice_filter():
    """SQLAlchemy filter for invoices that may behave as collectible AR."""
    return ~Invoice.id.in_(prepaid_non_ar_invoice_ids())
