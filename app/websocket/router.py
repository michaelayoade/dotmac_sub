from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logging import get_logger
from app.services import team_inbox_assignment
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.realtime_platform import build_event
from app.services.realtime_subscriptions import (
    RealtimeSubscriptionError,
    authorize_topic,
)
from app.websocket.auth import (
    accepted_auth_subprotocol,
    authenticate_websocket,
    websocket_auth_session_is_active,
)
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
    await websocket.accept(subprotocol=accepted_auth_subprotocol(websocket))

    # Authenticate
    auth_result = await authenticate_websocket(websocket)
    if not auth_result:
        return
    if auth_result.get("surface") == "fiber_website":
        from app.config import settings

        origin = str(websocket.headers.get("origin") or "").rstrip("/")
        if origin != settings.fiber_chat_allowed_origin:
            await websocket.close(code=4003, reason="Origin not allowed")
            return

    user_id = auth_result["subscriber_id"]
    manager = get_connection_manager()

    # Register connection
    default_topics: tuple[str, ...] = ()
    if auth_result.get("principal_type") == "system_user":
        default_topics = ("audience:staff",)
    await manager.register_connection(user_id, websocket, topics=default_topics)

    try:
        while True:
            data = await websocket.receive_text()
            if not await _connected_session_is_active(auth_result):
                await websocket.close(code=4001, reason="Session is no longer active")
                return
            keep_open = await _handle_client_message(
                user_id, auth_result, websocket, data, manager
            )
            if not keep_open:
                return
    except WebSocketDisconnect:
        logger.debug("websocket_disconnected user_id=%s", user_id)
    except Exception as exc:
        logger.warning("websocket_error user_id=%s error=%s", user_id, exc)
    finally:
        await manager.unregister_connection(user_id, websocket)


async def _handle_client_message(
    user_id: str,
    auth: dict,
    websocket: WebSocket,
    raw_data: str,
    manager,
) -> bool:
    """Process incoming client message."""
    try:
        data = json.loads(raw_data)
        message = InboundMessage(**data)

        if message.type == InboundMessageType.SUBSCRIBE:
            if message.requested_topic:
                topic = _authorized_topic(auth, message.requested_topic)
                manager.subscribe_topic(websocket, topic)

        elif message.type == InboundMessageType.UNSUBSCRIBE:
            if message.requested_topic:
                topic = _authorized_topic(auth, message.requested_topic)
                manager.unsubscribe_topic(websocket, topic)

        elif message.type == InboundMessageType.TYPING:
            if message.requested_topic:
                topic = _authorized_topic(auth, message.requested_topic)
                if not topic.startswith("conversation:"):
                    raise RealtimeSubscriptionError(
                        "typing_not_supported",
                        "Typing events are only supported for conversations",
                    )
                conversation_id = topic.removeprefix("conversation:")
                typing_event = WebSocketEvent(
                    event=EventType.USER_TYPING,
                    data={
                        "user_id": user_id,
                        "agent_name": auth.get("display_name"),
                        "conversation_id": conversation_id,
                        "is_typing": message.data.get("is_typing", True)
                        if message.data
                        else True,
                    },
                )
                await manager.broadcast_to_topic(topic, typing_event)

        elif message.type == InboundMessageType.PING:
            if auth.get("principal_type") == "system_user":
                observed_at = datetime.now(UTC)
                refreshed = await asyncio.to_thread(
                    _refresh_staff_presence,
                    auth,
                    observed_at,
                )
                if not refreshed:
                    await websocket.close(
                        code=4001, reason="Session is no longer active"
                    )
                    return False
            await manager.send_heartbeat(user_id, websocket)

    except RealtimeSubscriptionError as exc:
        rejected = build_event(
            "realtime:connection",
            "realtime.subscription_rejected",
            {"code": exc.code, "message": exc.message},
            refresh_required=False,
        )
        await websocket.send_json(rejected.model_dump(mode="json"))
    except json.JSONDecodeError:
        logger.warning("websocket_invalid_json user_id=%s", user_id)
    except Exception as exc:
        logger.warning("websocket_message_error user_id=%s error=%s", user_id, exc)
    return True


async def _connected_session_is_active(auth: dict) -> bool:
    if auth.get("principal_type") == "chat_widget":
        return True
    try:
        return await asyncio.to_thread(websocket_auth_session_is_active, auth)
    except Exception:
        logger.warning(
            "websocket_session_validation_failed principal=%s",
            auth.get("principal_id"),
            exc_info=True,
        )
        return False


def _refresh_staff_presence(auth: dict, observed_at: datetime) -> bool:
    """Invoke the presence owner from a fresh transport-owned DB session."""

    try:
        system_user_id = UUID(str(auth.get("principal_id") or ""))
        auth_session_id = UUID(str(auth.get("session_id") or ""))
    except ValueError:
        return False
    try:
        with db_session_adapter.owner_command_session() as db:
            outcome = team_inbox_assignment.refresh_agent_presence(
                db,
                command=team_inbox_assignment.AgentPresenceHeartbeatCommand(
                    context=CommandContext.system(
                        actor=f"system-user:{system_user_id}",
                        scope="team-inbox:presence-heartbeat",
                        reason="record authenticated WebSocket Inbox activity",
                    ),
                    system_user_id=system_user_id,
                    auth_session_id=auth_session_id,
                    observed_at=observed_at,
                ),
            )
        return outcome.disposition in {
            team_inbox_assignment.AgentPresenceHeartbeatDisposition.refreshed,
            team_inbox_assignment.AgentPresenceHeartbeatDisposition.explicit_unavailable,
        }
    except Exception:
        logger.warning(
            "websocket_presence_refresh_refused principal=%s session=%s",
            system_user_id,
            auth_session_id,
            exc_info=True,
        )
        return False


def _authorized_topic(auth: dict, requested_topic: str) -> str:
    with db_session_adapter.read_session() as db:
        return authorize_topic(db, auth, requested_topic)
