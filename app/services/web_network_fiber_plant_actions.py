"""Web action helpers for admin fiber plant routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Request

from app.models.network import FdhCabinet, Splitter
from app.services import web_network_fdh as fdh_service
from app.services import web_network_fiber_plant as fiber_plant_service
from app.services.audit_helpers import build_audit_activities, log_audit_event


@dataclass
class FiberWebActionResult:
    success: bool
    redirect_url: str | None = None
    form_context: dict[str, object] | None = None
    status_code: int = 200
    not_found_message: str | None = None


def _actor_id_from_request(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    subscriber_id = current_user.get("subscriber_id")
    return str(subscriber_id) if subscriber_id else None


def _current_person_id(request: Request) -> str:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user["person_id"])


def _log_fiber_audit_event(
    db,
    request: Request,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object] | None,
) -> None:
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
    )


def activity_for_entity(db, entity_type: str, entity_id: str, *, limit: int = 10):
    return build_audit_activities(db, entity_type, entity_id, limit=limit)


def approve_change_request_from_form(
    request: Request, db, *, request_id: str, form
) -> str:
    review_notes = fiber_plant_service.form_optional_str(form, "review_notes")
    force_apply = form.get("force_apply") == "true"
    approved, error = fiber_plant_service.approve_change_request(
        db,
        request_id=request_id,
        reviewer_person_id=_current_person_id(request),
        review_notes=review_notes,
        force_apply=force_apply,
    )
    if not approved and error == "conflict":
        return f"/admin/network/fiber-change-requests/{request_id}?error=conflict"

    _log_fiber_audit_event(
        db,
        request,
        action="approve",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        metadata={"force_apply": force_apply, "review_notes": review_notes},
    )
    return f"/admin/network/fiber-change-requests/{request_id}"


def reject_change_request_from_form(
    request: Request, db, *, request_id: str, form
) -> str:
    review_notes = fiber_plant_service.form_optional_str(form, "review_notes")
    error = fiber_plant_service.reject_change_request(
        db,
        request_id=request_id,
        reviewer_person_id=_current_person_id(request),
        review_notes=review_notes,
    )
    if error == "reject_note_required":
        return (
            f"/admin/network/fiber-change-requests/{request_id}"
            "?error=reject_note_required"
        )

    _log_fiber_audit_event(
        db,
        request,
        action="reject",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        metadata={"review_notes": review_notes},
    )
    return f"/admin/network/fiber-change-requests/{request_id}"


def bulk_approve_change_requests_from_form(request: Request, db, form) -> str:
    request_ids = fiber_plant_service.form_getlist_str(form, "request_ids")
    force_apply = form.get("force_apply") == "true"
    result = fiber_plant_service.bulk_approve_change_requests(
        db,
        request_ids=request_ids,
        reviewer_person_id=_current_person_id(request),
        force_apply=force_apply,
    )
    for request_id in cast(list[object], result["approved_request_ids"]):
        _log_fiber_audit_event(
            db,
            request,
            action="approve",
            entity_type="fiber_change_request",
            entity_id=str(request_id),
            metadata={"force_apply": force_apply, "review_notes": "Bulk approved"},
        )
    return (
        "/admin/network/fiber-change-requests?bulk=approved"
        f"&skipped={result['skipped']}"
    )


def create_cabinet_from_form(request: Request, db, form) -> FiberWebActionResult:
    result = fdh_service.create_cabinet_submission(
        db, form, action_url="/admin/network/fdh-cabinets"
    )
    if result["error"]:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object], result["form_context"]),
        )

    cabinet = cast(FdhCabinet | None, result["cabinet"])
    if cabinet is None:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object] | None, result["form_context"]),
            status_code=400,
        )

    _log_fiber_audit_event(
        db,
        request,
        action="create",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        metadata={"name": cabinet.name, "code": cabinet.code},
    )
    return FiberWebActionResult(
        success=True, redirect_url=f"/admin/network/fdh-cabinets/{cabinet.id}"
    )


def update_cabinet_from_form(
    request: Request, db, *, cabinet_id: str, form
) -> FiberWebActionResult:
    cabinet = fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return FiberWebActionResult(
            success=False, not_found_message="FDH Cabinet not found", status_code=404
        )

    result = fdh_service.update_cabinet_submission(
        db,
        cabinet,
        form,
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )
    if result["error"]:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object], result["form_context"]),
        )

    _log_fiber_audit_event(
        db,
        request,
        action="update",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        metadata=cast(dict[str, object] | None, result["metadata"]),
    )
    return FiberWebActionResult(
        success=True, redirect_url=f"/admin/network/fdh-cabinets/{cabinet.id}"
    )


def create_splitter_from_form(request: Request, db, form) -> FiberWebActionResult:
    result = fdh_service.create_splitter_submission(
        db, form, action_url="/admin/network/splitters"
    )
    if result["error"]:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object], result["form_context"]),
        )

    splitter = cast(Splitter | None, result["splitter"])
    if splitter is None:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object] | None, result["form_context"]),
            status_code=400,
        )

    _log_fiber_audit_event(
        db,
        request,
        action="create",
        entity_type="splitter",
        entity_id=str(splitter.id),
        metadata={
            "name": splitter.name,
            "fdh_id": str(splitter.fdh_id) if splitter.fdh_id else None,
        },
    )
    return FiberWebActionResult(
        success=True, redirect_url=f"/admin/network/splitters/{splitter.id}"
    )


def update_splitter_from_form(
    request: Request, db, *, splitter_id: str, form
) -> FiberWebActionResult:
    splitter = fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return FiberWebActionResult(
            success=False, not_found_message="Splitter not found", status_code=404
        )

    result = fdh_service.update_splitter_submission(
        db,
        splitter,
        form,
        action_url=f"/admin/network/splitters/{splitter.id}",
    )
    if result["error"]:
        return FiberWebActionResult(
            success=False,
            form_context=cast(dict[str, object], result["form_context"]),
        )

    _log_fiber_audit_event(
        db,
        request,
        action="update",
        entity_type="splitter",
        entity_id=str(splitter.id),
        metadata=cast(dict[str, object] | None, result["metadata"]),
    )
    return FiberWebActionResult(
        success=True, redirect_url=f"/admin/network/splitters/{splitter.id}"
    )
