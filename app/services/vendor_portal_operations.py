"""Vendor projects, quotes and as-built workflow native to Sub."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    VendorAssignmentType,
)
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorQuoteLineUpdate,
    VendorRouteRevisionCreate,
)
from app.services.common import coerce_uuid

_EDITABLE_QUOTES = {
    ProjectQuoteStatus.draft.value,
    ProjectQuoteStatus.revision_requested.value,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _geom(geojson: dict):
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geojson)), 4326)


def _project(db: Session, project_id: str) -> InstallationProject:
    row = db.get(InstallationProject, coerce_uuid(project_id))
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Installation project not found")
    return row


def _quote(db: Session, quote_id: str, vendor_id: str | None = None) -> ProjectQuote:
    row = (
        db.query(ProjectQuote)
        .options(selectinload(ProjectQuote.line_items), joinedload(ProjectQuote.project))
        .filter(ProjectQuote.id == coerce_uuid(quote_id))
        .filter(ProjectQuote.is_active.is_(True))
        .one_or_none()
    )
    if row is None or (vendor_id and str(row.vendor_id) != str(vendor_id)):
        raise HTTPException(status_code=404, detail="Project quote not found")
    return row


def _recalculate(quote: ProjectQuote) -> None:
    subtotal = sum(
        (_money(item.amount) for item in quote.line_items if item.is_active),
        Decimal("0"),
    )
    quote.subtotal = _money(subtotal)
    quote.tax_total = _money(subtotal * Decimal(str(quote.vat_rate_percent or 0)) / 100)
    quote.total = _money(quote.subtotal + quote.tax_total)


def _serialize_project(row: InstallationProject) -> dict:
    project = row.project
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_code": getattr(project, "code", None),
        "project_name": getattr(project, "name", None),
        "subscriber_id": row.subscriber_id,
        "assigned_vendor_id": row.assigned_vendor_id,
        "assignment_type": row.assignment_type,
        "status": row.status,
        "bidding_open_at": row.bidding_open_at,
        "bidding_close_at": row.bidding_close_at,
        "approved_quote_id": row.approved_quote_id,
        "erp_purchase_order_id": row.erp_purchase_order_id,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_quote(row: ProjectQuote) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "vendor_id": row.vendor_id,
        "status": row.status,
        "currency": row.currency,
        "subtotal": row.subtotal,
        "vat_rate_percent": row.vat_rate_percent,
        "tax_total": row.tax_total,
        "total": row.total,
        "valid_from": row.valid_from,
        "valid_until": row.valid_until,
        "submitted_at": row.submitted_at,
        "reviewed_at": row.reviewed_at,
        "review_notes": row.review_notes,
        "line_items": [item for item in row.line_items if item.is_active],
    }


class VendorPortalOperations:
    @staticmethod
    def list_reviewable_quotes(db: Session, *, limit: int = 200) -> list[ProjectQuote]:
        return (
            db.query(ProjectQuote)
            .options(selectinload(ProjectQuote.line_items))
            .filter(
                ProjectQuote.status.in_(
                    (
                        ProjectQuoteStatus.submitted.value,
                        ProjectQuoteStatus.under_review.value,
                    )
                )
            )
            .filter(ProjectQuote.is_active.is_(True))
            .order_by(ProjectQuote.submitted_at.asc())
            .limit(max(1, min(limit, 500)))
            .all()
        )

    @staticmethod
    def latest_quote_for_project(
        db: Session, project_id: str, vendor_id: str
    ) -> dict | None:
        row = (
            db.query(ProjectQuote)
            .filter(ProjectQuote.project_id == coerce_uuid(project_id))
            .filter(ProjectQuote.vendor_id == coerce_uuid(vendor_id))
            .filter(ProjectQuote.is_active.is_(True))
            .order_by(ProjectQuote.created_at.desc())
            .first()
        )
        return _serialize_quote(_quote(db, str(row.id), vendor_id)) if row else None

    @staticmethod
    def list_projects(
        db: Session, vendor_id: str, *, available: bool, limit: int, offset: int
    ) -> list[dict]:
        query = db.query(InstallationProject).options(joinedload(InstallationProject.project))
        if available:
            now = _now()
            query = query.filter(
                InstallationProject.status
                == InstallationProjectStatus.open_for_bidding.value,
                InstallationProject.bidding_open_at <= now,
                InstallationProject.bidding_close_at >= now,
            )
        else:
            query = query.outerjoin(
                ProjectQuote, ProjectQuote.project_id == InstallationProject.id
            ).filter(
                or_(
                    InstallationProject.assigned_vendor_id == coerce_uuid(vendor_id),
                    ProjectQuote.vendor_id == coerce_uuid(vendor_id),
                )
            )
        rows = (
            query.filter(InstallationProject.is_active.is_(True))
            .order_by(InstallationProject.updated_at.desc())
            .distinct()
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [_serialize_project(row) for row in rows]

    @staticmethod
    def create_quote(
        db: Session, payload: VendorQuoteCreate, *, vendor_id: str, user_id: str
    ) -> dict:
        project = _project(db, str(payload.project_id))
        if (
            project.assignment_type == VendorAssignmentType.direct.value
            and str(project.assigned_vendor_id) != str(vendor_id)
        ):
            raise HTTPException(status_code=403, detail="Project is assigned to another vendor")
        if project.bidding_close_at and project.bidding_close_at <= _now():
            raise HTTPException(status_code=409, detail="Bidding window has closed")
        existing = (
            db.query(ProjectQuote)
            .filter(ProjectQuote.project_id == project.id)
            .filter(ProjectQuote.vendor_id == coerce_uuid(vendor_id))
            .filter(ProjectQuote.status.in_(tuple(_EDITABLE_QUOTES)))
            .order_by(ProjectQuote.created_at.desc())
            .first()
        )
        if existing:
            return _serialize_quote(_quote(db, str(existing.id), vendor_id))
        quote = ProjectQuote(
            project_id=project.id,
            vendor_id=coerce_uuid(vendor_id),
            currency=payload.currency.upper(),
            vat_rate_percent=payload.vat_rate_percent,
            valid_from=_now(),
            valid_until=_now() + timedelta(days=30),
            created_by_person_id=coerce_uuid(user_id),
        )
        db.add(quote)
        db.commit()
        return _serialize_quote(_quote(db, str(quote.id), vendor_id))

    @staticmethod
    def get_quote(db: Session, quote_id: str, vendor_id: str) -> dict:
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def add_quote_line(
        db: Session, quote_id: str, payload: VendorQuoteLineCreate, vendor_id: str
    ) -> dict:
        quote = _quote(db, quote_id, vendor_id)
        if quote.status not in _EDITABLE_QUOTES:
            raise HTTPException(status_code=409, detail="Quote is not editable")
        line = ProjectQuoteLineItem(
            quote_id=quote.id,
            **payload.model_dump(),
            amount=_money(payload.quantity * payload.unit_price),
            is_active=True,
        )
        quote.line_items.append(line)
        _recalculate(quote)
        db.commit()
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def update_quote_line(
        db: Session,
        quote_id: str,
        line_id: str,
        payload: VendorQuoteLineUpdate,
        vendor_id: str,
    ) -> dict:
        quote = _quote(db, quote_id, vendor_id)
        if quote.status not in _EDITABLE_QUOTES:
            raise HTTPException(status_code=409, detail="Quote is not editable")
        line = next(
            (item for item in quote.line_items if str(item.id) == line_id and item.is_active),
            None,
        )
        if line is None:
            raise HTTPException(status_code=404, detail="Quote line not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(line, key, value)
        line.amount = _money(line.quantity * line.unit_price)
        _recalculate(quote)
        db.commit()
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def delete_quote_line(
        db: Session, quote_id: str, line_id: str, vendor_id: str
    ) -> dict:
        quote = _quote(db, quote_id, vendor_id)
        if quote.status not in _EDITABLE_QUOTES:
            raise HTTPException(status_code=409, detail="Quote is not editable")
        line = next(
            (item for item in quote.line_items if str(item.id) == line_id and item.is_active),
            None,
        )
        if line is None:
            raise HTTPException(status_code=404, detail="Quote line not found")
        line.is_active = False
        _recalculate(quote)
        db.commit()
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def submit_quote(db: Session, quote_id: str, vendor_id: str) -> dict:
        quote = _quote(db, quote_id, vendor_id)
        if quote.status not in _EDITABLE_QUOTES:
            raise HTTPException(status_code=409, detail="Quote is not submittable")
        active = [item for item in quote.line_items if item.is_active]
        if not active:
            raise HTTPException(status_code=422, detail="Quote requires at least one line")
        _recalculate(quote)
        quote.status = ProjectQuoteStatus.submitted.value
        quote.submitted_at = _now()
        if quote.project.status == InstallationProjectStatus.open_for_bidding.value:
            quote.project.status = InstallationProjectStatus.quoted.value
        db.commit()
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def review_quote(
        db: Session,
        quote_id: str,
        *,
        reviewer_id: str,
        approve: bool,
        notes: str | None,
    ) -> dict:
        quote = _quote(db, quote_id)
        if quote.status not in {
            ProjectQuoteStatus.submitted.value,
            ProjectQuoteStatus.under_review.value,
        }:
            raise HTTPException(status_code=409, detail="Quote is not reviewable")
        quote.reviewed_at = _now()
        quote.reviewed_by_person_id = coerce_uuid(reviewer_id)
        quote.review_notes = (notes or "").strip() or None
        if approve:
            quote.status = ProjectQuoteStatus.approved.value
            quote.project.approved_quote_id = quote.id
            quote.project.assigned_vendor_id = quote.vendor_id
            quote.project.status = InstallationProjectStatus.approved.value
            db.flush()
            from app.models.field_erp_sync import (
                FieldErpSyncFlow,
                flow_owned_by_sub,
            )

            if flow_owned_by_sub(db, FieldErpSyncFlow.purchase_order):
                from app.services.dotmac_erp.purchase_order_sync import (
                    enqueue_purchase_order,
                )

                enqueue_purchase_order(db, quote.project)
        else:
            quote.status = ProjectQuoteStatus.revision_requested.value
        db.commit()
        return _serialize_quote(_quote(db, quote_id))

    @staticmethod
    def create_route_revision(
        db: Session,
        quote_id: str,
        payload: VendorRouteRevisionCreate,
        vendor_id: str,
    ) -> dict:
        quote = _quote(db, quote_id, vendor_id)
        next_number = (
            db.query(func.coalesce(func.max(ProposedRouteRevision.revision_number), 0))
            .filter(ProposedRouteRevision.quote_id == quote.id)
            .scalar()
            + 1
        )
        revision = ProposedRouteRevision(
            quote_id=quote.id,
            revision_number=next_number,
            route_geom=_geom(payload.geojson),
            length_meters=payload.length_meters,
        )
        db.add(revision)
        db.commit()
        return {
            "id": revision.id,
            "quote_id": revision.quote_id,
            "revision_number": revision.revision_number,
            "status": revision.status,
            "length_meters": revision.length_meters,
        }

    @staticmethod
    def submit_route_revision(
        db: Session, revision_id: str, vendor_id: str, user_id: str
    ) -> dict:
        revision = db.get(ProposedRouteRevision, coerce_uuid(revision_id))
        if revision is None or str(revision.quote.vendor_id) != str(vendor_id):
            raise HTTPException(status_code=404, detail="Route revision not found")
        if revision.status != ProposedRouteRevisionStatus.draft.value:
            raise HTTPException(status_code=409, detail="Route revision is not draft")
        revision.status = ProposedRouteRevisionStatus.submitted.value
        revision.submitted_at = _now()
        revision.submitted_by_person_id = coerce_uuid(user_id)
        db.commit()
        return {"id": revision.id, "status": revision.status}

    @staticmethod
    def submit_as_built(
        db: Session, payload: VendorAsBuiltCreate, vendor_id: str, user_id: str
    ) -> dict:
        project = _project(db, str(payload.project_id))
        if str(project.assigned_vendor_id) != str(vendor_id):
            raise HTTPException(status_code=403, detail="Project is assigned to another vendor")
        if not payload.geojson and not payload.line_items:
            raise HTTPException(status_code=422, detail="Provide a route or line items")
        row = AsBuiltRoute(
            project_id=project.id,
            proposed_revision_id=payload.proposed_revision_id,
            route_geom=_geom(payload.geojson) if payload.geojson else None,
            actual_length_meters=payload.actual_length_meters,
            submitted_at=_now(),
            submitted_by_person_id=coerce_uuid(user_id),
            variation_type=payload.variation_type,
            variation_reason=payload.variation_reason,
            work_order_ref=payload.work_order_ref,
        )
        for item in payload.line_items:
            row.line_items.append(
                AsBuiltLineItem(
                    **item.model_dump(),
                    amount=_money(item.quantity * item.unit_price),
                )
            )
        db.add(row)
        db.commit()
        return {
            "id": row.id,
            "project_id": row.project_id,
            "status": row.status,
            "actual_length_meters": row.actual_length_meters,
            "submitted_at": row.submitted_at,
            "line_items": row.line_items,
        }


vendor_portal_operations = VendorPortalOperations()
