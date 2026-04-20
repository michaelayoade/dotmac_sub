from __future__ import annotations

from fastapi import WebSocket

from app.services.auth_flow import decode_access_token
from app.services.db_session_adapter import db_session_adapter


async def authenticate_websocket(websocket: WebSocket) -> dict | None:
    """
    Authenticate WebSocket connection.

    Extracts JWT from query param (?token=) or cookie (session_token).
    Returns {subscriber_id, session_id} if valid, None otherwise.
    """
    token = websocket.query_params.get("token")

    if not token:
        token = websocket.cookies.get("session_token")

    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return None

    db = db_session_adapter.create_session()
    try:
        payload = decode_access_token(db, token)
        subscriber_id = payload.get("sub")
        session_id = payload.get("session_id")

        if not subscriber_id:
            await websocket.close(code=4001, reason="Invalid token")
            return None

        return {"subscriber_id": subscriber_id, "session_id": session_id}
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    finally:
        db.close()
