"""Billing services package.

This package provides billing-related services including invoices, payments,
credit notes, ledger entries, and tax rates.

All existing import patterns are preserved for backward compatibility:
    from app.services import billing as billing_service
    billing_service.invoices.create(db, payload)

    from app.services.billing import Invoices, invoices
    from app.services.billing import _recalculate_invoice_totals
"""

from app.services.billing.invoices import Invoices, InvoiceLines
from app.services.billing.credit_notes import CreditNotes, CreditNoteLines, CreditNoteApplications
from app.services.billing.payments import (
    Payments,
    PaymentMethods,
    BankAccounts,
    PaymentAllocations,
    PaymentChannels,
    CollectionAccounts,
    PaymentChannelAccounts,
)
from app.services.billing.providers import PaymentProviders, PaymentProviderEvents
from app.services.billing.ledger import LedgerEntries
from app.services.billing.tax import TaxRates
from app.services.billing.runs import BillingRuns
from app.services.billing.reporting import BillingReporting, billing_reporting

# Export common helpers used by billing_automation
from app.services.billing._common import (
    _validate_account,
    _validate_invoice_totals,
    _validate_credit_note_totals,
    _validate_invoice_line_amount,
    _resolve_tax_rate,
    _validate_invoice_currency,
    _recalculate_invoice_totals,
    _recalculate_credit_note_totals,
    _validate_payment_linkages,
    _validate_payment_provider,
    _validate_ledger_linkages,
)

# Singleton instances for service access
invoices = Invoices()
invoice_lines = InvoiceLines()
credit_notes = CreditNotes()
credit_note_lines = CreditNoteLines()
credit_note_applications = CreditNoteApplications()
payment_methods = PaymentMethods()
bank_accounts = BankAccounts()
payments = Payments()
payment_allocations = PaymentAllocations()
payment_channels = PaymentChannels()
collection_accounts = CollectionAccounts()
payment_channel_accounts = PaymentChannelAccounts()
ledger_entries = LedgerEntries()
tax_rates = TaxRates()
billing_runs = BillingRuns()
payment_providers = PaymentProviders()
payment_provider_events = PaymentProviderEvents()

__all__ = [
    # Classes
    "Invoices",
    "InvoiceLines",
    "CreditNotes",
    "CreditNoteLines",
    "CreditNoteApplications",
    "Payments",
    "PaymentMethods",
    "BankAccounts",
    "PaymentAllocations",
    "PaymentChannels",
    "CollectionAccounts",
    "PaymentChannelAccounts",
    "PaymentProviders",
    "PaymentProviderEvents",
    "LedgerEntries",
    "TaxRates",
    "BillingRuns",
    "BillingReporting",
    # Singleton instances
    "invoices",
    "invoice_lines",
    "credit_notes",
    "credit_note_lines",
    "credit_note_applications",
    "payment_methods",
    "bank_accounts",
    "payments",
    "payment_allocations",
    "payment_channels",
    "collection_accounts",
    "payment_channel_accounts",
    "ledger_entries",
    "tax_rates",
    "billing_runs",
    "payment_providers",
    "payment_provider_events",
    "billing_reporting",
    # Helper functions (for billing_automation compatibility)
    "_validate_account",
    "_validate_invoice_totals",
    "_validate_credit_note_totals",
    "_validate_invoice_line_amount",
    "_resolve_tax_rate",
    "_validate_invoice_currency",
    "_recalculate_invoice_totals",
    "_recalculate_credit_note_totals",
    "_validate_payment_linkages",
    "_validate_payment_provider",
    "_validate_ledger_linkages",
]
