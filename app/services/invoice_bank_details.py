"""Resolve company bank details for invoice display and exports."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import web_system_company_info as company_info_service
from app.services import web_system_config as web_system_config_service


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalise_account(account: dict[str, object]) -> dict[str, str] | None:
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


def get_invoice_bank_details(db: Session) -> dict[str, str] | None:
    """Return the bank details that should be printed on invoices.

    Direct bank transfer accounts are the source of truth because they already
    capture the payable account name and account number. If no direct-transfer
    account is configured, fall back to the older company-info bank fields.
    """
    context = web_system_config_service.get_direct_bank_transfer_context(db)
    accounts = context.get("direct_bank_transfer_accounts") or []
    if isinstance(accounts, list):
        enabled_accounts = [
            account
            for account in accounts
            if isinstance(account, dict) and account.get("enabled") == "true"
        ]
        for account in [*enabled_accounts, *accounts]:
            if not isinstance(account, dict):
                continue
            normalised = _normalise_account(account)
            if normalised:
                return normalised

    company_info = company_info_service.get_company_info(db)
    bank_name = _clean(company_info.get("company_bank_name"))
    account_number = _clean(company_info.get("company_bank_account"))
    account_name = _clean(company_info.get("company_name"))
    if bank_name and account_number:
        return {
            "bank_name": bank_name,
            "account_name": account_name or "Company Account",
            "account_number": account_number,
            "sort_code": "",
        }
    return None
