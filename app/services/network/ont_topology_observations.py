"""Canonical boundary for projecting electronic-topology observations.

Collectors supply exact external evidence. This owner may initialize empty
OLT/PON inventory edges, but it never overwrites an existing ONT or assignment
identity. Every distinct observation is retained with its latest agreement or
review-required state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_topology_observation import OntTopologyObservationEvidence

ALLOWED_SOURCES = {"huawei_fsp", "uisp"}


class OntTopologyObservationError(ValueError):
    """Raised when an electronic-topology observation is structurally invalid."""


@dataclass(frozen=True)
class OntTopologyObservationResult:
    evidence: OntTopologyObservationEvidence
    outcome: str
    reason: str | None
    pon_port: PonPort | None
    pon_created: bool
    ont_updated: bool
    assignment_conflict_ids: tuple[uuid.UUID, ...]

    @property
    def review_required(self) -> bool:
        return self.outcome == "review_required"


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OntTopologyObservationError(f"{field} is required")
    if len(normalized) > limit:
        raise OntTopologyObservationError(f"{field} must be at most {limit} characters")
    return normalized


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntTopologyObservationError(f"{field} must be a UUID") from exc


def _observed_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _topology_snapshot(ont: OntUnit) -> dict[str, object]:
    return {
        "board": ont.board,
        "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else None,
        "ont_unit_id": str(ont.id),
        "port": ont.port,
        "pon_port_id": str(ont.pon_port_id) if ont.pon_port_id else None,
    }


def _assignment_snapshot(assignment: OntAssignment) -> dict[str, object]:
    return {
        "active": assignment.active,
        "id": str(assignment.id),
        "pon_port_id": (
            str(assignment.pon_port_id) if assignment.pon_port_id else None
        ),
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else None
        ),
    }


def _resolve_observed_pon(
    db: Session,
    *,
    olt: OLTDevice,
    port_number: int | None,
    port_label: str | None,
    source: str,
) -> tuple[PonPort | None, bool, str | None]:
    conditions = []
    if port_label is not None:
        conditions.append(PonPort.name == port_label)
    if source == "uisp" and port_number is not None:
        conditions.append(PonPort.port_number == port_number)
    if not conditions:
        return None, False, "observation does not identify an exact PON"
    candidates = list(
        db.scalars(
            select(PonPort)
            .where(
                PonPort.olt_id == olt.id,
                or_(*conditions),
            )
            .order_by(PonPort.created_at.asc(), PonPort.id)
            .with_for_update()
        )
    )
    active = [candidate for candidate in candidates if candidate.is_active]
    if len(active) > 1:
        return None, False, "observed PON matches multiple active modeled ports"
    if len(active) == 1:
        pon = active[0]
        if (
            source == "uisp"
            and port_number is not None
            and pon.port_number is not None
            and pon.port_number != port_number
        ):
            return None, False, "observed PON label conflicts with modeled port number"
        if source == "uisp" and pon.port_number is None:
            pon.port_number = port_number
        return pon, False, None
    if candidates:
        return None, False, "observed PON matches only inactive modeled ports"

    if source != "uisp" or port_number is None or port_label is None:
        return None, False, "observed PON has no exact active modeled port"

    pon = PonPort(
        olt_id=olt.id,
        name=port_label,
        port_number=port_number,
        is_active=True,
        notes=f"Initialized by {source} electronic-topology observation owner.",
    )
    db.add(pon)
    db.flush()
    return pon, True, None


def _upsert_evidence(
    db: Session,
    *,
    source: str,
    evidence_key: str,
    ont: OntUnit,
    observed_olt_id: uuid.UUID,
    observed_port_number: int | None,
    observed_port_label: str | None,
    pon: PonPort | None,
    assignments: list[OntAssignment],
    assignment_conflicts: tuple[uuid.UUID, ...],
    outcome: str,
    reason: str | None,
    initial_result: dict[str, object],
    latest_snapshot: dict[str, object],
    seen_at: datetime,
) -> OntTopologyObservationEvidence:
    identity_payload = {
        "evidence_key": evidence_key,
        "observed_olt_id": str(observed_olt_id),
        "observed_port_label": observed_port_label,
        "observed_port_number": observed_port_number,
        "ont_unit_id": str(ont.id),
        "source": source,
    }
    observation_sha256 = _digest(identity_payload)
    evidence = db.scalar(
        select(OntTopologyObservationEvidence)
        .where(OntTopologyObservationEvidence.observation_sha256 == observation_sha256)
        .with_for_update()
    )
    assignment_ids = [str(row.id) for row in assignments]
    conflict_ids = [str(value) for value in assignment_conflicts]
    resolved_at = seen_at if outcome in {"initialized", "confirmed"} else None
    if evidence is None:
        evidence = OntTopologyObservationEvidence(
            source=source,
            evidence_key=evidence_key,
            observation_sha256=observation_sha256,
            ont_unit_id=ont.id,
            observed_olt_id=observed_olt_id,
            observed_pon_port_id=pon.id if pon else None,
            observed_port_number=observed_port_number,
            observed_port_label=observed_port_label,
            canonical_olt_id=ont.olt_device_id,
            canonical_pon_port_id=ont.pon_port_id,
            active_assignment_ids=assignment_ids,
            assignment_conflict_ids=conflict_ids,
            initial_outcome=outcome,
            latest_outcome=outcome,
            latest_reason=reason,
            initial_result=initial_result,
            latest_snapshot=latest_snapshot,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            seen_count=1,
            resolved_at=resolved_at,
        )
        db.add(evidence)
    else:
        evidence.observed_pon_port_id = pon.id if pon else None
        evidence.canonical_olt_id = ont.olt_device_id
        evidence.canonical_pon_port_id = ont.pon_port_id
        evidence.active_assignment_ids = assignment_ids
        evidence.assignment_conflict_ids = conflict_ids
        evidence.latest_outcome = outcome
        evidence.latest_reason = reason
        evidence.latest_snapshot = latest_snapshot
        evidence.last_seen_at = seen_at
        evidence.seen_count += 1
        evidence.resolved_at = resolved_at
    db.flush()
    return evidence


def observe_ont_electronic_topology(
    db: Session,
    *,
    source: str,
    evidence_key: str,
    ont_unit_id: str | uuid.UUID,
    observed_olt_id: str | uuid.UUID,
    observed_port_number: int | None,
    observed_port_label: str | None = None,
    observed_at: datetime | None = None,
) -> OntTopologyObservationResult:
    """Record an exact observation and initialize only empty topology fields."""

    normalized_source = _required_text(source, "source", limit=40).lower()
    if normalized_source not in ALLOWED_SOURCES:
        raise OntTopologyObservationError("unsupported topology observation source")
    normalized_key = _required_text(evidence_key, "evidence_key", limit=200)
    ont_id = _coerce_uuid(ont_unit_id, "ont_unit_id")
    olt_id = _coerce_uuid(observed_olt_id, "observed_olt_id")
    seen_at = _observed_at(observed_at)
    if observed_port_number is not None and not 0 <= observed_port_number <= 65535:
        raise OntTopologyObservationError(
            "observed_port_number must be between 0 and 65535"
        )
    normalized_label = str(observed_port_label or "").strip() or None
    if normalized_label and len(normalized_label) > 120:
        raise OntTopologyObservationError(
            "observed_port_label must be at most 120 characters"
        )
    if observed_port_number is not None and normalized_label is None:
        if normalized_source == "uisp":
            normalized_label = f"pon{observed_port_number}"

    ont = db.scalar(select(OntUnit).where(OntUnit.id == ont_id).with_for_update())
    if ont is None:
        raise OntTopologyObservationError("observed ONT not found")
    olt = db.scalar(select(OLTDevice).where(OLTDevice.id == olt_id).with_for_update())
    if olt is None:
        raise OntTopologyObservationError("observed OLT not found")
    assignments = list(
        db.scalars(
            select(OntAssignment)
            .where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
            .order_by(OntAssignment.id)
            .with_for_update()
        )
    )
    before = _topology_snapshot(ont)
    pon: PonPort | None = None
    pon_created = False
    pon_reason: str | None = None
    if observed_port_number is not None or normalized_label is not None:
        pon, pon_created, pon_reason = _resolve_observed_pon(
            db,
            olt=olt,
            port_number=observed_port_number,
            port_label=normalized_label,
            source=normalized_source,
        )

    ont_updated = False
    reason: str | None = None
    outcome = "confirmed"
    topology_conflict = ont.olt_device_id is not None and ont.olt_device_id != olt.id
    if pon is not None:
        topology_conflict = topology_conflict or (
            ont.pon_port_id is not None and ont.pon_port_id != pon.id
        )
    observed_board: str | None = None
    observed_port: str | None = None
    if normalized_source == "huawei_fsp" and normalized_label is not None:
        fsp_parts = [part.strip() for part in normalized_label.split("/")]
        if len(fsp_parts) == 3 and all(fsp_parts):
            observed_board = f"{fsp_parts[0]}/{fsp_parts[1]}"
            observed_port = fsp_parts[2]
            topology_conflict = topology_conflict or (
                ont.board is not None and ont.board != observed_board
            )
            topology_conflict = topology_conflict or (
                ont.port is not None and ont.port != observed_port
            )
    assignment_conflicts = tuple(
        row.id for row in assignments if pon is not None and row.pon_port_id != pon.id
    )

    if pon_reason is not None:
        outcome = "review_required"
        reason = pon_reason
    elif topology_conflict:
        outcome = "review_required"
        reason = "observed OLT/PON conflicts with canonical ONT topology"
    else:
        if ont.olt_device_id is None:
            ont.olt_device_id = olt.id
            ont_updated = True
        if pon is None:
            outcome = "incomplete"
            reason = "observation does not include an exact PON port"
        else:
            if ont.pon_port_id is None:
                ont.pon_port_id = pon.id
                ont_updated = True
            if observed_board is not None and ont.board is None:
                ont.board = observed_board
                ont_updated = True
            if observed_port is not None and ont.port is None:
                ont.port = observed_port
                ont_updated = True
            if assignment_conflicts:
                outcome = "review_required"
                reason = "active assignment PON projection disagrees with observation"
            elif ont_updated or pon_created:
                outcome = "initialized"

    after = _topology_snapshot(ont)
    latest_snapshot: dict[str, object] = {
        "active_assignments": [_assignment_snapshot(row) for row in assignments],
        "after": after,
        "before": before,
        "observed": {
            "olt_id": str(olt.id),
            "pon_port_id": str(pon.id) if pon else None,
            "port_label": normalized_label,
            "port_number": observed_port_number,
        },
    }
    result_payload: dict[str, object] = {
        "assignment_conflict_ids": [str(value) for value in assignment_conflicts],
        "ont_updated": ont_updated,
        "outcome": outcome,
        "pon_created": pon_created,
        "reason": reason,
        "snapshot": latest_snapshot,
    }
    evidence = _upsert_evidence(
        db,
        source=normalized_source,
        evidence_key=normalized_key,
        ont=ont,
        observed_olt_id=olt.id,
        observed_port_number=observed_port_number,
        observed_port_label=normalized_label,
        pon=pon,
        assignments=assignments,
        assignment_conflicts=assignment_conflicts,
        outcome=outcome,
        reason=reason,
        initial_result=result_payload,
        latest_snapshot=latest_snapshot,
        seen_at=seen_at,
    )
    return OntTopologyObservationResult(
        evidence=evidence,
        outcome=outcome,
        reason=reason,
        pon_port=pon,
        pon_created=pon_created,
        ont_updated=ont_updated,
        assignment_conflict_ids=assignment_conflicts,
    )
