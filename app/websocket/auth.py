from __future__ import annotations

from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.auth_dependencies import claims_for_principal
from app.services.auth_flow import decode_access_token, validate_active_session
from app.services.db_session_adapter import db_session_adapter
from app.services.team_inbox_widget import decode_widget_token

WEBSOCKET_AUTH_SUBPROTOCOL = "dotmac-auth"


def _validated_access_identity(
    db: Session, token: str
) -> tuple[dict, str, str, str] | None:
    """Decode a JWT and bind it to its current backing session and principal."""

    payload = decode_access_token(db, token)
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    principal_type = str(payload.get("principal_type") or "subscriber")
    session_id = str(payload.get("session_id") or "")
    if not principal_id or not session_id:
        return None
    validated = validate_active_session(db, session_id, principal_id)
    if validated is None:
        return None
    _session, _principal, validated_principal_type = validated
    if str(validated_principal_type) != principal_type:
        return None
    return payload, principal_id, principal_type, session_id


def websocket_auth_session_is_active(auth: dict) -> bool:
    """Re-check a connected JWT principal against its live AuthSession row."""

    if auth.get("principal_type") == "chat_widget":
        return True
    principal_id = str(auth.get("principal_id") or "")
    principal_type = str(auth.get("principal_type") or "")
    session_id = str(auth.get("session_id") or "")
    if not principal_id or not principal_type or not session_id:
        return False
    with db_session_adapter.read_session() as db:
        validated = validate_active_session(db, session_id, principal_id)
        return validated is not None and str(validated[2]) == principal_type


def _requested_subprotocols(websocket: WebSocket) -> tuple[str, ...]:
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def accepted_auth_subprotocol(websocket: WebSocket) -> str | None:
    protocols = _requested_subprotocols(websocket)
    if WEBSOCKET_AUTH_SUBPROTOCOL in protocols:
        return WEBSOCKET_AUTH_SUBPROTOCOL
    return None


def _subprotocol_token(websocket: WebSocket) -> str | None:
    protocols = _requested_subprotocols(websocket)
    try:
        marker = protocols.index(WEBSOCKET_AUTH_SUBPROTOCOL)
    except ValueError:
        return None
    token_index = marker + 1
    return protocols[token_index] if token_index < len(protocols) else None


def _websocket_token(websocket: WebSocket) -> str | None:
    # Browsers cannot set Authorization on a WebSocket handshake. Carry public
    # widget credentials in Sec-WebSocket-Protocol so they do not enter request
    # URLs, browser history, Nginx access logs, or Uvicorn request-line logs.
    # Cookies remain the preferred same-origin staff mechanism. The query
    # fallback supports one rolling-deployment window for older widget clients.
    return (
        _subprotocol_token(websocket)
        or websocket.cookies.get("session_token")
        or websocket.query_params.get("token")
    )


async def authenticate_staff_websocket(websocket: WebSocket) -> dict | None:
    """Authenticate a staff WebSocket and return an ``auth``-shaped dict.

    Shaped like ``require_user_auth``'s return value (principal_id / roles /
    scopes) so services that already take an auth dict — e.g. the workqueue's
    ``principal_from_auth`` — work unchanged over a socket.
    """
    token = _websocket_token(websocket)
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return None

    db = db_session_adapter.create_session()
    try:
        identity = _validated_access_identity(db, token)
        if identity is None:
            await websocket.close(code=4001, reason="Invalid token")
            return None
        payload, principal_id, principal_type, session_id = identity
        roles, scopes = claims_for_principal(db, principal_id, principal_type, payload)
        return {
            "principal_id": principal_id,
            "person_id": principal_id,
            "principal_type": principal_type,
            "session_id": session_id,
            "roles": roles,
            "scopes": scopes,
        }
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    finally:
        db.close()


async def authenticate_websocket(websocket: WebSocket) -> dict | None:
    """
    Authenticate WebSocket connection.

    Extracts JWT from the auth subprotocol or same-origin session cookie, with
    a temporary query-token fallback for rolling-deployment compatibility.
    Returns {subscriber_id, session_id} if valid, None otherwise.
    """
    token = _websocket_token(websocket)

    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return None

    db = db_session_adapter.create_session()
    try:
        try:
            widget_principal = decode_widget_token(db, token)
            return {
                "subscriber_id": f"chat_widget:{widget_principal.session_id}",
                "principal_id": f"chat_widget:{widget_principal.session_id}",
                "principal_type": "chat_widget",
                "session_id": widget_principal.session_id,
                "conversation_id": str(widget_principal.conversation_id),
                "surface": widget_principal.surface,
                "roles": [],
                "scopes": [],
            }
        except Exception:
            pass

        identity = _validated_access_identity(db, token)
        if identity is None:
            await websocket.close(code=4001, reason="Invalid token")
            return None
        payload, subscriber_id, principal_type, session_id = identity
        roles, scopes = claims_for_principal(db, subscriber_id, principal_type, payload)
        display_name = None
        if principal_type == "system_user":
            user = db.get(SystemUser, subscriber_id)
            if user is not None:
                display_name = (
                    user.display_name
                    or f"{user.first_name} {user.last_name}".strip()
                    or user.email
                )
        return {
            "subscriber_id": str(subscriber_id),
            "principal_id": str(subscriber_id),
            "principal_type": principal_type,
            "display_name": display_name,
            "session_id": session_id,
            "roles": roles,
            "scopes": scopes,
        }
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    finally:
        db.close()
