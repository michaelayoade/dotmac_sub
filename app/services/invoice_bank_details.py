"""Resolve company bank details for invoice display and exports."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.services.billing import collection_account_directory


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalise_account(account: Mapping[str, object]) -> dict[str, str] | None:
    bank_name = _clean(account.get("bank_name"))
    account_name = _clean(account.get("account_name"))
    account_number = _clean(account.get("account_number"))
    if not (bank_name and account_name and account_number):
        return None
    return {
        "bank_name": bank_name,
        "account_name": account_name,
        "account_number": account_number,
        "sort_code": _clean(account.get("sort_code")),
    }


def get_invoice_bank_details(
    db: Session, *, currency: str = "NGN"
) -> dict[str, str] | None:
    """Return the bank details that should be printed on invoices.

    Resolved from `collection_accounts`, the owner of a Dotmac receiving account,
    so an invoice names the same account the portal offers the same customer.

    The previous implementation read the `direct_bank_transfer_accounts` settings
    blob and fell back to the `company_bank_*` company-info fields. Both were
    copies of the same fact, and the fallback could silently print a *different*
    account from the one presentment offered. Returning ``None`` when nothing is
    configured is the correct outcome: an invoice with no bank details is
    recoverable, an invoice naming the wrong account is not.
    """
    account = collection_account_directory.primary_transfer_account(
        db, currency=currency
    )
    return _normalise_account(account) if account else None
