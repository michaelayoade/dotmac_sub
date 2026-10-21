"""Thin staff adapters; each capability has its exact permission, without a read floor."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.project_engineering import (
    EngineeringDraftInput,
    EngineeringIssuanceInput,
    EngineeringPolicyInput,
    EngineeringReviewInput,
    EngineeringRevocationInput,
)
from app.services import project_engineering_plans as owner
from app.services import project_work_order_handoff
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    POLICY,
    READ,
    REVOKE,
    WRITE,
    EngineeringPlanOutcome,
    EngineeringReadPurpose,
    EngineeringTechnology,
    EngineeringWorkspace,
    EngineeringWorkspaceQuery,
    ProjectDispatchPreview,
    ProjectDispatchPreviewQuery,
)
from app.services.project_work_order_handoff_contracts import (
    PERMISSION,
    ProjectWorkOrderIssued,
)

router = APIRouter(
    prefix="/projects/{project_id}/engineering", tags=["project-engineering"]
)


def error_status(error: DomainError) -> int:
    if error.code.endswith((".denied", ".unauthorized", "_forbidden")):
        return 403
    if error.code.endswith((".not_found", "_not_found")):
        return 404
    if error.code.endswith(
        (
            ".stale_state",
            ".unapproved",
            ".scope_mismatch",
            ".idempotency_conflict",
            ".relationship_conflict",
        )
    ):
        return 409
    return 422


def _read(
    db: Session,
    *,
    access: PermissionAdmission,
    project_id: UUID,
    task_id: UUID | None,
    technology: EngineeringTechnology,
    purpose: EngineeringReadPurpose,
    history_offset: int = 0,
    selected_revision_id: UUID | None = None,
) -> EngineeringWorkspace:
    try:
        return owner.read_workspace(
            db,
            query=EngineeringWorkspaceQuery(
                access=access,
                project_id=project_id,
                task_id=task_id,
                technology=technology,
                purpose=purpose,
                history_offset=history_offset,
                selected_revision_id=selected_revision_id,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.get("")
def overview(
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    selected_revision_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> EngineeringWorkspace:
    return _read(
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        purpose=EngineeringReadPurpose.overview,
        history_offset=history_offset,
        selected_revision_id=selected_revision_id,
    )


@router.get("/policy")
def policy(
    project_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(POLICY)),
    db: Session = Depends(get_db),
) -> EngineeringWorkspace:
    return _read(
        db,
        access=access,
        project_id=project_id,
        task_id=None,
        technology=EngineeringTechnology.fiber,
        purpose=EngineeringReadPurpose.policy,
    )


@router.get("/draft")
def draft(
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> EngineeringWorkspace:
    return _read(
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        purpose=EngineeringReadPurpose.draft,
    )


@router.get("/review")
def review(
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(APPROVE)),
    db: Session = Depends(get_db),
) -> EngineeringWorkspace:
    return _read(
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        purpose=EngineeringReadPurpose.review,
    )


@router.get("/revoke")
def revoke(
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(REVOKE)),
    db: Session = Depends(get_db),
) -> EngineeringWorkspace:
    return _read(
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        purpose=EngineeringReadPurpose.revoke,
    )


@router.get("/issuance")
def issuance(
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> ProjectDispatchPreview:
    try:
        return owner.preview_dispatch(
            db,
            query=ProjectDispatchPreviewQuery(
                access=access,
                project_id=project_id,
                task_id=task_id,
                technology=technology,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.post("/policy")
def publish_policy(
    project_id: UUID,
    payload: EngineeringPolicyInput,
    access: PermissionAdmission = Depends(require_permission_admission(POLICY)),
    db: Session = Depends(get_db),
) -> EngineeringPlanOutcome:
    db_session_adapter.release_read_transaction(db)
    try:
        return owner.publish_policy(
            db, command=payload.command(access=access, project_id=project_id)
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.post("/draft")
def publish_draft(
    project_id: UUID,
    payload: EngineeringDraftInput,
    task_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> EngineeringPlanOutcome:
    db_session_adapter.release_read_transaction(db)
    try:
        return owner.draft_revision(
            db,
            command=payload.command(
                access=access, project_id=project_id, task_id=task_id
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.post("/review")
def decide_review(
    project_id: UUID,
    payload: EngineeringReviewInput,
    task_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(APPROVE)),
    db: Session = Depends(get_db),
) -> EngineeringPlanOutcome:
    db_session_adapter.release_read_transaction(db)
    try:
        return owner.decide(
            db,
            command=payload.command(
                access=access, project_id=project_id, task_id=task_id
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.post("/revoke")
def revoke_approval(
    project_id: UUID,
    payload: EngineeringRevocationInput,
    task_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(REVOKE)),
    db: Session = Depends(get_db),
) -> EngineeringPlanOutcome:
    db_session_adapter.release_read_transaction(db)
    try:
        return owner.decide(
            db,
            command=payload.command(
                access=access, project_id=project_id, task_id=task_id
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc


@router.post("/issuance")
def issue(
    project_id: UUID,
    payload: EngineeringIssuanceInput,
    task_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> ProjectWorkOrderIssued:
    db_session_adapter.release_read_transaction(db)
    try:
        return project_work_order_handoff.issue(
            db,
            command=payload.command(
                access=access, project_id=project_id, task_id=task_id
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=error_status(exc), detail=exc.message) from exc
