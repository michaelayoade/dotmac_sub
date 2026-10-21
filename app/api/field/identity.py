"""Restricted identity adapters. Every file read repeats subject/reviewer policy."""

from dataclasses import asdict
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.field.execution import field_domain_errors, field_system_user_id
from app.csrf import get_csrf_token, set_csrf_cookie
from app.csrf import require_browser_csrf_header as _browser_csrf
from app.db import get_db
from app.models.field_identity import IdentityState
from app.services import field_identity
from app.services.auth_dependencies import (
    load_permission_keys,
    require_permission,
    require_user_auth,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.field_identity_contracts import (
    REVIEW_PERMISSION,
    WRITE_SCOPE,
    IdentityAccess,
    IdentityDetail,
    IdentityOutcome,
    ReviewIdentity,
    SubmitIdentity,
)
from app.services.file_storage import FileValidationError, file_uploads
from app.services.object_storage import ObjectNotFoundError
from app.services.owner_commands import CommandContext

router = APIRouter(
    prefix="/identity", tags=["field-identity"], dependencies=[Depends(_browser_csrf)]
)


def _access(auth: dict, db: Session) -> IdentityAccess:
    return IdentityAccess(field_system_user_id(auth), load_permission_keys(auth, db))


def _context(access: IdentityAccess, request_id: UUID, reason: str) -> CommandContext:
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{access.system_user_id}",
        scope=WRITE_SCOPE,
        reason=reason,
        idempotency_key=str(request_id),
    )


class ReviewRequest(BaseModel):
    request_id: UUID
    revision: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    decision: IdentityState
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/{subject_id}", response_model=IdentityDetail)
def get_identity(
    subject_id: UUID,
    response: Response,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> IdentityDetail:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    with field_domain_errors():
        return field_identity.detail(
            db, access=_access(auth, db), subject_id=subject_id
        )


@router.post("/{subject_id}", response_model=IdentityOutcome)
async def submit_identity(
    subject_id: UUID,
    request_id: UUID = Form(...),
    expected_revision: int = Form(...),
    display_name: str = Form(...),
    company_name: str = Form(...),
    portrait: UploadFile = File(...),
    evidence: UploadFile = File(...),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> IdentityOutcome:
    access = _access(auth, db)
    with field_domain_errors():
        field_identity.authorize_upload(db, access=access, subject_id=subject_id)
    db_session_adapter.release_read_transaction(db)
    prepared = []
    for upload, entity_type in (
        (portrait, "field_identity_portrait"),
        (evidence, "field_identity_evidence"),
    ):
        data = await upload.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Identity file exceeds 5 MiB")
        try:
            prepared.append(
                field_identity.prepare_identity_upload(
                    subject_id=subject_id,
                    portrait=entity_type == "field_identity_portrait",
                    filename=upload.filename or "upload",
                    content_type=upload.content_type,
                    data=data,
                )
            )
        except (FileValidationError, field_identity.FieldIdentityError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    with field_domain_errors():
        return field_identity.submit(
            db,
            command=SubmitIdentity(
                context=_context(
                    access, request_id, "Submit restricted identity for manual review"
                ),
                access=access,
                subject_id=subject_id,
                expected_revision=expected_revision,
                display_name=display_name,
                company_name=company_name,
                portrait=prepared[0],
                evidence=prepared[1],
            ),
        )


@router.post("/{subject_id}/review", response_model=IdentityOutcome)
def review_identity(
    subject_id: UUID,
    payload: ReviewRequest,
    auth: dict = Depends(require_permission(REVIEW_PERMISSION)),
    db: Session = Depends(get_db),
) -> IdentityOutcome:
    access = _access(auth, db)
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return field_identity.review(
            db,
            command=ReviewIdentity(
                context=_context(access, payload.request_id, payload.reason),
                access=access,
                subject_id=subject_id,
                revision=payload.revision,
                expected_version=payload.expected_version,
                decision=payload.decision,
            ),
        )


@router.get("/{subject_id}/files/{file_id}")
def identity_file(
    subject_id: UUID,
    file_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    with field_domain_errors():
        row = field_identity.private_file(
            db, access=_access(auth, db), subject_id=subject_id, file_id=file_id
        )
    try:
        stream = file_uploads.stream_file(row)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )


@router.get("/{subject_id}/review-page", response_class=HTMLResponse)
def review_page(
    subject_id: UUID,
    request: Request,
    auth: dict = Depends(require_permission(REVIEW_PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Small staff review adapter with explicit revision and independent reviewer."""
    import html

    with field_domain_errors():
        info = field_identity.detail(
            db, access=_access(auth, db), subject_id=subject_id
        )
    data = asdict(info)
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[3] / "templates"),
        autoescape=select_autoescape(),
    )
    csrf_token = get_csrf_token(request)
    response = HTMLResponse(
        env.get_template("admin/field_identity/review.html").render(
            identity=data,
            subject_id=str(subject_id),
            request_id=str(uuid4()),
            title=html.escape(info.display_name),
            csrf_token=csrf_token,
        ),
        headers={"Cache-Control": "private, no-store"},
    )
    set_csrf_cookie(response, csrf_token, request)
    return response
