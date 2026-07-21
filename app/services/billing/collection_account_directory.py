"""Single reader for the Dotmac receiving accounts shown to customers.

`collection_accounts` is the owner of "a Dotmac bank account". Before this module
the same fact lived in four places — the `billing.direct_bank_transfer_accounts`
JSON settings blob, the legacy singular `direct_bank_transfer_*` settings, the
`company_bank_*` company-info fields used as an invoice fallback, and this table
(which was empty). Presentment read the settings; attribution read the table; and
because the thing shown to a customer was a dict parsed from a string rather than
an entity, there was no identity to record on the resulting payment. That is why
"which of our accounts received this money?" was unanswerable.

Every customer-facing surface now resolves through here: the portal top-up
transfer page, the reseller portal, `/api/me`, and invoice bank details.

This module is presentment only. It deliberately does **not** decide *which*
account a given customer should use — that routing lives in
`_resolve_collection_account`, keyed on channel and currency. Nor does it touch
gateway presentment, which belongs to `payment_routing` (health- and
policy-aware). See docs/designs/PAYMENT_CHANNEL_COLLECTION_ACCOUNT_SOT.md.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.billing.collection_accounts import CollectionAccounts


def enabled_transfer_accounts(
    db: Session, *, currency: str = "NGN"
) -> list[dict[str, str]]:
    """Compatibility reader around the collection-account owner."""
    return CollectionAccounts.presentment_accounts(db, currency=currency)


def primary_transfer_account(
    db: Session, *, currency: str = "NGN"
) -> dict[str, str] | None:
    """The account to print on an invoice when only one can be shown."""
    return CollectionAccounts.primary_presentment_account(db, currency=currency)
