"""Versioned numeric readiness policy for the complete fiber cutover cohort.

This owner combines exact read-only evidence from the staged identity,
connectivity, field-verification, and canonical OLT-to-customer topology owners.
It does not select identities, infer geography or connectivity, create work,
mutate topology, or authorize a production cutover. A passing report is only
evidence for an independently reviewed production change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.fiber_topology import audit_fiber_topology
from app.services.network.fiber_topology_connectivity_coverage import (
    reconcile_fiber_connectivity_coverage,
)
from app.services.network.fiber_topology_field_worklist import (
    reconcile_fiber_field_worklist,
)
from app.services.network.fiber_topology_identity_coverage import (
    reconcile_fiber_identity_coverage,
)

BASIS_POINTS = 10_000
GLOBAL_COHORT_NAME = "all_sub_operating_geographies"


class FiberTopologyCutoverReadinessError(ValueError):
    """Raised when exact cutover evidence cannot be evaluated consistently."""


@dataclass(frozen=True)
class FiberTopologyCutoverPolicy:
    policy_version: str
    exact_coverage_bps: int
    required_field_verification_bps: int
    dormant_low_risk_sample_bps: int
    dormant_low_risk_minimum_sample: int
    dormant_discrepancy_expansion_bps: int
    dormant_low_risk_classifier_owner: str | None
    supported_cohort_names: tuple[str, ...]
    required_critical_field_scopes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dormant_discrepancy_expansion_bps": (
                self.dormant_discrepancy_expansion_bps
            ),
            "dormant_low_risk_classifier_owner": (
                self.dormant_low_risk_classifier_owner
            ),
            "dormant_low_risk_minimum_sample": (self.dormant_low_risk_minimum_sample),
            "dormant_low_risk_sample_bps": self.dormant_low_risk_sample_bps,
            "exact_coverage_bps": self.exact_coverage_bps,
            "policy_version": self.policy_version,
            "required_critical_field_scopes": list(self.required_critical_field_scopes),
            "required_field_verification_bps": (self.required_field_verification_bps),
            "supported_cohort_names": list(self.supported_cohort_names),
        }

    @property
    def policy_sha256(self) -> str:
        return _digest(self.to_dict())


FIBER_TOPOLOGY_CUTOVER_POLICY = FiberTopologyCutoverPolicy(
    policy_version="fiber_topology_cutover_v1",
    exact_coverage_bps=BASIS_POINTS,
    required_field_verification_bps=BASIS_POINTS,
    dormant_low_risk_sample_bps=2_000,
    dormant_low_risk_minimum_sample=25,
    dormant_discrepancy_expansion_bps=200,
    dormant_low_risk_classifier_owner=None,
    supported_cohort_names=(GLOBAL_COHORT_NAME,),
    required_critical_field_scopes=(
        "pop_olt",
        "feeder_trunk",
        "cabinet",
        "splitter",
        "customer_bearing_endpoint",
        "changed_or_conflicting_source",
    ),
)


@dataclass(frozen=True)
class FiberTopologyCutoverEvidence:
    cohort_name: str
    identity_report_sha256: str
    identity_total: int
    identity_exact_current: int
    identity_terminal_current: int
    identity_blocker_codes: tuple[str, ...]
    connectivity_report_sha256: str
    connectivity_total: int
    connectivity_exact_current: int
    connectivity_terminal_current: int
    connectivity_blocker_codes: tuple[str, ...]
    topology_report_sha256: str
    topology_blocker_codes: tuple[str, ...]
    customer_trace_total: int
    customer_trace_evaluated: int
    customer_trace_complete: int
    field_worklist_report_sha256: str
    required_field_total: int
    required_field_current_agreement: int
    required_field_blocker_codes: tuple[str, ...]
    represented_critical_field_scopes: tuple[str, ...]
    dormant_low_risk_total: int = 0
    dormant_sample_selected: int = 0
    dormant_sample_current_agreement: int = 0
    dormant_sample_discrepancies: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "cohort_name": self.cohort_name,
            "connectivity": {
                "blocker_codes": list(self.connectivity_blocker_codes),
                "exact_current": self.connectivity_exact_current,
                "report_sha256": self.connectivity_report_sha256,
                "terminal_current": self.connectivity_terminal_current,
                "total": self.connectivity_total,
            },
            "customer_trace": {
                "complete": self.customer_trace_complete,
                "evaluated": self.customer_trace_evaluated,
                "total": self.customer_trace_total,
            },
            "field_verification": {
                "blocker_codes": list(self.required_field_blocker_codes),
                "current_agreement": self.required_field_current_agreement,
                "dormant_low_risk_total": self.dormant_low_risk_total,
                "dormant_sample_current_agreement": (
                    self.dormant_sample_current_agreement
                ),
                "dormant_sample_discrepancies": (self.dormant_sample_discrepancies),
                "dormant_sample_selected": self.dormant_sample_selected,
                "report_sha256": self.field_worklist_report_sha256,
                "represented_critical_field_scopes": list(
                    self.represented_critical_field_scopes
                ),
                "required_total": self.required_field_total,
            },
            "identity": {
                "blocker_codes": list(self.identity_blocker_codes),
                "exact_current": self.identity_exact_current,
                "report_sha256": self.identity_report_sha256,
                "terminal_current": self.identity_terminal_current,
                "total": self.identity_total,
            },
            "topology": {
                "blocker_codes": list(self.topology_blocker_codes),
                "report_sha256": self.topology_report_sha256,
            },
        }


@dataclass(frozen=True)
class FiberTopologyCutoverReadinessReport:
    report_sha256: str
    cohort_sha256: str
    policy_sha256: str
    policy: FiberTopologyCutoverPolicy
    evidence: FiberTopologyCutoverEvidence
    gates: tuple[dict[str, object], ...]
    readiness_blocker_codes: tuple[str, ...]
    dormant_sample_required_count: int
    dormant_asset_class_expansion_required: bool
    ready_for_cutover_review: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "cohort_sha256": self.cohort_sha256,
            "dormant_asset_class_expansion_required": (
                self.dormant_asset_class_expansion_required
            ),
            "dormant_sample_required_count": self.dormant_sample_required_count,
            "evidence": self.evidence.to_dict(),
            "gates": list(self.gates),
            "policy": self.policy.to_dict(),
            "policy_sha256": self.policy_sha256,
            "readiness_blocker_codes": list(self.readiness_blocker_codes),
            "ready_for_cutover_review": self.ready_for_cutover_review,
            "report_sha256": self.report_sha256,
            "schema_version": 1,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _basis_points(numerator: int, denominator: int) -> int:
    if denominator == 0:
        return BASIS_POINTS if numerator == 0 else 0
    return numerator * BASIS_POINTS // denominator


def _coverage_gate(
    code: str,
    *,
    numerator: int,
    denominator: int,
    threshold_bps: int,
    detail: str,
) -> dict[str, object]:
    coverage_bps = _basis_points(numerator, denominator)
    return {
        "code": code,
        "coverage_bps": coverage_bps,
        "denominator": denominator,
        "detail": detail,
        "numerator": numerator,
        "ready": bool(
            denominator > 0
            and coverage_bps >= threshold_bps
            and numerator == denominator
        ),
        "threshold_bps": threshold_bps,
    }


def _zero_gate(code: str, *, count: int, detail: str) -> dict[str, object]:
    return {
        "code": code,
        "count": count,
        "detail": detail,
        "ready": count == 0,
        "threshold": 0,
    }


def _validate_evidence(evidence: FiberTopologyCutoverEvidence) -> None:
    hashes = {
        "connectivity_report_sha256": evidence.connectivity_report_sha256,
        "field_worklist_report_sha256": evidence.field_worklist_report_sha256,
        "identity_report_sha256": evidence.identity_report_sha256,
        "topology_report_sha256": evidence.topology_report_sha256,
    }
    invalid_hashes = [
        name
        for name, value in hashes.items()
        if len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ]
    if invalid_hashes:
        raise FiberTopologyCutoverReadinessError(
            "invalid exact evidence SHA-256 fields: "
            + ", ".join(sorted(invalid_hashes))
        )
    count_fields = {
        "connectivity_exact_current": evidence.connectivity_exact_current,
        "connectivity_terminal_current": evidence.connectivity_terminal_current,
        "connectivity_total": evidence.connectivity_total,
        "customer_trace_complete": evidence.customer_trace_complete,
        "customer_trace_evaluated": evidence.customer_trace_evaluated,
        "customer_trace_total": evidence.customer_trace_total,
        "dormant_low_risk_total": evidence.dormant_low_risk_total,
        "dormant_sample_current_agreement": (evidence.dormant_sample_current_agreement),
        "dormant_sample_discrepancies": evidence.dormant_sample_discrepancies,
        "dormant_sample_selected": evidence.dormant_sample_selected,
        "identity_exact_current": evidence.identity_exact_current,
        "identity_terminal_current": evidence.identity_terminal_current,
        "identity_total": evidence.identity_total,
        "required_field_current_agreement": (evidence.required_field_current_agreement),
        "required_field_total": evidence.required_field_total,
    }
    negative = [name for name, value in count_fields.items() if value < 0]
    if negative:
        raise FiberTopologyCutoverReadinessError(
            f"negative cutover evidence counts: {', '.join(sorted(negative))}"
        )
    relationships = (
        (evidence.identity_exact_current, evidence.identity_total),
        (evidence.identity_terminal_current, evidence.identity_total),
        (evidence.connectivity_exact_current, evidence.connectivity_total),
        (evidence.connectivity_terminal_current, evidence.connectivity_total),
        (evidence.customer_trace_evaluated, evidence.customer_trace_total),
        (evidence.customer_trace_complete, evidence.customer_trace_evaluated),
        (evidence.required_field_current_agreement, evidence.required_field_total),
        (evidence.dormant_sample_selected, evidence.dormant_low_risk_total),
        (
            evidence.dormant_sample_current_agreement,
            evidence.dormant_sample_selected,
        ),
        (evidence.dormant_sample_discrepancies, evidence.dormant_sample_selected),
    )
    if any(numerator > denominator for numerator, denominator in relationships):
        raise FiberTopologyCutoverReadinessError(
            "cutover evidence numerator exceeds its exact cohort denominator"
        )
    if (
        evidence.dormant_sample_current_agreement
        + evidence.dormant_sample_discrepancies
        > evidence.dormant_sample_selected
    ):
        raise FiberTopologyCutoverReadinessError(
            "dormant sample agreement and discrepancy counts overlap"
        )


def _dormant_sample_required(total: int) -> int:
    if total == 0:
        return 0
    policy = FIBER_TOPOLOGY_CUTOVER_POLICY
    percentage = (
        total * policy.dormant_low_risk_sample_bps + BASIS_POINTS - 1
    ) // BASIS_POINTS
    return min(total, max(policy.dormant_low_risk_minimum_sample, percentage))


def evaluate_fiber_cutover_readiness(
    evidence: FiberTopologyCutoverEvidence,
) -> FiberTopologyCutoverReadinessReport:
    """Apply the one checked-in numeric policy to an exact cohort snapshot."""

    _validate_evidence(evidence)
    policy = FIBER_TOPOLOGY_CUTOVER_POLICY
    cohort_supported = evidence.cohort_name in policy.supported_cohort_names
    identity_owner_blockers = len(evidence.identity_blocker_codes)
    connectivity_owner_blockers = len(evidence.connectivity_blocker_codes)
    topology_blockers = len(evidence.topology_blocker_codes)
    field_blockers = len(evidence.required_field_blocker_codes)
    expected_field_total = evidence.identity_total + evidence.connectivity_total
    missing_critical_scopes = sorted(
        set(policy.required_critical_field_scopes)
        - set(evidence.represented_critical_field_scopes)
    )
    dormant_sample_required = _dormant_sample_required(evidence.dormant_low_risk_total)
    discrepancy_expansion_required = bool(
        evidence.dormant_sample_selected
        and evidence.dormant_sample_discrepancies * BASIS_POINTS
        > evidence.dormant_sample_selected * policy.dormant_discrepancy_expansion_bps
    )
    if discrepancy_expansion_required:
        dormant_sample_required = evidence.dormant_low_risk_total

    dormant_selection_ready = (
        evidence.dormant_sample_selected >= dormant_sample_required
    )
    dormant_agreement_ready = (
        evidence.dormant_sample_current_agreement == evidence.dormant_sample_selected
    )
    gates: tuple[dict[str, object], ...] = (
        {
            "code": "cohort_scope_is_explicit_and_supported",
            "cohort_name": evidence.cohort_name,
            "detail": (
                "The policy accepts only the complete global latest-source and "
                "active-fiber cohort until an exact geographic membership owner exists."
            ),
            "ready": cohort_supported,
        },
        _coverage_gate(
            "identity_exact_current_coverage",
            numerator=evidence.identity_exact_current,
            denominator=evidence.identity_total,
            threshold_bps=policy.exact_coverage_bps,
            detail="Every latest staged point identity is exactly covered once.",
        ),
        _coverage_gate(
            "identity_review_result_provenance_coverage",
            numerator=evidence.identity_terminal_current,
            denominator=evidence.identity_total,
            threshold_bps=policy.exact_coverage_bps,
            detail=(
                "Every latest staged point identity has current terminal review, "
                "result, and provenance evidence."
            ),
        ),
        _zero_gate(
            "identity_owner_blockers_zero",
            count=identity_owner_blockers,
            detail="No identity source, ambiguity, drift, or pending gate is blocked.",
        ),
        _coverage_gate(
            "connectivity_exact_current_coverage",
            numerator=evidence.connectivity_exact_current,
            denominator=evidence.connectivity_total,
            threshold_bps=policy.exact_coverage_bps,
            detail="Every latest staged fiber path is exactly covered once.",
        ),
        _coverage_gate(
            "connectivity_review_result_provenance_coverage",
            numerator=evidence.connectivity_terminal_current,
            denominator=evidence.connectivity_total,
            threshold_bps=policy.exact_coverage_bps,
            detail=(
                "Every latest staged path has current terminal review, result, "
                "and provenance evidence."
            ),
        ),
        _zero_gate(
            "connectivity_owner_blockers_zero",
            count=connectivity_owner_blockers,
            detail=(
                "No connectivity source, ambiguity, drift, or pending gate is blocked."
            ),
        ),
        _zero_gate(
            "canonical_topology_blockers_zero",
            count=topology_blockers,
            detail="The canonical OLT-to-customer topology audit has no blockers.",
        ),
        _coverage_gate(
            "customer_bearing_paths_exhaustively_evaluated",
            numerator=evidence.customer_trace_evaluated,
            denominator=evidence.customer_trace_total,
            threshold_bps=policy.exact_coverage_bps,
            detail="Every active fiber subscription is included in the trace audit.",
        ),
        _coverage_gate(
            "customer_bearing_paths_traceable",
            numerator=evidence.customer_trace_complete,
            denominator=evidence.customer_trace_total,
            threshold_bps=policy.exact_coverage_bps,
            detail="Every active fiber subscription has one complete validated path.",
        ),
        {
            "code": "field_source_cohort_matches_coverage_cohorts",
            "detail": (
                "The required field cohort contains every latest staged identity "
                "and connectivity source row exactly once."
            ),
            "expected": expected_field_total,
            "observed": evidence.required_field_total,
            "ready": bool(
                expected_field_total > 0
                and evidence.required_field_total == expected_field_total
            ),
        },
        {
            "code": "critical_field_scope_contract_complete",
            "detail": (
                "POP/OLT, feeder/trunk, cabinet, splitter, customer endpoint, and "
                "changed/conflicting field scopes must all have authoritative evidence."
            ),
            "missing_scopes": missing_critical_scopes,
            "ready": not missing_critical_scopes,
        },
        _coverage_gate(
            "required_field_rows_current_agreement",
            numerator=evidence.required_field_current_agreement,
            denominator=evidence.required_field_total,
            threshold_bps=policy.required_field_verification_bps,
            detail=(
                "Every non-dormant or not-yet-classified latest source row has "
                "current agreeing field evidence."
            ),
        ),
        _zero_gate(
            "required_field_blockers_zero",
            count=field_blockers,
            detail="No required field row is pending, conflicting, stale, or drifted.",
        ),
        {
            "code": "dormant_low_risk_classification_authoritative",
            "classifier_owner": policy.dormant_low_risk_classifier_owner,
            "detail": (
                "Rows may leave the 100% required field cohort only through a "
                "named authoritative dormant-low-risk classifier."
            ),
            "ready": bool(
                evidence.dormant_low_risk_total == 0
                or policy.dormant_low_risk_classifier_owner
            ),
        },
        {
            "code": "dormant_low_risk_sample_selection_complete",
            "detail": (
                "Explicit dormant low-risk rows require a 20% audit with a "
                "25-row minimum; a discrepancy rate above 2% expands the class "
                "to complete review."
            ),
            "ready": dormant_selection_ready,
            "required": dormant_sample_required,
            "selected": evidence.dormant_sample_selected,
        },
        {
            "code": "dormant_low_risk_sample_evidence_current",
            "detail": "Every selected dormant audit row has current agreeing evidence.",
            "ready": dormant_agreement_ready,
            "required": evidence.dormant_sample_selected,
            "verified": evidence.dormant_sample_current_agreement,
        },
        _zero_gate(
            "known_dormant_sample_discrepancies_zero",
            count=evidence.dormant_sample_discrepancies,
            detail=(
                "Any known sample discrepancy blocks cutover review; above 2% also "
                "expands the dormant asset class to complete review."
            ),
        ),
    )
    readiness_blockers = tuple(
        str(gate["code"]) for gate in gates if not bool(gate["ready"])
    )
    ready = not readiness_blockers
    cohort_payload = evidence.to_dict()
    cohort_sha256 = _digest(cohort_payload)
    report_payload: dict[str, object] = {
        "cohort_sha256": cohort_sha256,
        "dormant_asset_class_expansion_required": (discrepancy_expansion_required),
        "dormant_sample_required_count": dormant_sample_required,
        "evidence": cohort_payload,
        "gates": list(gates),
        "policy": policy.to_dict(),
        "policy_sha256": policy.policy_sha256,
        "readiness_blocker_codes": list(readiness_blockers),
        "ready_for_cutover_review": ready,
        "schema_version": 1,
    }
    return FiberTopologyCutoverReadinessReport(
        report_sha256=_digest(report_payload),
        cohort_sha256=cohort_sha256,
        policy_sha256=policy.policy_sha256,
        policy=policy,
        evidence=evidence,
        gates=gates,
        readiness_blocker_codes=readiness_blockers,
        dormant_sample_required_count=dormant_sample_required,
        dormant_asset_class_expansion_required=discrepancy_expansion_required,
        ready_for_cutover_review=ready,
    )


def ensure_cutover_readiness_repeatable_snapshot(db: Session) -> None:
    """Require one consistent PostgreSQL snapshot for the complete read."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return
    isolation_level = db.connection().get_isolation_level().upper()
    if isolation_level not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise FiberTopologyCutoverReadinessError(
            "fiber cutover readiness requires a fresh REPEATABLE READ transaction"
        )


