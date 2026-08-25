"""Total, deterministic legacy-fact disposition for Billing backfill."""

from __future__ import annotations

import hashlib

from dotmac_billing import DueDateBasisStatus

from sub_billing_adoption.contracts import (
    DispositionResultV1,
    EvidenceState,
    FactLifecycle,
    LegacyDisposition,
    LegacyFactKind,
    LegacyFinancialFactV1,
    SourceAuthority,
    due_date_basis_is_collectible,
)


def _missing_evidence(fact: LegacyFinancialFactV1) -> tuple[str, ...]:
    missing: list[str] = []
    if fact.source_version is None or not fact.source_version.strip():
        missing.append("source_version")
    if fact.fact_kind is LegacyFactKind.INVOICE:
        if fact.due_date_basis is None:
            missing.append("due_date_basis")
        elif fact.due_date_basis.status is DueDateBasisStatus.UNKNOWN_UNVERIFIED:
            missing.append("verified_due_date_basis")
    if (
        fact.fact_kind
        in {
            LegacyFactKind.SETTLEMENT,
            LegacyFactKind.REFUND,
            LegacyFactKind.REVERSAL,
        }
        and fact.settlement_evidence is EvidenceState.MISSING
    ):
        missing.append("confirmed_settlement_evidence")
    if (
        fact.fact_kind is LegacyFactKind.ALLOCATION
        and fact.allocation_evidence is EvidenceState.MISSING
    ):
        missing.append("allocation_edge_evidence")
    if fact.tax_evidence is EvidenceState.MISSING:
        missing.append("tax_provenance")
    if fact.fx_evidence is EvidenceState.MISSING:
        missing.append("fx_provenance")
    return tuple(sorted(missing))


def _fingerprint(fact: LegacyFinancialFactV1, missing: tuple[str, ...]) -> str:
    basis = fact.due_date_basis
    parts = (
        str(fact.tenant_id),
        fact.fact_kind.value,
        str(fact.fact_id),
        str(fact.account_id),
        fact.currency,
        str(fact.minor_units),
        format(fact.amount, "f"),
        fact.source_authority.value,
        fact.lifecycle.value,
        fact.source_ref,
        fact.source_version or "",
        fact.observed_at.isoformat(),
        "" if basis is None else basis.status.value,
        "" if basis is None else basis.source_authority,
        "" if basis is None else basis.evidence_ref,
        fact.settlement_evidence.value,
        fact.allocation_evidence.value,
        fact.tax_evidence.value,
        fact.fx_evidence.value,
        "1" if fact.known_incorrect else "0",
        *missing,
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def classify_legacy_fact(fact: LegacyFinancialFactV1) -> DispositionResultV1:
    """Classify every admitted fact into exactly one S0 disposition."""

    missing = _missing_evidence(fact)
    if fact.known_incorrect:
        disposition = LegacyDisposition.KNOWN_INCORRECT_NATIVE_FACT
    elif fact.source_authority is SourceAuthority.PROVIDER_OWNED:
        disposition = LegacyDisposition.PROVIDER_PROJECTION
    elif not missing:
        disposition = LegacyDisposition.TARGET_BACKFILL
    elif fact.lifecycle is FactLifecycle.CLOSED:
        disposition = LegacyDisposition.CLOSED_LEGACY_ARCHIVE
    else:
        disposition = LegacyDisposition.CUTOVER_BLOCKER

    collectible = (
        disposition is LegacyDisposition.TARGET_BACKFILL
        and fact.fact_kind is LegacyFactKind.INVOICE
        and due_date_basis_is_collectible(fact.due_date_basis)
    )
    return DispositionResultV1(
        fact_id=fact.fact_id,
        fact_kind=fact.fact_kind,
        disposition=disposition,
        missing_evidence=missing,
        collectible=collectible,
        accounting_reemit_allowed=False,
        evidence_fingerprint=_fingerprint(fact, missing),
    )


__all__ = ["classify_legacy_fact"]
