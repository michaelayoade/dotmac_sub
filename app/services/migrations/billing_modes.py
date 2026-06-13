"""Authoritative Splynx billing-type → DotMac BillingMode mapping.

The customer's billing type lives in Splynx's ``customers.billing_type``
column (values: ``prepaid_monthly``, ``prepaid``, ``recurring``) — NOT in
``services_internet`` (which has no billing_type column) nor in
``customer_billing.type`` (an unrelated integer that is 1 for every customer).

Reading the wrong source is exactly what silently defaulted the entire base
to prepaid during migration, so this single helper is the one place the
mapping is defined. Import it everywhere a billing mode is derived from
Splynx.
"""

from __future__ import annotations

from app.models.catalog import BillingMode

# Splynx billing_type (lower-cased) -> DotMac billing mode.
# Only "recurring" is invoice-based/postpaid; both prepaid variants draw the
# monthly charge down from the customer deposit balance.
BILLING_TYPE_MAP = {
    "recurring": "postpaid",
    "prepaid": "prepaid",
    "prepaid_monthly": "prepaid",
}


def map_billing_mode(billing_type_raw: str | None) -> BillingMode:
    """Map a Splynx ``customers.billing_type`` value to a DotMac BillingMode.

    Unknown/empty values default to prepaid — the overwhelming majority — but
    callers should pass the real ``customers.billing_type``, never a field
    from ``services_internet`` (which does not carry it).
    """
    mapped = BILLING_TYPE_MAP.get((billing_type_raw or "").strip().lower(), "prepaid")
    return BillingMode.postpaid if mapped == "postpaid" else BillingMode.prepaid
