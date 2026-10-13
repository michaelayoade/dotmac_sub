"""Thin staff and selected-field-actor completion-review transports."""

from collections.abc import Mapping
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.field.execution import field_domain_errors, field_system_user_id
from app.csrf import require_browser_csrf_header
from app.db import get_db
from app.schemas.field_completion_review import (
    CompletionCriteriaInput,
    CompletionDecisionInput,
    CompletionEnrollmentInput,
    CompletionPolicyInput,
    CompletionRevocationInput,
    CompletionSubmissionInput,
    FieldCompletionPreviewResponse,
)

# Replacement commands approve scope coverage, never execution or completion.
from app.schemas.field_failed_scope_replacement import (
    ReplacementReviewInput,
    ReplacementRevocationInput,
)
from app.services.auth_dependencies import (
    require_permission_admission,
    require_user_auth,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionEvidenceContent,
    CompletionEvidenceQuery,
    CompletionPolicyPurpose,
    CompletionPolicyQuery,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionPolicySnapshot,
    CompletionReviewHistory,
    CompletionReviewOutcome,
    CompletionReviewQuery,
    CompletionWorkspace,
    CompletionWorkspacePurpose,
    CompletionWorkspaceQuery,
    FailedScopeReplacementOutcome,
    FailedScopeReplacementWorkspace,
    FailedScopeReplacementWorkspaceQuery,
    FieldSubmissionPreviewQuery,
    ReplacementWorkspacePurpose,
)
from app.services.file_storage import build_content_disposition
from app.services.permission_admission import PermissionAdmission

router = APIRouter(prefix="/completion-review", tags=["completion-review"])
field_router = APIRouter(prefix="/field", tags=["field-completion-review"])


@router.get("/projects/{project_id}/policy")
def policy(
    project_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> CompletionPolicySnapshot:
    with field_domain_errors():
        return owner.policy_snapshot(
            db,
            query=CompletionPolicyQuery(
                access=access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=project_id
                ),
            ),
        )


@router.get("/projects/{project_id}/policy/edit")
def policy_preview(
    project_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionPolicySnapshot:
    with field_domain_errors():
        return owner.policy_snapshot(
            db,
            query=CompletionPolicyQuery(
                access=access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=project_id
                ),
                purpose=CompletionPolicyPurpose.publish_policy,
            ),
        )


@router.post(
    "/projects/{project_id}/policy", dependencies=[Depends(require_browser_csrf_header)]
)
def publish_policy(
    project_id: UUID,
    payload: CompletionPolicyInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.publish_policy(
            db,
            command=payload.command(
                access=access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=project_id
                ),
            ),
        )


