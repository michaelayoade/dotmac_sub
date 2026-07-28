"""Thin form/context adapter for the tax-accounting operator console."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.payment_proof import WithholdingTaxStatus
from app.services import tax_accounting
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def build_page_data(
    db: Session,
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
            code="financial.tax_accounting.filter_invalid",
            message="Unknown withholding-tax status filter.",
            details={"field": "wht_status"},
        ) from exc
    return tax_accounting.build_tax_operations_state(
        db,
        date_from=date_from,
        date_to=date_to,
        wht_status=parsed_wht_status,
        wht_search=wht_search,
        wht_page=wht_page,
    ).to_context()


def transition_wht(
    db: Session,
    *,
    record_id: str,
    target_status: str,
    actor_id: str | None,
    certificate_reference: str | None,
    notes: str | None,
):
    if not actor_id:
        raise tax_accounting.TaxAccountingError(
            code="financial.tax_accounting.actor_required",
            message="An authenticated staff actor is required.",
        )
    try:
        status = WithholdingTaxStatus(target_status)
    except ValueError as exc:
        raise tax_accounting.TaxAccountingError(
            code="financial.tax_accounting.target_status_invalid",
            message="Unknown withholding-tax status.",
            details={"field": "target_status"},
        ) from exc
    try:
        normalized_record_id = UUID(record_id)
    except ValueError as exc:
        raise tax_accounting.TaxAccountingError(
            code="financial.tax_accounting.record_id_invalid",
            message="Withholding-tax record identifier must be a UUID.",
            details={"field": "record_id"},
        ) from exc
    db_session_adapter.release_read_transaction(db)
    return tax_accounting.transition_withholding_tax(
        db,
        tax_accounting.TransitionWithholdingTaxCommand(
            record_id=normalized_record_id,
            target_status=status,
            certificate_reference=certificate_reference,
            notes=notes,
        ),
        context=CommandContext.system(
            actor=actor_id,
            scope=f"withholding_tax:{normalized_record_id}",
            reason=f"Transition withholding-tax record to {status.value}",
            idempotency_key=f"withholding-tax:{normalized_record_id}:{status.value}",
        ),
    )
