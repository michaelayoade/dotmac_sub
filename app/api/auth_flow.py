from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth import Session as AuthSession, SessionStatus, UserCredential
from app.models.person import Person
from app.schemas.auth import MFAMethodRead
from app.schemas.auth_flow import (
    AvatarUploadResponse,
    ErrorResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    MeUpdateRequest,
    MfaConfirmRequest,
    MfaSetupRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    PasswordChangeRequest,
    PasswordChangeResponse,
    RefreshRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SessionInfoResponse,
    SessionListResponse,
    SessionRevokeResponse,
    TokenResponse,
)
from app.services import auth_flow as auth_flow_service
from app.services import avatar as avatar_service
from app.services.auth_dependencies import require_user_auth
from app.services.auth_flow import (
    hash_password,
    request_password_reset,
    reset_password,
    verify_password,
)
from app.services.email import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        428: {
            "model": ErrorResponse,
            "description": "Password reset required",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "code": "PASSWORD_RESET_REQUIRED",
                            "message": "Password reset required",
                        }
                    }
                }
            },
        }
    },
)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    return auth_flow_service.auth_flow.login_response(
        db, payload.username, payload.password, request, payload.provider
    )


@router.post("/mfa/setup", response_model=MfaSetupResponse, status_code=status.HTTP_200_OK)
def mfa_setup(
    payload: MfaSetupRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    if str(payload.person_id) != str(auth["person_id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    return auth_flow_service.auth_flow.mfa_setup(
        db, str(payload.person_id), payload.label
    )


@router.post(
    "/mfa/confirm",
    response_model=MFAMethodRead,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def mfa_confirm(
    payload: MfaConfirmRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return auth_flow_service.auth_flow.mfa_confirm(
        db, str(payload.method_id), payload.code, auth["person_id"]
    )


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def mfa_verify(payload: MfaVerifyRequest, request: Request, db: Session = Depends(get_db)):
    return auth_flow_service.auth_flow.mfa_verify_response(
        db, payload.mfa_token, payload.code, request
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    return auth_flow_service.auth_flow.refresh_response(
        db, payload.refresh_token, request
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"model": ErrorResponse},
    },
)
def logout(payload: LogoutRequest, request: Request, db: Session = Depends(get_db)):
    return auth_flow_service.auth_flow.logout_response(
        db, payload.refresh_token, request
    )


@router.get(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def get_me(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    person = db.get(Person, auth["person_id"])
    if not person:
        raise HTTPException(status_code=404, detail="User not found")

    return MeResponse(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=person.display_name,
        avatar_url=person.avatar_url,
        email=person.email,
        email_verified=person.email_verified,
        phone=person.phone,
        date_of_birth=person.date_of_birth,
        gender=person.gender.value if person.gender else "unknown",
        preferred_contact_method=person.preferred_contact_method.value if person.preferred_contact_method else None,
        locale=person.locale,
        timezone=person.timezone,
        roles=auth.get("roles", []),
        scopes=auth.get("scopes", []),
    )


@router.patch(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def update_me(
    payload: MeUpdateRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    person = db.get(Person, auth["person_id"])
    if not person:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(person, field, value)

    db.commit()
    db.refresh(person)

    return MeResponse(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=person.display_name,
        avatar_url=person.avatar_url,
        email=person.email,
        email_verified=person.email_verified,
        phone=person.phone,
        date_of_birth=person.date_of_birth,
        gender=person.gender.value if person.gender else "unknown",
        preferred_contact_method=person.preferred_contact_method.value if person.preferred_contact_method else None,
        locale=person.locale,
        timezone=person.timezone,
        roles=auth.get("roles", []),
        scopes=auth.get("scopes", []),
    )


@router.post(
    "/me/avatar",
    response_model=AvatarUploadResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
    },
)
async def upload_avatar(
    file: UploadFile,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    person = db.get(Person, auth["person_id"])
    if not person:
        raise HTTPException(status_code=404, detail="User not found")

    # Delete old avatar if exists
    avatar_service.delete_avatar(person.avatar_url)

    # Save new avatar
    avatar_url = await avatar_service.save_avatar(file, str(person.id))

    # Update person record
    person.avatar_url = avatar_url
    db.commit()

    return AvatarUploadResponse(avatar_url=avatar_url)


@router.delete(
    "/me/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse},
    },
)
def delete_avatar(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    person = db.get(Person, auth["person_id"])
    if not person:
        raise HTTPException(status_code=404, detail="User not found")

    avatar_service.delete_avatar(person.avatar_url)
    person.avatar_url = None
    db.commit()


@router.get(
    "/me/sessions",
    response_model=SessionListResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def list_sessions(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    sessions = (
        db.query(AuthSession)
        .filter(AuthSession.person_id == auth["person_id"])
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .order_by(AuthSession.created_at.desc())
        .all()
    )

    current_session_id = auth.get("session_id")

    return SessionListResponse(
        sessions=[
            SessionInfoResponse(
                id=s.id,
                status=s.status.value,
                ip_address=s.ip_address,
                user_agent=s.user_agent,
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                expires_at=s.expires_at,
                is_current=(str(s.id) == current_session_id),
            )
            for s in sessions
        ],
        total=len(sessions),
    )


@router.delete(
    "/me/sessions/{session_id}",
    response_model=SessionRevokeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def revoke_session(
    session_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.person_id == auth["person_id"])
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == SessionStatus.revoked:
        raise HTTPException(status_code=400, detail="Session already revoked")

    now = datetime.now(timezone.utc)
    session.status = SessionStatus.revoked
    session.revoked_at = now
    db.commit()

    return SessionRevokeResponse(revoked_at=now)


@router.delete(
    "/me/sessions",
    response_model=SessionRevokeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def revoke_all_other_sessions(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    current_session_id = auth.get("session_id")

    sessions = (
        db.query(AuthSession)
        .filter(AuthSession.person_id == auth["person_id"])
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.id != current_session_id)
        .all()
    )

    now = datetime.now(timezone.utc)
    for session in sessions:
        session.status = SessionStatus.revoked
        session.revoked_at = now

    db.commit()

    return SessionRevokeResponse(revoked_at=now, revoked_count=len(sessions))


@router.post(
    "/me/password",
    response_model=PasswordChangeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def change_password(
    payload: PasswordChangeRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.person_id == auth["person_id"])
        .filter(UserCredential.is_active.is_(True))
        .first()
    )

    if not credential:
        raise HTTPException(status_code=404, detail="No credentials found")

    if not verify_password(payload.current_password, credential.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    now = datetime.now(timezone.utc)
    credential.password_hash = hash_password(payload.new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    db.commit()

    return PasswordChangeResponse(changed_at=now)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    """
    Request a password reset email.
    Always returns success to prevent email enumeration.
    """
    result = request_password_reset(db, payload.email)

    if result:
        send_password_reset_email(
            db=db,
            to_email=result["email"],
            reset_token=result["token"],
            person_name=result["person_name"],
        )

    return ForgotPasswordResponse()


@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def reset_password_endpoint(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
):
    """
    Reset password using the token from forgot-password email.
    """
    reset_at = reset_password(db, payload.token, payload.new_password)
    return ResetPasswordResponse(reset_at=reset_at)
