from collections.abc import Callable, Generator, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyGenerateResponse,
    ApiKeyRead,
    ApiKeyUpdate,
    MFAMethodRead,
    SessionRead,
    UserCredentialCreate,
    UserCredentialRead,
    UserCredentialUpdate,
)
from app.schemas.common import ListResponse
from app.services import auth as auth_service
from app.services import credential_administration as credential_admin
from app.services.auth_dependencies import (
    require_permission,
    require_role,
    require_user_auth,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

router = APIRouter()

# Defence in depth for the API-key minting/rewriting routes below.
#
# The whole ``app.api.auth`` router is already mounted admin-only
# (``("app.api.auth", "router", "api", "admin")`` in ``app/main.py``), and that
# mount stays the first layer. This is the second: minting or rewriting an API
# key is credential administration, and it should not depend on a single word
# in a mount spec staying correct.
#
# Deliberately the admin ROLE rather than a permission key
# (e.g. ``system:settings:write``): the authority to mint credentials must not
# silently widen if a permission later becomes assignable to a non-admin role.
#
# The exact route set is pinned by
# ``tests/architecture/test_credential_minting_route_guards.py``.
_credential_admin = Depends(require_role("admin"))


def _credential_command_session() -> Generator[Session, None, None]:
    # Authorization uses its own request session. Public command ownership
    # begins on a fresh session; no caller transaction is committed/discarded.
    with db_session_adapter.owner_command_session() as db:
        yield db


def _credential_context(auth: Mapping[str, object]) -> CommandContext:
    principal = auth.get("principal_id")
    if not principal:
        raise HTTPException(
            status_code=403, detail="Credential administrator identity is required"
        )
    return CommandContext.system(
        actor=str(principal),
        scope=credential_admin.SCOPE,
        reason="administrative credential command",
    )


def _credential_result(
    operation: Callable[[], credential_admin.CredentialOutcome],
) -> credential_admin.CredentialView:
    try:
        return operation().credential
    except credential_admin.CredentialAdministrationError as exc:
        suffix = exc.code.rsplit(".", 1)[-1]
        code = (
            503
            if suffix == "lock_timeout"
            else 409
            if suffix in {"conflict", "stale_credential"}
            else 404
            if suffix
            in {"principal_not_found", "credential_not_found", "radius_not_found"}
            else 422
        )
        raise HTTPException(
            status_code=code,
            detail=exc.message,
            headers={"Retry-After": "1"} if code == 503 else None,
        ) from exc


@router.post(
    "/user-credentials",
    response_model=UserCredentialRead,
    status_code=status.HTTP_201_CREATED,
    tags=["user-credentials"],
    dependencies=[
        Depends(require_permission("auth:credential:write")),
        _credential_admin,
    ],
)
def create_user_credential(
    payload: UserCredentialCreate,
    auth: Mapping[str, object] = _credential_admin,
    db: Session = Depends(_credential_command_session),
) -> credential_admin.CredentialView:
    command = credential_admin.CreateCredential(
        context=_credential_context(auth),
        subscriber_id=payload.subscriber_id,
        username=payload.username,
        provider=payload.provider,
        radius_server_id=payload.radius_server_id,
        password=payload.password,
    )
    return _credential_result(
        lambda: credential_admin.create_credential(db, command=command)
    )


@router.get(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:read"))],
)
def get_user_credential(credential_id: str, db: Session = Depends(get_db)):
    return auth_service.user_credentials.get(db, credential_id)


@router.get(
    "/user-credentials",
    response_model=ListResponse[UserCredentialRead],
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:read"))],
)
def list_user_credentials(
    subscriber_id: str | None = None,
    provider: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.user_credentials.list_response(
        db,
        subscriber_id=subscriber_id,
        provider=provider,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
    dependencies=[
        Depends(require_permission("auth:credential:write")),
        _credential_admin,
    ],
)
def update_user_credential(
    credential_id: UUID,
    payload: UserCredentialUpdate,
    auth: Mapping[str, object] = _credential_admin,
    db: Session = Depends(_credential_command_session),
) -> credential_admin.CredentialView:
    command = credential_admin.UpdateCredential(
        context=_credential_context(auth),
        credential_id=credential_id,
        submitted_fields=frozenset(
            credential_admin.CredentialField(name) for name in payload.model_fields_set
        ),
        username=payload.username,
        radius_server_id=payload.radius_server_id,
        password=payload.password,
    )
    return _credential_result(
        lambda: credential_admin.update_credential(db, command=command)
    )


@router.delete(
    "/user-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["user-credentials"],
    dependencies=[
        Depends(require_permission("auth:credential:write")),
        _credential_admin,
    ],
)
def delete_user_credential(
    credential_id: UUID,
    auth: Mapping[str, object] = _credential_admin,
    db: Session = Depends(_credential_command_session),
) -> None:
    command = credential_admin.DeactivateCredential(
        context=_credential_context(auth), credential_id=credential_id
    )
    _credential_result(
        lambda: credential_admin.deactivate_credential(db, command=command)
    )


@router.get(
    "/mfa-methods/{method_id}",
    response_model=MFAMethodRead,
    tags=["mfa-methods"],
)
def get_mfa_method(method_id: str, db: Session = Depends(get_db)):
    return auth_service.mfa_methods.get(db, method_id)


@router.get(
    "/mfa-methods",
    response_model=ListResponse[MFAMethodRead],
    tags=["mfa-methods"],
)
def list_mfa_methods(
    subscriber_id: str | None = None,
    method_type: str | None = None,
    is_primary: bool | None = None,
    enabled: bool | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.mfa_methods.list_response(
        db,
        subscriber_id=subscriber_id,
        method_type=method_type,
        is_primary=is_primary,
        enabled=enabled,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/mfa-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["mfa-methods"],
    dependencies=[
        Depends(require_permission("auth:credential:write")),
        _credential_admin,
    ],
)
def delete_mfa_method(
    method_id: str,
    reason: str = Query(
        ...,
        min_length=3,
        max_length=500,
        description=(
            "Why the factor is removed: verification channel and ticket "
            "reference. Stored in the audit evidence."
        ),
    ),
    auth: Mapping[str, object] = _credential_admin,
    db: Session = Depends(get_db),
) -> None:
    # Disabling a second factor is credential administration: admin role AND
    # the credential write permission, with the audit staged by the owner.
    principal = auth.get("principal_id")
    if not principal:
        raise HTTPException(
            status_code=403, detail="Credential administrator identity is required"
        )
    auth_service.mfa_methods.delete(
        db, method_id, actor_id=str(principal), reason=reason
    )


@router.get(
    "/sessions/{session_id}",
    response_model=SessionRead,
    tags=["sessions"],
)
def get_session(session_id: str, db: Session = Depends(get_db)):
    return auth_service.sessions.get(db, session_id)


@router.get(
    "/sessions",
    response_model=ListResponse[SessionRead],
    tags=["sessions"],
)
def list_sessions(
    subscriber_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.sessions.list_response(
        db,
        subscriber_id=subscriber_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sessions"],
)
def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_user_auth),
):
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    from app.services.audit_adapter import AuditActor
    from app.services.db_session_adapter import db_session_adapter
    from app.services.session_manager import SessionRevocationError

    actor = (
        AuditActor.api_key(str(auth["api_key_id"]))
        if auth.get("api_key_id")
        else AuditActor.user(str(auth["principal_id"]))
    )
    with db_session_adapter.owner_command_session() as command_db:
        try:
            auth_service.sessions.delete(command_db, session_id=session_id, actor=actor)
        except SessionRevocationError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        except OperationalError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) == "55P03"
                or getattr(exc.orig, "pgcode", None) == "55P03"
            ):
                raise HTTPException(
                    status_code=503,
                    detail="Session revocation busy",
                    headers={"Retry-After": "5"},
                ) from exc
            raise