def reconcile_fiber_cutover_readiness(
    db: Session,
) -> FiberTopologyCutoverReadinessReport:
    """Reconcile the one supported complete cohort without writing state."""

    ensure_cutover_readiness_repeatable_snapshot(db)
    identity = reconcile_fiber_identity_coverage(db)
    connectivity = reconcile_fiber_connectivity_coverage(db)
    field = reconcile_fiber_field_worklist(db)
    topology = audit_fiber_topology(db, verify_customer_traces=True)
    trace = topology.trace_coverage
    if trace is None:
        raise FiberTopologyCutoverReadinessError(
            "exhaustive customer trace evidence was not produced"
        )

    identity_blockers = tuple(
        f"identity:{gate['code']}" for gate in identity.gates if not bool(gate["ready"])
    )
    connectivity_blockers = tuple(
        f"connectivity:{gate['code']}"
        for gate in connectivity.gates
        if not bool(gate["ready"])
    )
    field_blockers = tuple(
        f"field:{state}"
        for state, count in sorted(field.state_counts.items())
        if state != "current_agreement" and count
    )
    topology_blockers = tuple(
        finding.code for finding in topology.findings if finding.severity == "blocker"
    )
    topology_payload = topology.to_dict()
    evidence = FiberTopologyCutoverEvidence(
        cohort_name=GLOBAL_COHORT_NAME,
        identity_report_sha256=identity.coverage_report_sha256,
        identity_total=identity.staged_point_count,
        identity_exact_current=identity.coverage_counts["exact"],
        identity_terminal_current=(
            identity.lifecycle_counts["applied_current"]
            + identity.lifecycle_counts["rejected_current"]
        ),
        identity_blocker_codes=identity_blockers,
        connectivity_report_sha256=connectivity.coverage_report_sha256,
        connectivity_total=connectivity.staged_path_count,
        connectivity_exact_current=connectivity.coverage_counts["exact"],
        connectivity_terminal_current=(
            connectivity.lifecycle_counts["applied_current"]
            + connectivity.lifecycle_counts["rejected_current"]
        ),
        connectivity_blocker_codes=connectivity_blockers,
        topology_report_sha256=_digest(topology_payload),
        topology_blocker_codes=topology_blockers,
        customer_trace_total=trace.total_subscriptions,
        customer_trace_evaluated=trace.evaluated_subscriptions,
        customer_trace_complete=trace.complete_traces,
        field_worklist_report_sha256=field.report_sha256,
        required_field_total=field.staged_feature_count,
        required_field_current_agreement=field.current_agreement_count,
        required_field_blocker_codes=field_blockers,
        represented_critical_field_scopes=(
            "cabinet",
            "changed_or_conflicting_source",
            "feeder_trunk",
        ),
    )
    return evaluate_fiber_cutover_readiness(evidence)


__all__ = [
    "BASIS_POINTS",
    "FIBER_TOPOLOGY_CUTOVER_POLICY",
    "GLOBAL_COHORT_NAME",
    "FiberTopologyCutoverEvidence",
    "FiberTopologyCutoverPolicy",
    "FiberTopologyCutoverReadinessError",
    "FiberTopologyCutoverReadinessReport",
    "ensure_cutover_readiness_repeatable_snapshot",
    "evaluate_fiber_cutover_readiness",
    "reconcile_fiber_cutover_readiness",
]
