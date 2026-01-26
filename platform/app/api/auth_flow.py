from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.auth import MFAMethodRead
from app.schemas.auth_flow import (
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MfaConfirmRequest,
    MfaSetupRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    RefreshRequest,
    TokenResponse,
)
from app.services import auth_flow as auth_flow_service

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
    result = auth_flow_service.auth_flow.login(
        db, payload.username, payload.password, request
    )
    if result.get("refresh_token"):
        settings = auth_flow_service.auth_flow.refresh_cookie_settings(db)
        response = Response(status_code=status.HTTP_200_OK)
        response.set_cookie(
            key=settings["key"],
            value=result["refresh_token"],
            httponly=settings["httponly"],
            secure=settings["secure"],
            samesite=settings["samesite"],
            domain=settings["domain"],
            path=settings["path"],
            max_age=settings["max_age"],
        )
        response.media_type = "application/json"
        result = {**result, "refresh_token": None}
        response.body = LoginResponse(**result).model_dump_json().encode("utf-8")
        return response
    return result


@router.post("/mfa/setup", response_model=MfaSetupResponse, status_code=status.HTTP_200_OK)
def mfa_setup(payload: MfaSetupRequest, db: Session = Depends(get_db)):
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
        404: {"model": ErrorResponse},
    },
)
def mfa_confirm(payload: MfaConfirmRequest, db: Session = Depends(get_db)):
    return auth_flow_service.auth_flow.mfa_confirm(db, str(payload.method_id), payload.code)


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
    result = auth_flow_service.auth_flow.mfa_verify(
        db, payload.mfa_token, payload.code, request
    )
    settings = auth_flow_service.auth_flow.refresh_cookie_settings(db)
    response = Response(status_code=status.HTTP_200_OK)
    response.set_cookie(
        key=settings["key"],
        value=result["refresh_token"],
        httponly=settings["httponly"],
        secure=settings["secure"],
        samesite=settings["samesite"],
        domain=settings["domain"],
        path=settings["path"],
        max_age=settings["max_age"],
    )
    response.media_type = "application/json"
    result = {**result, "refresh_token": None}
    response.body = TokenResponse(**result).model_dump_json().encode("utf-8")
    return response


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    settings = auth_flow_service.auth_flow.refresh_cookie_settings(db)
    refresh_token = auth_flow_service.auth_flow.resolve_refresh_token(
        request, payload.refresh_token, db
    )
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    result = auth_flow_service.auth_flow.refresh(db, refresh_token, request)
    response = Response(status_code=status.HTTP_200_OK)
    response.set_cookie(
        key=settings["key"],
        value=result["refresh_token"],
        httponly=settings["httponly"],
        secure=settings["secure"],
        samesite=settings["samesite"],
        domain=settings["domain"],
        path=settings["path"],
        max_age=settings["max_age"],
    )
    response.media_type = "application/json"
    result = {**result, "refresh_token": None}
    response.body = TokenResponse(**result).model_dump_json().encode("utf-8")
    return response


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"model": ErrorResponse},
    },
)
def logout(payload: LogoutRequest, request: Request, db: Session = Depends(get_db)):
    settings = auth_flow_service.auth_flow.refresh_cookie_settings(db)
    refresh_token = auth_flow_service.auth_flow.resolve_refresh_token(
        request, payload.refresh_token, db
    )
    if not refresh_token:
        raise HTTPException(status_code=404, detail="Session not found")
    result = auth_flow_service.auth_flow.logout(db, refresh_token)
    response = Response(status_code=status.HTTP_200_OK)
    response.delete_cookie(
        key=settings["key"],
        domain=settings["domain"],
        path=settings["path"],
    )
    response.media_type = "application/json"
    response.body = LogoutResponse(**result).model_dump_json().encode("utf-8")
    return response
