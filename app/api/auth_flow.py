from typing import cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.db import get_db
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
    SessionListResponse,
    SessionRevokeResponse,
    TokenResponse,
)
from app.services import auth_flow as auth_flow_service
from app.services import session_manager as session_manager_service
from app.services import user_profile as user_profile_service
from app.services.auth_dependencies import require_user_auth

router = APIRouter(prefix="/auth", tags=["auth"])


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
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> Response | LoginResponse:
    result = auth_flow_service.auth_flow.login_response(
        db,
        payload.username,
        payload.password,
        request,
        payload.provider.value if payload.provider else None,
    )
    if isinstance(result, Response):
        return result
    return LoginResponse.model_validate(result)


@router.post(
    "/mfa/setup",
    response_model=MfaSetupResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_setup(
    payload: MfaSetupRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> MfaSetupResponse:
    if str(payload.subscriber_id) != str(auth["subscriber_id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    result = auth_flow_service.auth_flow.mfa_setup(
        db, str(payload.subscriber_id), payload.label
    )
    return MfaSetupResponse.model_validate(result)


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
) -> MFAMethodRead:
    result = auth_flow_service.auth_flow.mfa_confirm(
        db, str(payload.method_id), payload.code, auth["subscriber_id"]
    )
    return MFAMethodRead.model_validate(result)


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def mfa_verify(
    payload: MfaVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.mfa_verify_response(
            db, payload.mfa_token, payload.code, request
        ),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def refresh(
    payload: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.refresh_response(
            db, payload.refresh_token, request
        ),
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"model": ErrorResponse},
    },
)
def logout(
    payload: LogoutRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.logout_response(
            db, payload.refresh_token, request
        ),
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
) -> MeResponse:
    return user_profile_service.get_me(
        db,
        subscriber_id=auth["subscriber_id"],
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
) -> MeResponse:
    return user_profile_service.update_me(
        db,
        subscriber_id=auth["subscriber_id"],
        payload=payload,
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
) -> AvatarUploadResponse:
    return await user_profile_service.upload_avatar(
        db,
        subscriber_id=auth["subscriber_id"],
        file=file,
    )


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
) -> None:
    user_profile_service.delete_avatar(
        db,
        subscriber_id=auth["subscriber_id"],
    )


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
) -> SessionListResponse:
    return session_manager_service.list_sessions(
        db,
        subscriber_id=auth["subscriber_id"],
        current_session_id=auth.get("session_id"),
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
) -> SessionRevokeResponse:
    return session_manager_service.revoke_session(
        db,
        session_id=session_id,
        subscriber_id=auth["subscriber_id"],
    )


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
) -> SessionRevokeResponse:
    return session_manager_service.revoke_all_other_sessions(
        db,
        subscriber_id=auth["subscriber_id"],
        current_session_id=auth.get("session_id"),
    )


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
) -> PasswordChangeResponse:
    changed_at = auth_flow_service.change_password(
        db,
        subscriber_id=str(auth["subscriber_id"]),
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return PasswordChangeResponse(changed_at=changed_at)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> ForgotPasswordResponse:
    """
    Request a password reset email.
    Always returns success to prevent email enumeration.
    """
    auth_flow_service.forgot_password_flow(db, payload.email)
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
) -> ResetPasswordResponse:
    """
    Reset password using the token from forgot-password email.
    """
    reset_at = auth_flow_service.reset_password(db, payload.token, payload.new_password)
    return ResetPasswordResponse(reset_at=reset_at)
