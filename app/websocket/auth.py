from __future__ import annotations

from fastapi import WebSocket, status

from app.db import SessionLocal
from app.services.auth_flow import decode_access_token


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

    db = SessionLocal()
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
