from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logging import get_logger
from app.websocket.auth import authenticate_websocket
from app.websocket.events import (
    EventType,
    InboundMessage,
    InboundMessageType,
    WebSocketEvent,
)
from app.websocket.manager import get_connection_manager

logger = get_logger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/inbox")
async def inbox_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time inbox updates.

    Client actions:
    - subscribe: Subscribe to conversation updates
    - unsubscribe: Unsubscribe from conversation
    - typing: Broadcast typing indicator
    - ping: Keep-alive ping
    """
    await websocket.accept()

    # Authenticate
    auth_result = await authenticate_websocket(websocket)
    if not auth_result:
        return

    user_id = auth_result["subscriber_id"]
    manager = get_connection_manager()

    # Register connection
    await manager.register_connection(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            await _handle_client_message(user_id, websocket, data, manager)
    except WebSocketDisconnect:
        logger.debug("websocket_disconnected user_id=%s", user_id)
    except Exception as exc:
        logger.warning("websocket_error user_id=%s error=%s", user_id, exc)
    finally:
        await manager.unregister_connection(user_id, websocket)


async def _handle_client_message(
    user_id: str, websocket: WebSocket, raw_data: str, manager
):
    """Process incoming client message."""
    try:
        data = json.loads(raw_data)
        message = InboundMessage(**data)

        if message.type == InboundMessageType.SUBSCRIBE:
            if message.conversation_id:
                await manager.subscribe_conversation(user_id, message.conversation_id)

        elif message.type == InboundMessageType.UNSUBSCRIBE:
            if message.conversation_id:
                await manager.unsubscribe_conversation(
                    user_id, message.conversation_id
                )

        elif message.type == InboundMessageType.TYPING:
            if message.conversation_id:
                typing_event = WebSocketEvent(
                    event=EventType.USER_TYPING,
                    data={
                        "user_id": user_id,
                        "conversation_id": message.conversation_id,
                        "is_typing": message.data.get("is_typing", True)
                        if message.data
                        else True,
                    },
                )
                await manager.broadcast_to_conversation(
                    message.conversation_id, typing_event
                )

        elif message.type == InboundMessageType.PING:
            await manager.send_heartbeat(user_id, websocket)

    except json.JSONDecodeError:
        logger.warning("websocket_invalid_json user_id=%s", user_id)
    except Exception as exc:
        logger.warning("websocket_message_error user_id=%s error=%s", user_id, exc)
