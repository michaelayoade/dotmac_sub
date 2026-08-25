"""Typed preparation boundary for Sub's dotmac-billing tenant adoption."""

from sub_billing_adoption.classification import classify_legacy_fact
from sub_billing_adoption.contracts import (
    AuthorityPhase,
    CoupledAuthorityWatermarkV1,
    EvidenceState,
    FactLifecycle,
    LegacyDisposition,
    LegacyFactKind,
    LegacyFinancialFactV1,
    SourceAuthority,
)
from sub_billing_adoption.cutover import (
    CutoverEvidenceV1,
    RollbackDisposition,
    evaluate_cutover_readiness,
    rollback_disposition,
)
from sub_billing_adoption.reconciliation import reconcile_shadow
from sub_billing_adoption.shadow import run_shadow

__version__ = "0.1.0a1"

__all__ = [
    "AuthorityPhase",
    "CoupledAuthorityWatermarkV1",
    "CutoverEvidenceV1",
    "EvidenceState",
    "FactLifecycle",
    "LegacyDisposition",
    "LegacyFactKind",
    "LegacyFinancialFactV1",
    "RollbackDisposition",
    "SourceAuthority",
    "classify_legacy_fact",
    "evaluate_cutover_readiness",
    "reconcile_shadow",
    "rollback_disposition",
    "run_shadow",
]
