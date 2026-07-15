"""Compatibility wrapper for invoice classification helpers.

New code should import from ``app.services.invoice_classification`` so it does
not trigger the heavy ``app.services.billing`` package exports.
"""

from app.services.invoice_classification import (
    collectible_ar_invoice_filter,
    prepaid_non_ar_invoice_ids,
)

__all__ = ["collectible_ar_invoice_filter", "prepaid_non_ar_invoice_ids"]
