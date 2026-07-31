"""Field-side fiber capture over sub's network-plant truth."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import AccessType, CatalogOffer, Subscription
from app.models.fiber_change_request import FiberChangeRequest
from app.models.field_attachment import FieldAttachment
from app.models.field_fiber import FIELD_FIBER_TEST_TYPES, FieldFiberTestResult
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSplice,
    FiberSpliceClosure,
    FiberStrand,
    OLTDevice,
    OntAssignment,
    OntSignalObservation,
    OntUnit,
)
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid
from app.services.fiber_topology import (
    FiberSubscriptionTrace,
    trace_fiber_subscription,
)
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.network import (
    fiber_splice_proposals,
    fiber_topology_field_observations,
    fiber_topology_work_order_evidence_map,
)
from app.services.network.fiber_splice_proposals import (
    FieldTechnicianActor,
    SpliceProposalReceipt,
    SpliceProposalStatus,
)

_TESTABLE_ASSET_MODELS = {
    "fiber_strand": FiberStrand,
    "fiber_splice": FiberSplice,
    "splice_closure": FiberSpliceClosure,
    "fiber_access_point": FiberAccessPoint,
    "fdh": FdhCabinet,
    "fdh_cabinet": FdhCabinet,
    "olt": OLTDevice,
    "olt_device": OLTDevice,
}


def propose_splice(
    db: Session,
    principal: dict[str, Any],
    *,
    closure_id: str,
    from_strand_id: str,
    from_strand_end: str,
    to_strand_id: str,
    to_strand_end: str,
    tray_id: str | None = None,
    position: int | None = None,
    splice_type: str | None = None,
    loss_db: float | None = None,
    note: str | None = None,
    work_order_id: str | None = None,
) -> SpliceProposalReceipt:
    profile = _profile_from_principal(db, principal)
    work_order = (
        _scoped_work_order(db, profile, work_order_id) if work_order_id else None
    )
    actor = FieldTechnicianActor(
        technician_id=profile.id,
        person_id=profile.person_id,
        system_user_id=profile.system_user_id,
    )
    return fiber_splice_proposals.propose_splice(
        db,
        actor=actor,
        closure_id=closure_id,
        from_strand_id=from_strand_id,
        from_strand_end=from_strand_end,
        to_strand_id=to_strand_id,
        to_strand_end=to_strand_end,
        tray_id=tray_id,
        position=position,
        splice_type=splice_type,
        loss_db=loss_db,
        note=note,
        work_order=work_order,
    )


def record_test(
    db: Session,
    principal: dict[str, Any],
    *,
    crm_work_order_id: str,
    asset_type: str,
    asset_id: str,
    test_type: str,
    wavelength_nm: int | None = None,
    value_db: float | None = None,
    unit: str | None = None,
    passed: bool | None = None,
    instrument: str | None = None,
    measured_at: datetime | None = None,
    notes: str | None = None,
    attachment_id: str | None = None,
    client_ref: str | None = None,
) -> FieldFiberTestResult:
    profile = _profile_from_principal(db, principal)
    row = _scoped_work_order(db, profile, crm_work_order_id)
    normalized_type = _normalize_asset_type(asset_type)
    if test_type not in FIELD_FIBER_TEST_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown test_type '{test_type}'")
    model = _TESTABLE_ASSET_MODELS.get(normalized_type)
    if model is None:
        raise HTTPException(
            status_code=400, detail=f"Unsupported asset type: {asset_type}"
        )
    asset_uuid = _uuid_or_422(asset_id, "asset_id")
    if db.get(model, asset_uuid) is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    client_uuid = _uuid_or_422(client_ref, "client_ref") if client_ref else None
    if client_uuid is not None:
        existing = (
            db.query(FieldFiberTestResult)
            .filter(FieldFiberTestResult.client_ref == client_uuid)
            .one_or_none()
        )
        if existing is not None:
            return existing

    attachment_uuid = (
        _uuid_or_422(attachment_id, "attachment_id") if attachment_id else None
    )
    if attachment_uuid is not None:
        attachment = db.get(FieldAttachment, attachment_uuid)
        if (
            attachment is None
            or not attachment.is_active
            or attachment.work_order_mirror_id != row.id
        ):
            raise HTTPException(status_code=404, detail="Attachment not found")

    result = FieldFiberTestResult(
        work_order_mirror_id=row.id,
        asset_type=normalized_type,
        asset_id=asset_uuid,
        test_type=test_type,
        wavelength_nm=wavelength_nm,
        value_db=value_db,
        unit=unit,
        passed=passed,
        instrument=instrument,
        attachment_id=attachment_uuid,
        measured_by_technician_id=profile.id,
        measured_by_person_id=profile.person_id,
        measured_by_system_user_id=profile.system_user_id,
        measured_at=measured_at,
        notes=notes,
        client_ref=client_uuid,
    )
    db.add(result)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if client_uuid is not None:
            existing = (
                db.query(FieldFiberTestResult)
                .filter(FieldFiberTestResult.client_ref == client_uuid)
                .one_or_none()
            )
            if existing is not None:
                return existing
        raise
    db.refresh(result)
    return result


def list_tests(
    db: Session,
    principal: dict[str, Any],
    *,
    crm_work_order_id: str,
) -> list[FieldFiberTestResult]:
    profile = _profile_from_principal(db, principal)
    row = _scoped_work_order(db, profile, crm_work_order_id)
    return (
        db.query(FieldFiberTestResult)
        .filter(FieldFiberTestResult.work_order_mirror_id == row.id)
        .order_by(FieldFiberTestResult.created_at.desc())
        .all()
    )


def record_source_observation(
    db: Session,
    principal: dict[str, Any],
    *,
    work_order_public_id: str,
    staged_feature_id: str,
    expected_feature_content_sha256: str,
    verification_scope: str,
    outcome: str,
    observed_at: datetime,
    client_ref: str,
    observed_external_label: str | None = None,
    observed_asset_type: str | None = None,
    observed_asset_id: str | None = None,
    start_endpoint_type: str | None = None,
    start_endpoint_ref_id: str | None = None,
    end_endpoint_type: str | None = None,
    end_endpoint_ref_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    instrument: str | None = None,
    measurement_payload: dict[str, Any] | None = None,
    attachment_ids: list[str] | None = None,
    notes: str | None = None,
):
    """Thin technician/work-order adapter around the observation owner."""

    profile = _profile_from_principal(db, principal)
    work_order = _scoped_work_order(db, profile, work_order_public_id)
    try:
        return fiber_topology_field_observations.record_fiber_field_observation(
            db,
            staged_feature_id=staged_feature_id,
            expected_feature_content_sha256=expected_feature_content_sha256,
            work_order_id=work_order.id,
            recorded_by_technician_id=profile.id,
            recorded_by_person_id=profile.person_id,
            recorded_by_system_user_id=profile.system_user_id,
            verification_scope=verification_scope,
            outcome=outcome,
            observed_at=observed_at,
            client_ref=client_ref,
            observed_external_label=observed_external_label,
            observed_asset_type=observed_asset_type,
            observed_asset_id=observed_asset_id,
            start_endpoint_type=start_endpoint_type,
            start_endpoint_ref_id=start_endpoint_ref_id,
            end_endpoint_type=end_endpoint_type,
            end_endpoint_ref_id=end_endpoint_ref_id,
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
            instrument=instrument,
            measurement_payload=measurement_payload,
            attachment_ids=attachment_ids,
            notes=notes,
        )
    except fiber_topology_field_observations.FiberTopologyFieldObservationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def list_source_observations(
    db: Session,
    principal: dict[str, Any],
    *,
    work_order_public_id: str,
    staged_feature_id: str | None = None,
):
    """List immutable staged observations within the technician's job scope."""

    profile = _profile_from_principal(db, principal)
    work_order = _scoped_work_order(db, profile, work_order_public_id)
    try:
        return fiber_topology_field_observations.list_fiber_field_observations(
            db,
            work_order_id=work_order.id,
            staged_feature_id=staged_feature_id,
        )
    except fiber_topology_field_observations.FiberTopologyFieldObservationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def get_work_order_evidence_map(
    db: Session,
    principal: dict[str, Any],
    *,
    work_order_public_id: str,
):
    """Project exact fiber evidence inside the technician's native job scope."""

    fiber_topology_work_order_evidence_map.ensure_work_order_evidence_map_repeatable_snapshot(
        db
    )
    profile = _profile_from_principal(db, principal)
    work_order = _scoped_work_order(db, profile, work_order_public_id)
    try:
        return fiber_topology_work_order_evidence_map.project_fiber_work_order_evidence_map(
            db,
            work_order_id=work_order.id,
            expected_work_order_public_id=work_order.public_id,
        )
    except (
        fiber_topology_work_order_evidence_map.FiberTopologyWorkOrderEvidenceMapError
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


_MAX_TRACE_SUBSCRIPTIONS = 10
_MAX_PROPOSAL_ROWS = 200


@dataclass(frozen=True)
class FieldOntLiveStatus:
    """Latest observed OLT-side signal evidence for one exact ONT."""

    ont_unit_id: uuid.UUID
    serial_number: str
    olt_status: str | None
    olt_status_seen_at: datetime | None
    rx_signal_dbm: float | None
    rx_observed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ont_unit_id": self.ont_unit_id,
            "serial_number": self.serial_number,
            "olt_status": self.olt_status,
            "olt_status_seen_at": self.olt_status_seen_at,
            "rx_signal_dbm": self.rx_signal_dbm,
            "rx_observed_at": self.rx_observed_at,
        }