@router.get("/work-orders/{work_order_public_id}")
def workspace(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> CompletionWorkspace:
    with field_domain_errors():
        return owner.workspace(
            db,
            query=CompletionWorkspaceQuery(
                access=access, work_order_public_id=work_order_public_id
            ),
        )


@router.get("/work-orders/{work_order_public_id}/review")
def review_preview(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionWorkspace:
    with field_domain_errors():
        return owner.workspace(
            db,
            query=CompletionWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=CompletionWorkspacePurpose.decide,
            ),
        )


@router.get("/work-orders/{work_order_public_id}/revoke")
def revoke_preview(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionWorkspace:
    with field_domain_errors():
        return owner.workspace(
            db,
            query=CompletionWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=CompletionWorkspacePurpose.revoke,
            ),
        )


@router.get("/enrollments/{enrollment_id}/current")
def current(
    enrollment_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    with field_domain_errors():
        return owner.current(
            db, query=CompletionReviewQuery(access=access, enrollment_id=enrollment_id)
        )


@router.get("/enrollments/{enrollment_id}/history")
def history(
    enrollment_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> CompletionReviewHistory:
    with field_domain_errors():
        return owner.history(
            db, query=CompletionReviewQuery(access=access, enrollment_id=enrollment_id)
        )


@router.post(
    "/enrollments/{enrollment_id}/decisions",
    dependencies=[Depends(require_browser_csrf_header)],
)
def decide(
    enrollment_id: UUID,
    payload: CompletionDecisionInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.decide(
            db, command=payload.command(access=access, enrollment_id=enrollment_id)
        )


@router.post(
    "/enrollments/{enrollment_id}/revocations",
    dependencies=[Depends(require_browser_csrf_header)],
)
def revoke(
    enrollment_id: UUID,
    payload: CompletionRevocationInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.revoke(
            db, command=payload.command(access=access, enrollment_id=enrollment_id)
        )


def field_requester(auth: Mapping[str, object] = Depends(require_user_auth)) -> UUID:
    return field_system_user_id(auth)


@field_router.get(
    "/jobs/{work_order_id}/completion-review",
    response_model=FieldCompletionPreviewResponse,
)
def field_preview(
    work_order_id: str,
    requester: UUID = Depends(field_requester),
    db: Session = Depends(get_db),
) -> FieldCompletionPreviewResponse:
    with field_domain_errors():
        preview = owner.field_submission_preview(
            db,
            query=FieldSubmissionPreviewQuery(
                requester_system_user_id=requester, work_order_public_id=work_order_id
            ),
        )
    return FieldCompletionPreviewResponse.model_validate(preview)


@field_router.post(
    "/jobs/{work_order_id}/completion-review/submissions",
    dependencies=[Depends(require_browser_csrf_header)],
)
def field_submit(
    work_order_id: str,
    payload: CompletionSubmissionInput,
    requester: UUID = Depends(field_requester),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.submit_evidence(
            db,
            command=payload.command(
                requester_system_user_id=requester, work_order_public_id=work_order_id
            ),
        )


def _evidence_response(content: CompletionEvidenceContent) -> StreamingResponse:
    return StreamingResponse(
        content.stream.chunks,
        media_type=content.evidence.content_type,
        headers={
            "Content-Disposition": build_content_disposition(content.evidence.filename),
            "Content-Length": str(content.evidence.file_size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/enrollments/{enrollment_id}/submissions/{submission_id}/attachments/{attachment_id}/content"
)
def evidence(
    enrollment_id: UUID,
    submission_id: UUID,
    attachment_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    with field_domain_errors():
        content = owner.evidence_content(
            db,
            query=CompletionEvidenceQuery(
                access=access,
                enrollment_id=enrollment_id,
                submission_id=submission_id,
                attachment_id=attachment_id,
            ),
        )
    return _evidence_response(content)


@router.get(
    "/enrollments/{enrollment_id}/submissions/{submission_id}/attachments/{attachment_id}/review-content"
)
def review_evidence(
    enrollment_id: UUID,
    submission_id: UUID,
    attachment_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    with field_domain_errors():
        content = owner.evidence_content(
            db,
            query=CompletionEvidenceQuery(
                access=access,
                enrollment_id=enrollment_id,
                submission_id=submission_id,
                attachment_id=attachment_id,
                purpose=CompletionWorkspacePurpose.decide,
            ),
        )
    return _evidence_response(content)


@router.get("/work-orders/{work_order_public_id}/policy/edit")
def work_order_policy_preview(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionPolicySnapshot:
    with field_domain_errors():
        return owner.policy_snapshot(
            db,
            query=CompletionPolicyQuery(
                access=access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.work_order,
                    work_order_public_id=work_order_public_id,
                ),
                purpose=CompletionPolicyPurpose.publish_policy,
            ),
        )


@router.post(
    "/work-orders/{work_order_public_id}/policy",
    dependencies=[Depends(require_browser_csrf_header)],
)
def publish_work_order_policy(
    work_order_public_id: str,
    payload: CompletionPolicyInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.publish_policy(
            db,
            command=payload.command(
                access=access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.work_order,
                    work_order_public_id=work_order_public_id,
                ),
            ),
        )


@router.post(
    "/work-orders/{work_order_public_id}/enrollment",
    dependencies=[Depends(require_browser_csrf_header)],
)
def enroll(
    work_order_public_id: str,
    payload: CompletionEnrollmentInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.enroll(
            db,
            command=payload.command(
                access=access, work_order_public_id=work_order_public_id
            ),
        )


@router.post(
    "/enrollments/{enrollment_id}/criteria",
    dependencies=[Depends(require_browser_csrf_header)],
)
def publish_criteria(
    enrollment_id: UUID,
    payload: CompletionCriteriaInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> CompletionReviewOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.publish_criteria(
            db, command=payload.command(access=access, enrollment_id=enrollment_id)
        )


@router.get("/work-orders/{work_order_public_id}/replacement-review")
def replacement_preview(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> FailedScopeReplacementWorkspace:
    with field_domain_errors():
        return owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=ReplacementWorkspacePurpose.review,
            ),
        )


@router.get("/work-orders/{work_order_public_id}/replacement-history")
def replacement_history(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> FailedScopeReplacementWorkspace:
    with field_domain_errors():
        return owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
            ),
        )


@router.post(
    "/enrollments/{source_enrollment_id}/replacement-decisions",
    dependencies=[Depends(require_browser_csrf_header)],
)
def review_replacement(
    source_enrollment_id: UUID,
    payload: ReplacementReviewInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> FailedScopeReplacementOutcome:
    with field_domain_errors():
        command = payload.command(
            access=access, source_enrollment_id=source_enrollment_id
        )
        db_session_adapter.release_read_transaction(db)
        return owner.review_failed_scope_replacement(db, command=command)


@router.post(
    "/enrollments/{source_enrollment_id}/replacement-revocations",
    dependencies=[Depends(require_browser_csrf_header)],
)
def revoke_replacement(
    source_enrollment_id: UUID,
    payload: ReplacementRevocationInput,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> FailedScopeReplacementOutcome:
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return owner.revoke_failed_scope_replacement(
            db,
            command=payload.command(
                access=access, source_enrollment_id=source_enrollment_id
            ),
        )


@router.get("/work-orders/{work_order_public_id}/replacement-revoke")
def replacement_revoke_preview(
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> FailedScopeReplacementWorkspace:
    with field_domain_errors():
        return owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=ReplacementWorkspacePurpose.revoke,
            ),
        )
