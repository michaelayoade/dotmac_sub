"""Read-only ONT assignment invariant audit and constraint cutover gate.

This owner reports exact persisted disagreements. It never chooses replacement
identity, creates repair decisions, mutates assignments, or enables database
constraints. Every repair remains an explicit independently reviewed command in
``network.ont_assignment_identity``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network_subscriber_bridge import (
    AssignmentSubscriptionSnapshot,
    assignment_subscription_snapshots,
)

REPAIR_OWNER = "network.ont_assignment_identity"

REASON_LABELS = {
    "active_release_marker": "Active assignment carries release evidence",
    "assignment_ont_pon_mismatch": "Assignment and ONT disagree on PON",
    "assignment_pon_not_found": "Assignment PON no longer exists",
    "assignment_pon_olt_mismatch": "Assignment PON and ONT disagree on OLT",
    "assignment_pon_olt_not_found": "Assignment PON OLT no longer exists",
    "duplicate_active_ont": "ONT has multiple active assignments",
    "duplicate_active_subscription": "Subscription has multiple active ONTs",
    "inactive_assignment_pon": "Active assignment points to an inactive PON",
    "inactive_assignment_pon_olt": "Active assignment PON belongs to an inactive OLT",
    "inactive_ont": "Active assignment points to an inactive ONT",
    "inactive_ont_olt": "Active assignment ONT points to an inactive OLT",
    "inactive_ont_pon": "Active assignment ONT points to an inactive PON",
    "missing_assigned_at": "Active assignment has no assignment timestamp",
    "missing_assignment_pon": "Assignment has no exact PON",
    "missing_ont_olt": "ONT has no exact OLT",
    "missing_ont_pon": "ONT has no exact PON",
    "missing_subscriber": "Assignment has no subscriber projection",
    "missing_subscription": "Assignment has no exact subscription",
    "ont_not_found": "Assigned ONT no longer exists",
    "ont_olt_not_found": "ONT OLT no longer exists",
    "ont_pon_not_found": "ONT PON no longer exists",
    "ont_pon_olt_mismatch": "ONT PON and ONT disagree on OLT",
    "subscriber_projection_mismatch": "Subscriber differs from exact subscription",
    "subscription_not_found": "Assigned subscription no longer exists",
    "terminal_subscription": "Assigned subscription is terminal",
}

_REASON_ORDER = tuple(REASON_LABELS)

_GATE_CODES = {
    "active_assignment_required_identity": frozenset(
        {
            "active_release_marker",
            "missing_assigned_at",
            "missing_assignment_pon",
            "missing_subscriber",
            "missing_subscription",
            "subscription_not_found",
            "terminal_subscription",
            "subscriber_projection_mismatch",
        }
    ),
    "one_active_assignment_per_ont": frozenset({"duplicate_active_ont"}),
    "one_active_assignment_per_subscription": frozenset(
        {"duplicate_active_subscription"}
    ),
    "active_assignment_exact_network_target": frozenset(
        {
            "assignment_ont_pon_mismatch",
            "assignment_pon_not_found",
            "assignment_pon_olt_mismatch",
            "assignment_pon_olt_not_found",
            "inactive_assignment_pon",
            "inactive_assignment_pon_olt",
            "inactive_ont",
            "inactive_ont_olt",
            "inactive_ont_pon",
            "missing_ont_olt",
            "missing_ont_pon",
            "ont_not_found",
            "ont_olt_not_found",
            "ont_pon_not_found",
            "ont_pon_olt_mismatch",
        }
    ),
}


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _models_by_id(
    db: Session, model: type[object], ids: set[uuid.UUID]
) -> dict[uuid.UUID, object]:
    if not ids:
        return {}
    return {
        row.id: row  # type: ignore[attr-defined]
        for row in db.scalars(select(model).where(model.id.in_(ids)))  # type: ignore[attr-defined]
    }


def _assignment_evidence(assignment: OntAssignment) -> dict[str, object]:
    return {
        "active": assignment.active,
        "assigned_at": _timestamp(assignment.assigned_at),
        "id": str(assignment.id),
        "ont_unit_id": str(assignment.ont_unit_id),
        "pon_port_id": (
            str(assignment.pon_port_id) if assignment.pon_port_id else None
        ),
        "release_reason": assignment.release_reason,
        "released_at": _timestamp(assignment.released_at),
        "subscriber_id": (
            str(assignment.subscriber_id) if assignment.subscriber_id else None
        ),
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else None
        ),
    }


def _ont_evidence(ont: OntUnit | None) -> dict[str, object] | None:
    if ont is None:
        return None
    return {
        "id": str(ont.id),
        "is_active": ont.is_active,
        "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else None,
        "pon_port_id": str(ont.pon_port_id) if ont.pon_port_id else None,
        "serial_number": ont.serial_number,
    }


def _subscription_evidence(
    subscription: AssignmentSubscriptionSnapshot | None,
) -> dict[str, object] | None:
    if subscription is None:
        return None
    return {
        "id": str(subscription.id),
        "status": subscription.status,
        "subscriber_id": str(subscription.subscriber_id),
    }


def _pon_evidence(
    pon: PonPort | None, olt_by_id: dict[uuid.UUID, OLTDevice]
) -> dict[str, object] | None:
    if pon is None:
        return None
    olt = olt_by_id.get(pon.olt_id)
    return {
        "id": str(pon.id),
        "is_active": pon.is_active,
        "olt": ({"id": str(olt.id), "is_active": olt.is_active} if olt else None),
        "olt_id": str(pon.olt_id),
    }


@dataclass(frozen=True)
class OntAssignmentCutoverFinding:
    assignment_id: uuid.UUID
    ont_unit_id: uuid.UUID
    ont_serial_number: str
    subscription_id: uuid.UUID | None
    subscriber_id: uuid.UUID | None
    assignment_pon_port_id: uuid.UUID | None
    ont_pon_port_id: uuid.UUID | None
    ont_olt_id: uuid.UUID | None
    reason_codes: tuple[str, ...]
    related_assignment_ids: tuple[uuid.UUID, ...]
    exact_evidence: dict[str, object]
    input_sha256: str
    repair_owner: str = REPAIR_OWNER
    allowed_repair_actions: tuple[str, ...] = ("canonicalize", "deactivate")

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return tuple(REASON_LABELS[code] for code in self.reason_codes)

    @property
    def review_path(self) -> str:
        return (
            "/admin/network/ont-identity-reviews/new?primary_assignment_id="
            f"{self.assignment_id}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_repair_actions": list(self.allowed_repair_actions),
            "assignment_id": str(self.assignment_id),
            "assignment_pon_port_id": (
                str(self.assignment_pon_port_id)
                if self.assignment_pon_port_id
                else None
            ),
            "exact_evidence": self.exact_evidence,
            "input_sha256": self.input_sha256,
            "ont_olt_id": str(self.ont_olt_id) if self.ont_olt_id else None,
            "ont_pon_port_id": (
                str(self.ont_pon_port_id) if self.ont_pon_port_id else None
            ),
            "ont_serial_number": self.ont_serial_number,
            "ont_unit_id": str(self.ont_unit_id),
            "reason_codes": list(self.reason_codes),
            "reason_labels": list(self.reason_labels),
            "related_assignment_ids": [
                str(value) for value in self.related_assignment_ids
            ],
            "repair_owner": self.repair_owner,
            "review_path": self.review_path,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "subscription_id": (
                str(self.subscription_id) if self.subscription_id else None
            ),
        }


@dataclass(frozen=True)
class OntAssignmentConstraintGate:
    name: str
    blocking_codes: tuple[str, ...]
    blocking_assignment_ids: tuple[uuid.UUID, ...]

    @property
    def ready(self) -> bool:
        return not self.blocking_assignment_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "blocking_assignment_ids": [
                str(value) for value in self.blocking_assignment_ids
            ],
            "blocking_codes": list(self.blocking_codes),
            "name": self.name,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class OntAssignmentCutoverAudit:
    active_assignment_count: int
    clean_assignment_count: int
    findings: tuple[OntAssignmentCutoverFinding, ...]
    reason_counts: dict[str, int]
    gates: tuple[OntAssignmentConstraintGate, ...]
    report_sha256: str

    @property
    def blocker_assignment_count(self) -> int:
        return len(self.findings)

    @property
    def blocker_reason_count(self) -> int:
        return sum(self.reason_counts.values())

    @property
    def ready_for_constraints(self) -> bool:
        return all(gate.ready for gate in self.gates)

    def to_dict(self) -> dict[str, object]:
        return {
            "active_assignment_count": self.active_assignment_count,
            "blocker_assignment_count": self.blocker_assignment_count,
            "blocker_reason_count": self.blocker_reason_count,
            "clean_assignment_count": self.clean_assignment_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "gates": [gate.to_dict() for gate in self.gates],
            "ready_for_constraints": self.ready_for_constraints,
            "reason_counts": self.reason_counts,
            "repair_owner": REPAIR_OWNER,
            "report_sha256": self.report_sha256,
        }


def audit_ont_assignment_cutover(db: Session) -> OntAssignmentCutoverAudit:
    """Exhaustively audit active assignment invariants without writing."""

    assignments = list(
        db.scalars(
            select(OntAssignment)
            .where(OntAssignment.active.is_(True))
            .order_by(OntAssignment.id)
        )
    )
    ont_by_id = {
        key: value
        for key, value in _models_by_id(
            db, OntUnit, {assignment.ont_unit_id for assignment in assignments}
        ).items()
        if isinstance(value, OntUnit)
    }
    subscription_by_id = assignment_subscription_snapshots(
        db,
        {
            assignment.subscription_id
            for assignment in assignments
            if assignment.subscription_id is not None
        },
    )
    pon_ids = {
        assignment.pon_port_id
        for assignment in assignments
        if assignment.pon_port_id is not None
    }
    pon_ids.update(
        ont.pon_port_id for ont in ont_by_id.values() if ont.pon_port_id is not None
    )
    pon_by_id = {
        key: value
        for key, value in _models_by_id(db, PonPort, pon_ids).items()
        if isinstance(value, PonPort)
    }
    olt_ids = {
        ont.olt_device_id for ont in ont_by_id.values() if ont.olt_device_id is not None
    }
    olt_ids.update(pon.olt_id for pon in pon_by_id.values())
    olt_by_id = {
        key: value
        for key, value in _models_by_id(db, OLTDevice, olt_ids).items()
        if isinstance(value, OLTDevice)
    }

    assignments_by_ont: defaultdict[uuid.UUID, list[OntAssignment]] = defaultdict(list)
    assignments_by_subscription: defaultdict[uuid.UUID, list[OntAssignment]] = (
        defaultdict(list)
    )
    for assignment in assignments:
        assignments_by_ont[assignment.ont_unit_id].append(assignment)
        if assignment.subscription_id is not None:
            assignments_by_subscription[assignment.subscription_id].append(assignment)

    findings: list[OntAssignmentCutoverFinding] = []
    for assignment in assignments:
        reasons: set[str] = set()
        ont = ont_by_id.get(assignment.ont_unit_id)
        subscription = (
            subscription_by_id.get(assignment.subscription_id)
            if assignment.subscription_id is not None
            else None
        )
        assignment_pon = (
            pon_by_id.get(assignment.pon_port_id)
            if assignment.pon_port_id is not None
            else None
        )
        ont_pon = (
            pon_by_id.get(ont.pon_port_id)
            if ont is not None and ont.pon_port_id is not None
            else None
        )

        if len(assignments_by_ont[assignment.ont_unit_id]) > 1:
            reasons.add("duplicate_active_ont")
        if (
            assignment.subscription_id is not None
            and len(assignments_by_subscription[assignment.subscription_id]) > 1
        ):
            reasons.add("duplicate_active_subscription")
        if assignment.assigned_at is None:
            reasons.add("missing_assigned_at")
        if assignment.released_at is not None or assignment.release_reason is not None:
            reasons.add("active_release_marker")

        if assignment.subscriber_id is None:
            reasons.add("missing_subscriber")
        if assignment.subscription_id is None:
            reasons.add("missing_subscription")
        elif subscription is None:
            reasons.add("subscription_not_found")
        else:
            if not subscription.assignment_eligible:
                reasons.add("terminal_subscription")
            if (
                assignment.subscriber_id is not None
                and assignment.subscriber_id != subscription.subscriber_id
            ):
                reasons.add("subscriber_projection_mismatch")

        if ont is None:
            reasons.add("ont_not_found")
        else:
            if ont.is_active is False:
                reasons.add("inactive_ont")
            if ont.pon_port_id is None:
                reasons.add("missing_ont_pon")
            elif ont_pon is None:
                reasons.add("ont_pon_not_found")
            elif ont_pon.is_active is False:
                reasons.add("inactive_ont_pon")
            if ont.olt_device_id is None:
                reasons.add("missing_ont_olt")
            else:
                ont_olt = olt_by_id.get(ont.olt_device_id)
                if ont_olt is None:
                    reasons.add("ont_olt_not_found")
                elif ont_olt.is_active is False:
                    reasons.add("inactive_ont_olt")

        if assignment.pon_port_id is None:
            reasons.add("missing_assignment_pon")
        elif assignment_pon is None:
            reasons.add("assignment_pon_not_found")
        else:
            if assignment_pon.is_active is False:
                reasons.add("inactive_assignment_pon")
            assignment_olt = olt_by_id.get(assignment_pon.olt_id)
            if assignment_olt is None:
                reasons.add("assignment_pon_olt_not_found")
            elif assignment_olt.is_active is False:
                reasons.add("inactive_assignment_pon_olt")

        if ont is not None:
            if (
                assignment.pon_port_id is not None
                and ont.pon_port_id is not None
                and assignment.pon_port_id != ont.pon_port_id
            ):
                reasons.add("assignment_ont_pon_mismatch")
            if (
                assignment_pon is not None
                and ont.olt_device_id is not None
                and assignment_pon.olt_id != ont.olt_device_id
            ):
                reasons.add("assignment_pon_olt_mismatch")
            if (
                ont_pon is not None
                and ont.olt_device_id is not None
                and ont_pon.olt_id != ont.olt_device_id
            ):
                reasons.add("ont_pon_olt_mismatch")

        if not reasons:
            continue

        related_ids = {
            row.id
            for row in assignments_by_ont[assignment.ont_unit_id]
            if row.id != assignment.id
        }
        if assignment.subscription_id is not None:
            related_ids.update(
                row.id
                for row in assignments_by_subscription[assignment.subscription_id]
                if row.id != assignment.id
            )
        ordered_reasons = tuple(code for code in _REASON_ORDER if code in reasons)
        ordered_related = tuple(sorted(related_ids, key=str))
        exact_evidence: dict[str, object] = {
            "assignment": _assignment_evidence(assignment),
            "assignment_pon": _pon_evidence(assignment_pon, olt_by_id),
            "ont": _ont_evidence(ont),
            "ont_pon": _pon_evidence(ont_pon, olt_by_id),
            "related_assignment_ids": [str(value) for value in ordered_related],
            "subscription": _subscription_evidence(subscription),
        }
        input_sha256 = _digest(
            {
                "exact_evidence": exact_evidence,
                "reason_codes": ordered_reasons,
                "repair_owner": REPAIR_OWNER,
            }
        )
        findings.append(
            OntAssignmentCutoverFinding(
                assignment_id=assignment.id,
                ont_unit_id=assignment.ont_unit_id,
                ont_serial_number=ont.serial_number if ont else "Unknown ONT",
                subscription_id=assignment.subscription_id,
                subscriber_id=assignment.subscriber_id,
                assignment_pon_port_id=assignment.pon_port_id,
                ont_pon_port_id=ont.pon_port_id if ont else None,
                ont_olt_id=ont.olt_device_id if ont else None,
                reason_codes=ordered_reasons,
                related_assignment_ids=ordered_related,
                exact_evidence=exact_evidence,
                input_sha256=input_sha256,
            )
        )

    findings_tuple = tuple(findings)
    reason_counts = dict(
        sorted(Counter(code for row in findings for code in row.reason_codes).items())
    )
    gates = tuple(
        OntAssignmentConstraintGate(
            name=name,
            blocking_codes=tuple(
                code for code in _REASON_ORDER if code in blocking_codes
            ),
            blocking_assignment_ids=tuple(
                sorted(
                    {
                        finding.assignment_id
                        for finding in findings
                        if blocking_codes.intersection(finding.reason_codes)
                    },
                    key=str,
                )
            ),
        )
        for name, blocking_codes in _GATE_CODES.items()
    )
    report_payload = {
        "active_assignment_count": len(assignments),
        "findings": [finding.to_dict() for finding in findings_tuple],
        "gates": [gate.to_dict() for gate in gates],
        "reason_counts": reason_counts,
    }
    return OntAssignmentCutoverAudit(
        active_assignment_count=len(assignments),
        clean_assignment_count=len(assignments) - len(findings_tuple),
        findings=findings_tuple,
        reason_counts=reason_counts,
        gates=gates,
        report_sha256=_digest(report_payload),
    )


__all__ = [
    "REASON_LABELS",
    "REPAIR_OWNER",
    "OntAssignmentConstraintGate",
    "OntAssignmentCutoverAudit",
    "OntAssignmentCutoverFinding",
    "audit_ont_assignment_cutover",
]
