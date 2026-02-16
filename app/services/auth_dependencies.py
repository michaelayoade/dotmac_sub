from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db as _get_db
from app.models.auth import ApiKey, Session as AuthSession, SessionStatus
from app.models.rbac import Permission, SubscriberPermission, SubscriberRole, Role, RolePermission
from app.services.auth import hash_api_key
from app.services.auth_flow import decode_access_token, hash_session_token


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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
    request: Request = None,
    db: Session = Depends(_get_db),
):
    token = _extract_bearer_token(authorization) or x_session_token
    now = datetime.now(timezone.utc)
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
                expires_at = _as_utc(session.expires_at)
                if expires_at and expires_at <= now:
                    raise HTTPException(status_code=401, detail="Session expired")
            actor_id = str(payload.get("sub"))
            if request is not None:
                request.state.actor_id = actor_id
                request.state.actor_type = "user"
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
                request.state.actor_id = str(session.subscriber_id)
                request.state.actor_type = "user"
            return {"actor_type": "user", "actor_id": str(session.subscriber_id)}
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
                request.state.actor_type = "api_key"
            return {"actor_type": "api_key", "actor_id": str(api_key.id)}
    raise HTTPException(status_code=401, detail="Unauthorized")


def require_user_auth(
    authorization: str | None = Header(default=None),
    request: Request = None,
    db: Session = Depends(_get_db),
):
    token = _extract_bearer_token(authorization)
    if not token and request is not None:
        token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_access_token(db, token)
    subscriber_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not subscriber_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(timezone.utc)
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.subscriber_id == subscriber_id)
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
    actor_id = str(subscriber_id)
    if request is not None:
        request.state.actor_id = actor_id
        request.state.actor_type = "user"
    return {
        "subscriber_id": str(subscriber_id),
        "session_id": str(session_id),
        "roles": roles,
        "scopes": scopes,
    }


def require_role(role_name: str):
    def _require_role(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        subscriber_id = auth["subscriber_id"]
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
            db.query(SubscriberRole)
            .filter(SubscriberRole.subscriber_id == subscriber_id)
            .filter(SubscriberRole.role_id == role.id)
            .first()
        )
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_role


def _expand_permission_keys(permission_key: str) -> list[str]:
    """
    Expand a permission key to include hierarchical matches.

    For granular permissions like 'billing:invoice:create', this returns:
    - 'billing:invoice:create' (exact match)
    - 'billing:write' (domain:write implies domain:*:create/update/delete)
    - 'billing:read' (if the action is 'read')

    This allows both granular and broad permissions to work together.
    """
    keys = [permission_key]
    parts = permission_key.split(":")

    if len(parts) >= 2:
        domain = parts[0]
        # For 3-part permissions like billing:invoice:create
        if len(parts) == 3:
            action = parts[2]
            # billing:invoice:read -> also accept billing:read
            if action == "read":
                keys.append(f"{domain}:read")
            # billing:invoice:create/update/delete -> also accept billing:write
            elif action in ("create", "update", "delete", "write"):
                keys.append(f"{domain}:write")
        # For 2-part permissions like customer:read
        elif len(parts) == 2:
            action = parts[1]
            # customer:read is already a broad permission
            # customer:create/update/delete -> also accept customer:write (if it exists)
            if action in ("create", "update", "delete"):
                keys.append(f"{domain}:write")

    return keys


def require_permission(permission_key: str):
    def _require_permission(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        subscriber_id = auth["subscriber_id"]
        roles = set(auth.get("roles") or [])
        if "admin" in roles:
            return auth

        # Expand the permission key to include hierarchical matches
        possible_keys = _expand_permission_keys(permission_key)

        # Check if permission is granted via JWT scopes
        scopes = set(auth.get("scopes") or [])
        if scopes & set(possible_keys):
            return auth

        # Find all matching permissions (exact or hierarchical)
        permissions = (
            db.query(Permission)
            .filter(Permission.key.in_(possible_keys))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        if not permissions:
            raise HTTPException(status_code=403, detail="Permission not found")

        permission_ids = [p.id for p in permissions]

        # Check if user has any of the matching permissions via roles
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == subscriber_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )

        # Check if user has any direct permission grants
        has_direct_permission = (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == subscriber_id)
            .filter(SubscriberPermission.permission_id.in_(permission_ids))
            .first()
        )

        if not has_role_permission and not has_direct_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_permission


def require_any_permission(*permission_keys: str):
    """Require user to have at least one of the specified permissions."""
    def _require_any_permission(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        subscriber_id = auth["subscriber_id"]
        roles = set(auth.get("roles") or [])
        if "admin" in roles:
            return auth

        # Expand all permission keys
        all_possible_keys = set()
        for key in permission_keys:
            all_possible_keys.update(_expand_permission_keys(key))

        permissions = (
            db.query(Permission)
            .filter(Permission.key.in_(all_possible_keys))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        if not permissions:
            raise HTTPException(status_code=403, detail="Permission not found")

        permission_ids = [p.id for p in permissions]

        # Check if user has any of the matching permissions via roles
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == subscriber_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )

        # Check if user has any direct permission grants
        has_direct_permission = (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == subscriber_id)
            .filter(SubscriberPermission.permission_id.in_(permission_ids))
            .first()
        )

        if not has_role_permission and not has_direct_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_any_permission


def require_method_permission(
    read_permission_key: str,
    write_permission_key: str,
    read_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS"),
):
    """Require read permission for read methods, write permission otherwise."""
    normalized_read_methods = {method.upper() for method in read_methods}
    require_read = require_permission(read_permission_key)
    require_write = require_permission(write_permission_key)

    def _require_method_permission(
        request: Request,
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        if request.method.upper() in normalized_read_methods:
            return require_read(auth=auth, db=db)
        return require_write(auth=auth, db=db)

    return _require_method_permission