@router.post(
    "/api-keys",
    response_model=ApiKeyRead,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
    dependencies=[_credential_admin],
)
def create_api_key(payload: ApiKeyCreate, db: Session = Depends(get_db)):
    return auth_service.api_keys.create(db, payload)


@router.post(
    "/api-keys/generate",
    response_model=ApiKeyGenerateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
    dependencies=[_credential_admin],
)
def generate_api_key(
    payload: ApiKeyGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    return auth_service.api_keys.generate_with_rate_limit(db, payload, request)


@router.get(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
)
def get_api_key(key_id: str, db: Session = Depends(get_db)):
    return auth_service.api_keys.get(db, key_id)


@router.get(
    "/api-keys",
    response_model=ListResponse[ApiKeyRead],
    tags=["api-keys"],
)
def list_api_keys(
    subscriber_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.api_keys.list_response(
        db,
        subscriber_id=subscriber_id,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
    dependencies=[_credential_admin],
)
def update_api_key(key_id: str, payload: ApiKeyUpdate, db: Session = Depends(get_db)):
    return auth_service.api_keys.update(db, key_id, payload)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
)
def delete_api_key(key_id: UUID, auth: dict = Depends(require_role("admin"))):
    from sqlalchemy.exc import OperationalError

    from app.services import api_key_administration as api_key_admin
    from app.services.audit_adapter import AuditActor

    actor = (
        AuditActor.api_key(str(auth["api_key_id"]))
        if auth.get("api_key_id")
        else AuditActor.user(str(auth["principal_id"]))
    )
    command = api_key_admin.RevokeApiKeyCommand(
        context=CommandContext.system(
            actor=str(actor.actor_id),
            scope=api_key_admin.SCOPE,
            reason="Administrative API-key revocation",
        ),
        key_id=key_id,
        actor=actor,
    )
    with db_session_adapter.owner_command_session() as command_db:
        try:
            api_key_admin.revoke(command_db, command=command)
        except api_key_admin.ApiKeyRevocationError as exc:
            raise HTTPException(
                status_code=404 if exc.code.endswith(".not_found") else 403,
                detail=exc.message,
            ) from exc
        except OperationalError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) == "55P03"
                or getattr(exc.orig, "pgcode", None) == "55P03"
            ):
                raise HTTPException(
                    status_code=503,
                    detail="API key revocation busy",
                    headers={"Retry-After": "5"},
                ) from exc
            raise
