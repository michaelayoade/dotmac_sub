"""Vendor projects, quotes and as-built workflow native to Sub."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeVar

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.domain_settings import SettingDomain
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
)
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorQuoteLineUpdate,
    VendorRouteRevisionCreate,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value
from app.services.ui_contracts import Action

_EDITABLE_QUOTES = {
    ProjectQuoteStatus.draft.value,
    ProjectQuoteStatus.revision_requested.value,
}
ResultT = TypeVar("ResultT")


class VendorProjectWorkspaceError(DomainError):
    """Stable failures from vendor workspace policy and record boundaries."""


def _error(suffix: str, message: str) -> VendorProjectWorkspaceError:
    return VendorProjectWorkspaceError(
        code=f"operations.vendor_project_workspace.{suffix}",
        message=message,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="operations.vendor_project_workspace",
        concern="vendor project workspace mutation coordination",
        name=name,
    )


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class CreateVendorQuoteCommand:
    context: CommandContext
    payload: VendorQuoteCreate
    vendor_id: str
    user_id: str
    resolved_currency: str | None = None
    quote_validity_days: int | None = None


@dataclass(frozen=True, slots=True)
class AddVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    payload: VendorQuoteLineCreate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class UpdateVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    line_id: str
    payload: VendorQuoteLineUpdate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class DeleteVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    line_id: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class ReviewVendorQuoteCommand:
    context: CommandContext
    quote_id: str
    reviewer_id: str
    approve: bool
    notes: str | None


@dataclass(frozen=True, slots=True)
class CreateVendorRouteRevisionCommand:
    context: CommandContext
    quote_id: str
    payload: VendorRouteRevisionCreate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class SubmitVendorRouteRevisionCommand:
    context: CommandContext
    revision_id: str
    vendor_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class StageVendorQuoteSubmission:
    context: CommandContext
    quote_id: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class StageVendorAsBuiltSubmission:
    context: CommandContext
    payload: VendorAsBuiltCreate
    vendor_id: str
    user_id: str


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _project(
    db: Session, project_id: str, *, for_update: bool = False
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error("project_not_found", "Installation project not found.")
    return row


def _quote(
    db: Session,
    quote_id: str,
    vendor_id: str | None = None,
    *,
    for_update: bool = False,
) -> ProjectQuote:
    query = (
        db.query(ProjectQuote)
        .options(
            selectinload(ProjectQuote.line_items), joinedload(ProjectQuote.project)
        )
        .filter(ProjectQuote.id == coerce_uuid(quote_id))
        .filter(ProjectQuote.is_active.is_(True))
    )
    if for_update:
        query = query.with_for_update(of=ProjectQuote)
    row = query.one_or_none()
    if row is None or (vendor_id and str(row.vendor_id) != str(vendor_id)):
        raise _error("quote_not_found", "Project quote not found.")
    return row


def _serialize_project(
    row: InstallationProject, viewer_vendor_id: str | None = None
) -> dict:
    project = row.project
    is_mine = (
        viewer_vendor_id is not None
        and row.assigned_vendor_id is not None
        and str(row.assigned_vendor_id) == str(viewer_vendor_id)
    )
    lifecycle_action = None
    if is_mine and row.status == InstallationProjectStatus.approved.value:
        lifecycle_action = Action(
            key="start",
            label="Start work",
            allowed=True,
            preview_url=f"/vendor/projects/{row.id}/start",
            affected=1,
            requires_confirmation=True,
        )
    elif is_mine and row.status == InstallationProjectStatus.in_progress.value:
        lifecycle_action = Action(
            key="complete",
            label="Mark complete",
            allowed=True,
            preview_url=f"/vendor/projects/{row.id}/complete",
            affected=1,
            requires_confirmation=True,
        )
    as_built_action = Action(
        key="submit_as_built",
        label="Review and submit as-built",
        allowed=is_mine,
        reason=None if is_mine else "As-built submission is available after award",
    )
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
        "lifecycle_action": lifecycle_action,
        "as_built_action": as_built_action,
    }


def _serialize_quote(row: ProjectQuote) -> dict:
    editable = row.status in _EDITABLE_QUOTES
    return {
        "id": row.id,
        "project_id": row.project_id,
        "vendor_id": row.vendor_id,
        "status": row.status,
        # Editability is projected from the same set the mutation paths enforce;
        # the template consumes allowed/reason and never re-derives status rules.
        "edit_action": Action(
            key="edit",
            label="Edit quote",
            allowed=editable,
            reason=None
            if editable
            else f"A {row.status.replace('_', ' ')} quote cannot be edited",
        ),
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
        query = db.query(InstallationProject).options(
            joinedload(InstallationProject.project)
        )
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
        return [_serialize_project(row, viewer_vendor_id=vendor_id) for row in rows]

    @staticmethod
    def create_quote(
        db: Session,
        command: CreateVendorQuoteCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            currency = command.payload.currency or str(
                resolve_value(db, SettingDomain.billing, "default_currency")
            )
            validity_days = int(
                resolve_value(
                    db,
                    SettingDomain.projects,
                    "vendor_quote_validity_days",
                )
            )
            return vendor_project_records.stage_create_quote(
                db,
                replace(
                    command,
                    resolved_currency=currency.upper(),
                    quote_validity_days=validity_days,
                ),
            )

        return _execute(
            db,
            context=command.context,
            name="create_vendor_quote",
            operation=operation,
        )

    @staticmethod
    def get_quote(db: Session, quote_id: str, vendor_id: str) -> dict:
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def preview_quote_submission(
        db: Session,
        quote_id: str,
        vendor_id: str,
        *,
        for_update: bool = False,
    ) -> dict:
        """Own the read-only impact snapshot for a quote submission."""
        quote = _quote(db, quote_id, vendor_id, for_update=for_update)
        project = (
            _project(db, str(quote.project_id), for_update=True)
            if for_update
            else quote.project
        )
        if quote.status not in _EDITABLE_QUOTES:
            raise _error("quote_not_submittable", "Quote is not submittable.")
        active = [item for item in quote.line_items if item.is_active]
        if not active:
            raise _error("quote_line_required", "Quote requires at least one line.")
        subtotal = sum((_money(item.amount) for item in active), Decimal("0.00"))
        tax_total = _money(
            subtotal * Decimal(str(quote.vat_rate_percent or 0)) / Decimal("100")
        )
        total = _money(subtotal + tax_total)
        return {
            "submission_type": "quote",
            "project_id": str(quote.project_id),
            "target_id": str(quote.id),
            "title": "Submit quote for review",
            "summary": (
                f"{len(active)} line item{'s' if len(active) != 1 else ''}; "
                f"{quote.currency} {total:,.2f} total"
            ),
            "details": [
                ("Line items", str(len(active))),
                ("Subtotal", f"{quote.currency} {subtotal:,.2f}"),
                ("Tax", f"{quote.currency} {tax_total:,.2f}"),
                ("Total", f"{quote.currency} {total:,.2f}"),
                ("Result", "Quote becomes read-only and enters staff review"),
            ],
            "state": {
                "quote_id": str(quote.id),
                "project_id": str(quote.project_id),
                "project_status": project.status,
                "project_updated_at": project.updated_at,
                "status": quote.status,
                "currency": quote.currency,
                "vat_rate_percent": quote.vat_rate_percent,
                "updated_at": quote.updated_at,
                "lines": [
                    {
                        "id": str(item.id),
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "amount": item.amount,
                        "updated_at": item.updated_at,
                    }
                    for item in sorted(active, key=lambda row: str(row.id))
                ],
            },
        }

    @staticmethod
    def preview_as_built_submission(
        db: Session,
        payload: VendorAsBuiltCreate,
        vendor_id: str,
        *,
        for_update: bool = False,
    ) -> dict:
        """Own the read-only impact snapshot for an as-built submission."""
        project = _project(db, str(payload.project_id), for_update=for_update)
        if str(project.assigned_vendor_id) != str(vendor_id):
            raise _error(
                "project_not_assigned",
                "Project is assigned to another vendor.",
            )
        if not payload.geojson and not payload.line_items:
            raise _error(
                "as_built_evidence_required",
                "Provide a route or line items.",
            )
        if payload.geojson:
            if payload.geojson.get("type") != "LineString" or not isinstance(
                payload.geojson.get("coordinates"), list
            ):
                raise _error(
                    "invalid_as_built_route",
                    "As-built route must be a GeoJSON LineString.",
                )
            if len(payload.geojson["coordinates"]) < 2:
                raise _error(
                    "invalid_as_built_route",
                    "As-built route requires at least two coordinates.",
                )
        length_label = (
            f"{payload.actual_length_meters:,.1f} m"
            if payload.actual_length_meters is not None
            else "Not supplied"
        )
        return {
            "submission_type": "as_built",
            "project_id": str(project.id),
            "target_id": None,
            "title": "Submit as-built route",
            "summary": "Creates the immutable route evidence staff will review",
            "details": [
                ("Route type", "GeoJSON LineString" if payload.geojson else "None"),
                ("Actual length", length_label),
                ("Variation reason", payload.variation_reason or "None"),
                ("Result", "A submitted as-built record is created for review"),
            ],
            "state": {
                "project_id": str(project.id),
                "project_status": project.status,
                "project_updated_at": project.updated_at,
                "assigned_vendor_id": str(project.assigned_vendor_id),
                "payload": payload.model_dump(mode="json"),
            },
            "payload": payload.model_dump(mode="json"),
        }

    @staticmethod
    def add_quote_line(
        db: Session,
        command: AddVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_add_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="add_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def update_quote_line(
        db: Session,
        command: UpdateVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_update_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="update_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def delete_quote_line(
        db: Session,
        command: DeleteVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_delete_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="delete_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def stage_quote_submission(
        db: Session,
        command: StageVendorQuoteSubmission,
    ) -> dict:
        """Stage quote submission in the signed-confirmation transaction."""

        from app.services import vendor_project_records

        return vendor_project_records.stage_quote_submission(db, command)

    @staticmethod
    def review_quote(
        db: Session,
        command: ReviewVendorQuoteCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_review_quote(db, command)

        return _execute(
            db,
            context=command.context,
            name="review_vendor_quote",
            operation=operation,
        )

    @staticmethod
    def create_route_revision(
        db: Session,
        command: CreateVendorRouteRevisionCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_create_route_revision(db, command)

        return _execute(
            db,
            context=command.context,
            name="create_vendor_route_revision",
            operation=operation,
        )

    @staticmethod
    def submit_route_revision(
        db: Session,
        command: SubmitVendorRouteRevisionCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_submit_route_revision(db, command)

        return _execute(
            db,
            context=command.context,
            name="submit_vendor_route_revision",
            operation=operation,
        )

    @staticmethod
    def stage_as_built_submission(
        db: Session,
        command: StageVendorAsBuiltSubmission,
    ) -> dict:
        """Stage as-built evidence in the signed-confirmation transaction."""

        from app.services import vendor_project_records

        return vendor_project_records.stage_as_built_submission(db, command)


vendor_portal_operations = VendorPortalOperations()
