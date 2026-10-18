"""Census-to-observation assembly for the opening rehearsal (Step 5).

Maps Sub's opening census preview (``BillingOpeningExportPreviewV2``)
onto the pinned ``dotmac-billing`` rehearsal observation contract
(``OpeningSubObservationV2``) field for field. This is pure evidence
translation: it verifies nothing, qualifies no cash, and invents no
originals — every gap the census carries (blocking exceptions,
unqualified cash, incomplete populations) passes through as counted
blockers for the rehearsal report, which is the exceptions queue the
manual review works from.

Manifest/command assembly and the rehearsal run itself build on this
observation in later slices; nothing here writes money state.
"""

from __future__ import annotations

from collections import defaultdict

from dotmac_billing import (
    OpeningBlockerV1,
    OpeningObservedControlV1,
    OpeningObservedInvoiceV1,
    OpeningObservedLaneV1,
    OpeningObservedPopulationV1,
    OpeningObservedPrecisionV1,
    OpeningObservedSettlementV1,
    OpeningSubObservationV2,
)

from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreviewV2,
    CashSourceKind,
    OpeningException,
)

__all__ = ["build_opening_sub_observation"]

_CASH_IDENTITIES: tuple[tuple[CashSourceKind, str], ...] = (
    (CashSourceKind.payments, "payments"),
    (CashSourceKind.payment_settlements, "payment_settlements"),
    (CashSourceKind.payment_provider_events, "provider_events"),
    (CashSourceKind.integration_inbox, "inbox_receipts"),
    (CashSourceKind.payment_refunds, "refunds"),
    (CashSourceKind.payment_reversals, "reversals"),
    (CashSourceKind.payment_proofs, "proofs"),
    (CashSourceKind.payment_proof_corrections, "proof_corrections"),
    (CashSourceKind.topup_intents, "topups"),
)


def _blockers(
    exceptions: tuple[OpeningException, ...],
) -> tuple[OpeningBlockerV1, ...]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for exception in exceptions:
        grouped[str(exception.code)].append(str(exception.source_id))
    return tuple(
        OpeningBlockerV1(
            code=code,
            count=len(references),
            references=tuple(sorted(references)),
        )
        for code, references in sorted(grouped.items())
    )


def build_opening_sub_observation(
    preview: BillingOpeningExportPreviewV2,
) -> OpeningSubObservationV2:
    """Translate one census preview into the rehearsal observation DTO."""

    declared = preview.capture.declared_source
    cash = preview.cash_evidence
    counts = {row.table: row.count for row in cash.source_counts}
    populations = tuple(
        OpeningObservedPopulationV1(
            table=str(kind),
            count=counts.get(kind, len(getattr(cash, attribute))),
            identities=tuple(
                sorted(
                    (row.id for row in getattr(cash, attribute)),
                    key=str,
                )
            ),
        )
        for kind, attribute in _CASH_IDENTITIES
    )
    return OpeningSubObservationV2(
        source_repository_sha=declared.source_repository_sha,
        installation_id=declared.installation_id,
        schema_heads=preview.capture.schema_heads,
        snapshot_id=preview.capture.snapshot_id,
        captured_at=preview.capture.captured_at,
        economic_cutoff=declared.economic_cutoff,
        isolation=preview.capture.isolation,
        population_version=declared.population_version,
        population_complete=preview.capture.population_complete,
        currency_precisions=tuple(
            OpeningObservedPrecisionV1(currency=currency, precision=precision)
            for currency, precision in declared.currency_precisions
        ),
        content_fingerprint=preview.content_fingerprint,
        invoice_ids=tuple(invoice.invoice_id for invoice in preview.invoices),
        eligible_invoice_ids=preview.eligible_invoice_ids,
        controls=tuple(
            OpeningObservedControlV1(
                account_id=control.account_id,
                currency=control.currency,
                invoice_count=control.invoice_count,
                eligible_count=control.eligible_count,
                excluded_count=control.excluded_count,
                exception_count=control.exception_count,
                original_total=control.original_total,
                canonical_discharge=control.canonical_discharge,
                residual=control.residual,
                eligible_residual=control.eligible_residual,
                source_count=control.source_count,
            )
            for control in preview.controls
        ),
        lanes=tuple(
            OpeningObservedLaneV1(
                account_id=lane.account_id,
                currency=lane.currency,
                fingerprint=lane.fingerprint,
                authority=lane.authority.value,
                authority_cutover_id=lane.authority_cutover_id,
                posting_group_count=lane.posting_group_count,
                collectible_receivable=lane.collectible_receivable,
                unapplied_customer_credit=lane.unapplied_customer_credit,
                prepaid_funding_reserved=lane.prepaid_funding_reserved,
                prepaid_funding_consumed=lane.prepaid_funding_consumed,
                written_off_total=lane.written_off_total,
                refunded_total=lane.refunded_total,
                adjustment_total=lane.adjustment_total,
            )
            for lane in preview.lanes
        ),
        cash_populations=populations,
        qualified_payment_ids=cash.qualified_payment_ids,
        blockers=_blockers(preview.blocking_exceptions + cash.blocking_exceptions),
        invoices=tuple(
            OpeningObservedInvoiceV1(
                invoice_id=invoice.invoice_id,
                account_id=invoice.account_id,
                currency=invoice.currency,
                precision=invoice.precision,
                invoice_number=invoice.invoice_number,
                issued_at=invoice.issued_at,
                total=invoice.total,
                canonical_discharge=invoice.canonical_discharge,
                residual=invoice.residual,
                settlement_ids=invoice.settlement_ids,
            )
            for invoice in preview.invoices
        ),
        settlements=tuple(
            OpeningObservedSettlementV1(
                kind=str(settlement.kind),
                id=settlement.id,
                invoice_id=settlement.invoice_id,
                source_id=settlement.source_id,
                amount=settlement.amount,
                active=settlement.active,
            )
            for settlement in preview.settlements
        ),
    )
