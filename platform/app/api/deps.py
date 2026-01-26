from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth import ApiKey, Session as AuthSession, SessionStatus
from app.models.rbac import Permission, PersonRole, RolePermission, Role
from app.services.auth import hash_api_key
from app.services.auth_flow import decode_access_token, hash_session_token


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _is_jwt(token: str) -> bool:
    return token.count(".") == 2


def _has_audit_scope(payload: dict) -> bool:
    scopes: set[str] = set()
    scope_value = payload.get("scope")
    if isinstance(scope_value, str):
        scopes.update(scope_value.split())
    scopes_value = payload.get("scopes")
    if isinstance(scopes_value, list):
        scopes.update(str(item) for item in scopes_value)
    role_value = payload.get("role")
    roles_value = payload.get("roles")
    roles: set[str] = set()
    if isinstance(role_value, str):
        roles.add(role_value)
    if isinstance(roles_value, list):
        roles.update(str(item) for item in roles_value)
    return (
        "audit:read" in scopes
        or "audit:*" in scopes
        or "admin" in roles
        or "auditor" in roles
    )


def require_audit_auth(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    request: Request | None = None,
    db: Session = Depends(_get_db),
):
    token = _extract_bearer_token(authorization) or x_session_token
    now = datetime.utcnow()
    if token:
        if _is_jwt(token):
            payload = decode_access_token(db, token)
            if not _has_audit_scope(payload):
                raise HTTPException(status_code=403, detail="Insufficient scope")
            session_id = payload.get("session_id")
            if session_id:
                session = db.get(AuthSession, session_id)
                if not session:
                    raise HTTPException(status_code=401, detail="Invalid session")
                if session.status != SessionStatus.active or session.revoked_at:
                    raise HTTPException(status_code=401, detail="Invalid session")
                if session.expires_at <= now:
                    raise HTTPException(status_code=401, detail="Session expired")
            actor_id = str(payload.get("sub"))
            if request is not None:
                request.state.actor_id = actor_id
            return {"actor_type": "user", "actor_id": actor_id}
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == hash_session_token(token))
            .filter(AuthSession.status == SessionStatus.active)
            .filter(AuthSession.revoked_at.is_(None))
            .filter(AuthSession.expires_at > now)
            .first()
        )
        if session:
            if request is not None:
                request.state.actor_id = str(session.person_id)
            return {"actor_type": "user", "actor_id": str(session.person_id)}
    if x_api_key:
        api_key = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash == hash_api_key(x_api_key))
            .filter(ApiKey.is_active.is_(True))
            .filter(ApiKey.revoked_at.is_(None))
            .filter((ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))
            .first()
        )
        if api_key:
            if request is not None:
                request.state.actor_id = str(api_key.id)
            return {"actor_type": "api_key", "actor_id": str(api_key.id)}
    raise HTTPException(status_code=401, detail="Unauthorized")


def require_user_auth(
    authorization: str | None = Header(default=None),
    request: Request | None = None,
    db: Session = Depends(_get_db),
):
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.utcnow()
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.person_id == person_id)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at > now)
        .first()
    )
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    roles_value = payload.get("roles")
    scopes_value = payload.get("scopes")
    roles = [str(role) for role in roles_value] if isinstance(roles_value, list) else []
    scopes = [str(scope) for scope in scopes_value] if isinstance(scopes_value, list) else []
    actor_id = str(person_id)
    if request is not None:
        request.state.actor_id = actor_id
    return {
        "person_id": str(person_id),
        "session_id": str(session_id),
        "roles": roles,
        "scopes": scopes,
    }


def require_role(role_name: str):
    def _require_role(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = auth["person_id"]
        roles = set(auth.get("roles") or [])
        if role_name in roles:
            return auth
        role = (
            db.query(Role)
            .filter(Role.name == role_name)
            .filter(Role.is_active.is_(True))
            .first()
        )
        if not role:
            raise HTTPException(status_code=403, detail="Role not found")
        link = (
            db.query(PersonRole)
            .filter(PersonRole.person_id == person_id)
            .filter(PersonRole.role_id == role.id)
            .first()
        )
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_role


def require_permission(permission_key: str):
    def _require_permission(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = auth["person_id"]
        roles = set(auth.get("roles") or [])
        if "admin" in roles:
            return auth
        permission = (
            db.query(Permission)
            .filter(Permission.key == permission_key)
            .filter(Permission.is_active.is_(True))
            .first()
        )
        if not permission:
            raise HTTPException(status_code=403, detail="Permission not found")
        has_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(PersonRole, PersonRole.role_id == Role.id)
            .filter(PersonRole.person_id == person_id)
            .filter(RolePermission.permission_id == permission.id)
            .filter(Role.is_active.is_(True))
            .first()
        )
        if not has_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_permission