@dataclass(frozen=True)
class FieldCustomerFiberTrace:
    """One customer fiber trace projected into the technician's job scope."""

    trace: FiberSubscriptionTrace
    ont_live: FieldOntLiveStatus | None

    def to_dict(self) -> dict[str, Any]:
        payload = self.trace.to_dict()
        payload["ont_live"] = self.ont_live.to_dict() if self.ont_live else None
        return payload


def list_customer_traces(
    db: Session,
    principal: dict[str, Any],
    *,
    crm_work_order_id: str,
) -> list[FieldCustomerFiberTrace]:
    """Project the job customer's fiber traces inside the technician's scope.

    Read-only adapter over the canonical trace owner
    (``app.services.fiber_topology.trace_fiber_subscription``); it never
    infers, repairs, or widens the owner's evidence.
    """

    profile = _profile_from_principal(db, principal)
    work_order = _scoped_work_order(db, profile, crm_work_order_id)
    subscriptions = (
        db.query(Subscription)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .filter(
            Subscription.subscriber_id == work_order.subscriber_id,
            CatalogOffer.access_type == AccessType.fiber,
        )
        .order_by(Subscription.created_at.desc())
        .limit(_MAX_TRACE_SUBSCRIPTIONS)
        .all()
    )
    return [
        FieldCustomerFiberTrace(
            trace=trace_fiber_subscription(db, subscription.id),
            ont_live=_latest_ont_live(db, subscription.id),
        )
        for subscription in subscriptions
    ]


