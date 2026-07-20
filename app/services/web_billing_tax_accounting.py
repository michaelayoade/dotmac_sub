"""Thin form/context adapter for the tax-accounting operator console."""

from __future__ import annotations

from app.models.payment_proof import WithholdingTaxStatus
from app.services import tax_accounting


def build_page_data(
    db,
    *,
    date_from: str | None,
    date_to: str | None,
    wht_status: str | None = None,
    wht_search: str | None = None,
    wht_page: int = 1,
) -> dict[str, object]:
    try:
        parsed_wht_status = WithholdingTaxStatus(wht_status) if wht_status else None
    except ValueError as exc:
        raise tax_accounting.TaxAccountingError(
            "Unknown withholding-tax status filter"
        ) from exc
    return tax_accounting.build_tax_operations_state(
        db,
        date_from=date_from,
        date_to=date_to,
        wht_status=parsed_wht_status,
        wht_search=wht_search,
        wht_page=wht_page,
    )


def transition_wht(
    db,
    *,
    record_id: str,
    target_status: str,
    actor_id: str | None,
    certificate_reference: str | None,
    notes: str | None,
):
    try:
        status = WithholdingTaxStatus(target_status)
    except ValueError as exc:
        raise tax_accounting.TaxAccountingError(
            "Unknown withholding-tax status"
        ) from exc
    return tax_accounting.transition_withholding_tax(
        db,
        record_id,
        target_status=status,
        actor_id=actor_id,
        certificate_reference=certificate_reference,
        notes=notes,
    )
