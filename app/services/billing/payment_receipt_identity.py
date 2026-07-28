"""Stable, non-secret customer receipt identity derived from a payment UUID."""

from __future__ import annotations

from uuid import UUID


def payment_receipt_reference(
    payment_id: UUID,
    stored_receipt_number: str | None = None,
) -> str:
    stored = str(stored_receipt_number or "").strip()
    if stored:
        return stored if stored.startswith("#") else f"#{stored}"
    return f"#RCP-{payment_id.hex[:8].upper()}"


def payment_receipt_path(payment_id: UUID) -> str:
    return f"/portal/billing/payments/{payment_id}/receipt"


__all__ = ["payment_receipt_path", "payment_receipt_reference"]