def _latest_ont_live(
    db: Session, subscription_id: uuid.UUID
) -> FieldOntLiveStatus | None:
    """Latest observed ONT signal for exactly one active assignment.

    Ambiguous or missing assignments return no live reading; the trace's own
    gap evidence names the conflict.
    """

    assignments = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.subscription_id == subscription_id,
            OntAssignment.active.is_(True),
        )
        .all()
    )
    if len(assignments) != 1:
        return None
    ont = db.get(OntUnit, assignments[0].ont_unit_id)
    if ont is None or not ont.is_active:
        return None
    observation = (
        db.query(OntSignalObservation)
        .filter(OntSignalObservation.ont_unit_id == ont.id)
        .order_by(OntSignalObservation.observed_at.desc())
        .first()
    )
    return FieldOntLiveStatus(
        ont_unit_id=ont.id,
        serial_number=ont.serial_number,
        olt_status=ont.olt_status.value if ont.olt_status else None,
        olt_status_seen_at=ont.olt_status_seen_at,
        rx_signal_dbm=observation.rx_signal_dbm if observation else None,
        rx_observed_at=observation.observed_at if observation else None,
    )


def list_splice_proposals(
    db: Session,
    principal: dict[str, Any],
    *,
    limit: int = 50,
) -> list[SpliceProposalStatus]:
    """List the technician's own splice change requests, newest first."""

    profile = _profile_from_principal(db, principal)
    rows = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(
            FiberChangeRequest.payload["field_actor"]["technician_id"].as_string()
            == str(profile.id)
        )
        .order_by(FiberChangeRequest.created_at.desc())
        .limit(max(1, min(limit, _MAX_PROPOSAL_ROWS)))
        .all()
    )
    return [fiber_splice_proposals.proposal_status(row) for row in rows]


def _scoped_work_order(db: Session, profile, crm_work_order_id: str) -> WorkOrder:
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrder.public_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _normalize_asset_type(asset_type: str) -> str:
    value = (asset_type or "").strip().lower()
    if value == "fiber_splice_closure":
        return "splice_closure"
    if value == "fdh_cabinet":
        return "fdh"
    if value == "olt_device":
        return "olt"
    return value


def _uuid_or_422(value, field_name: str):
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}") from exc
