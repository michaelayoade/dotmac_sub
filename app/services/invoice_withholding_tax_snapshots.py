"""Immutable customer-WHT evidence captured when an invoice is issued."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.domain_settings import SettingDomain
from app.services import customer_tax_policies
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.settings_spec import resolve_value

WITHHOLDING_TAX_RATE_SETTING = "withholding_tax_rate_percent"


class InvoiceWithholdingTaxSnapshotError(DomainError, ValueError):
    """Invoice issue cannot produce complete withholding-tax evidence."""


def _error(
    suffix: str, message: str, **details: object
) -> InvoiceWithholdingTaxSnapshotError:
    return InvoiceWithholdingTaxSnapshotError(
        code=f"financial.invoice_withholding_tax_snapshot.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingTaxSnapshot:
    policy_enabled: bool
    policy_version: int
    rate_percent: Decimal | None
    taxable_basis: Decimal
    withholding_tax_amount: Decimal
    net_bank_transfer_payable: Decimal

    def transfer_metadata(self, invoice: Invoice) -> dict[str, object] | None:
        if not self.policy_enabled:
            return None
        return {
            "schema_version": 1,
            "account_id": str(invoice.account_id),
            "policy_version": self.policy_version,
            "rate_provenance": WITHHOLDING_TAX_RATE_SETTING,
            "source_invoice_id": str(invoice.id),
            "currency": str(invoice.currency or "").strip().upper(),
            "vat_exclusive_amount": str(self.taxable_basis),
            "vat_amount": str(invoice.tax_total),
            "gross_amount": str(invoice.total),
            "withholding_tax_rate_percent": str(self.rate_percent),
            "withholding_tax_amount": str(self.withholding_tax_amount),
            "net_amount": str(self.net_bank_transfer_payable),
        }


def _rate_percent(db: Session) -> Decimal:
    raw = resolve_value(db, SettingDomain.billing, WITHHOLDING_TAX_RATE_SETTING)
    try:
        value = round_money(to_decimal(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "configuration_invalid",
            "Configured withholding-tax percentage is invalid",
            setting=WITHHOLDING_TAX_RATE_SETTING,
        ) from exc
    if value <= Decimal("0.00") or value >= Decimal("100.00"):
        raise _error(
            "configuration_invalid",
            "Configured withholding-tax percentage must be greater than 0 and less than 100",
            setting=WITHHOLDING_TAX_RATE_SETTING,
        )
    return value


def _stored_snapshot(invoice: Invoice) -> InvoiceWithholdingTaxSnapshot:
    return InvoiceWithholdingTaxSnapshot(
        policy_enabled=bool(invoice.withholding_tax_policy_enabled),
        policy_version=int(invoice.withholding_tax_policy_version or 0),
        rate_percent=invoice.withholding_tax_rate,
        taxable_basis=round_money(invoice.withholding_tax_taxable_basis or 0),
        withholding_tax_amount=round_money(invoice.withholding_tax_amount or 0),
        net_bank_transfer_payable=round_money(invoice.bank_transfer_net_payable or 0),
    )


def stage_invoice_withholding_tax_snapshot(
    db: Session, *, invoice: Invoice
) -> InvoiceWithholdingTaxSnapshot:
    """Write the one-time issue-time snapshot, or return the stored evidence.

    This participant is deliberately flush-only.  Invoice creation and issuance
    owners retain transaction ownership.
    """
    if invoice.withholding_tax_policy_enabled is not None:
        return _stored_snapshot(invoice)
    if invoice.status != InvoiceStatus.issued or invoice.is_proforma:
        raise _error(
            "invoice_not_issuable",
            "Withholding-tax evidence can be captured only for an issued invoice",
            invoice_id=str(invoice.id),
        )

    subtotal = round_money(invoice.subtotal or 0)
    vat_amount = round_money(invoice.tax_total or 0)
    total = round_money(invoice.total or 0)
    balance_due = round_money(invoice.balance_due or total)
    if subtotal < Decimal("0.00") or total < Decimal("0.00"):
        raise _error(
            "basis_unavailable",
            "Invoice withholding-tax basis is invalid",
            invoice_id=str(invoice.id),
        )
    if total != round_money(subtotal + vat_amount):
        raise _error(
            "basis_unavailable",
            "Invoice withholding-tax basis is inconsistent",
            invoice_id=str(invoice.id),
        )
    if balance_due != total:
        raise _error(
            "basis_unavailable",
            "Invoice withholding-tax snapshot requires an unsettled gross balance",
            invoice_id=str(invoice.id),
        )

    policy = customer_tax_policies.get_customer_withholding_tax_policy(
        db, account_id=invoice.account_id
    )
    invoice.withholding_tax_policy_enabled = policy.withholding_tax_enabled
    invoice.withholding_tax_policy_version = policy.version
    invoice.withholding_tax_taxable_basis = subtotal
    invoice.bank_transfer_net_payable = total
    invoice.withholding_tax_amount = Decimal("0.00")
    invoice.withholding_tax_rate = None
    invoice.withholding_tax_rate_provenance = None

    if policy.withholding_tax_enabled:
        if subtotal <= Decimal("0.00") or total <= Decimal("0.00"):
            raise _error(
                "basis_unavailable",
                "Invoice cannot use automatic withholding tax because the tax basis is unavailable",
                invoice_id=str(invoice.id),
            )
        rate_percent = _rate_percent(db)
        withholding_tax_amount = round_money(
            subtotal * rate_percent / Decimal("100.00")
        )
        net_payable = round_money(total - withholding_tax_amount)
        if withholding_tax_amount <= Decimal("0.00") or net_payable <= Decimal("0.00"):
            raise _error(
                "basis_unavailable",
                "Invoice cannot use automatic withholding tax with the current configuration",
                invoice_id=str(invoice.id),
            )
        invoice.withholding_tax_rate = rate_percent
        invoice.withholding_tax_rate_provenance = WITHHOLDING_TAX_RATE_SETTING
        invoice.withholding_tax_amount = withholding_tax_amount
        invoice.bank_transfer_net_payable = net_payable

    db.flush()
    return _stored_snapshot(invoice)
