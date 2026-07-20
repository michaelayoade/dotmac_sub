from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.config import settings
from app.logging import get_logger
from app.services.realtime_platform import (
    REDIS_CHANNEL_PREFIX,
    RealtimeEvent,
    build_event,
    parse_event,
    principal_topic,
    publish_event,
    redis_channel,
)
from app.websocket.events import EventType, WebSocketEvent

logger = get_logger(__name__)

REDIS_URL = settings.redis_url
CHANNEL_PREFIX = REDIS_CHANNEL_PREFIX


def _mask_redis_url(url: str) -> str:
    """Mask the password segment of a Redis URL for safe logging."""
    parsed = urlsplit(url)
    if "@" not in parsed.netloc:
        return url

    userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
    if ":" not in userinfo:
        return url

    username, _separator, _password = userinfo.partition(":")
    masked_netloc = f"{username}:***@{hostinfo}"
    return urlunsplit(parsed._replace(netloc=masked_netloc))


class ConnectionManager:
    """WebSocket projection adapter over the shared real-time broker.

    Subscriptions are per socket, not per principal. This prevents a user's
    inbox and workqueue sockets from receiving each other's topic streams.
    """

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {}
        self._subscriptions: dict[str, set[WebSocket]] = {}
        self._socket_owners: dict[WebSocket, str] = {}
        self._redis_client = None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._running = False

    async def connect(self):
        """Initialize the Redis subscriber used for cross-instance fan-out."""
        try:
            import redis.asyncio as aioredis

            self._redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
            self._pubsub = self._redis_client.pubsub()
            await self._pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
            self._running = True
            self._listener_task = asyncio.create_task(self._redis_listener())
            logger.info(
                "websocket_manager_connected redis=%s", _mask_redis_url(REDIS_URL)
            )
        except Exception as exc:
            logger.warning("websocket_manager_redis_failed error=%s", exc)

    async def disconnect(self):
        """Cleanup Redis resources and stop the listener."""
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.punsubscribe()
            await self._pubsub.aclose()
        if self._redis_client:
            await self._redis_client.aclose()
        logger.info("websocket_manager_disconnected")

    async def _redis_listener(self):
        try:
            while self._running:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message["type"] == "pmessage":
                    await self._handle_redis_message(
                        str(message["channel"]), message["data"]
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("websocket_redis_listener_error error=%s", exc)
        finally:
            self._running = False

    async def _handle_redis_message(self, channel: str, data: str | bytes):
        """Reject malformed or channel/topic-confused broker messages."""
        try:
            event = parse_event(data)
            if channel != redis_channel(event.topic):
                logger.warning(
                    "websocket_channel_topic_mismatch channel=%s topic=%s",
                    channel,
                    event.topic,
                )
                return
            await self._dispatch_to_subscribers(event)
        except Exception as exc:
            logger.warning("websocket_redis_message_error error=%s", exc)

    async def _dispatch_to_subscribers(self, event: RealtimeEvent):
        for websocket in list(self._subscriptions.get(event.topic, set())):
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json(event.model_dump(mode="json"))
            except Exception:
                self._remove_connection(websocket)

    async def register_connection(
        self,
        user_id: str,
        websocket: WebSocket,
        *,
        topics: tuple[str, ...] = (),
        ready_data: dict | None = None,
    ) -> None:
        self._connections.setdefault(user_id, set()).add(websocket)
        self._socket_owners[websocket] = user_id
        self.subscribe_topic(websocket, principal_topic(user_id))
        for topic in topics:
            self.subscribe_topic(websocket, topic)
        logger.debug("websocket_registered user_id=%s", user_id)

        data = {"user_id": user_id, "status": "connected", **(ready_data or {})}
        ack = build_event(
            "realtime:connection",
            EventType.CONNECTION_ACK,
            data,
            refresh_required=True,
        )
        await websocket.send_json(ack.model_dump(mode="json"))

    async def unregister_connection(self, user_id: str, websocket: WebSocket):
        del user_id  # ownership is recorded at registration
        self._remove_connection(websocket)

    def _remove_connection(self, websocket: WebSocket):
        user_id = self._socket_owners.pop(websocket, None)
        if user_id is not None:
            connections = self._connections.get(user_id, set())
            connections.discard(websocket)
            if not connections:
                self._connections.pop(user_id, None)

        for topic in list(self._subscriptions):
            sockets = self._subscriptions[topic]
            sockets.discard(websocket)
            if not sockets:
                del self._subscriptions[topic]
        logger.debug("websocket_unregistered user_id=%s", user_id)

    def subscribe_topic(self, websocket: WebSocket, topic: str):
        if websocket not in self._socket_owners:
            raise ValueError("WebSocket must be registered before subscribing")
        self._subscriptions.setdefault(topic, set()).add(websocket)
        logger.debug("websocket_subscribed topic=%s", topic)

    def unsubscribe_topic(self, websocket: WebSocket, topic: str):
        sockets = self._subscriptions.get(topic)
        if sockets is not None:
            sockets.discard(websocket)
            if not sockets:
                del self._subscriptions[topic]
        logger.debug("websocket_unsubscribed topic=%s", topic)

    async def broadcast_to_topic(self, topic: str, event: WebSocketEvent):
        realtime_event = build_event(
            topic,
            event.event,
            event.data,
            refresh_required=True,
            timestamp=event.timestamp,
        )
        published = await asyncio.to_thread(publish_event, realtime_event)
        # With a running subscriber Redis echoes the event exactly once. When
        # Redis or this listener is unavailable, preserve same-instance UX.
        if not published or not self._running:
            await self._dispatch_to_subscribers(realtime_event)

    async def broadcast_to_conversation(
        self, conversation_id: str, event: WebSocketEvent
    ):
        from app.services.realtime_platform import conversation_topic

        await self.broadcast_to_topic(conversation_topic(conversation_id), event)

    async def broadcast_to_user(self, user_id: str, event: WebSocketEvent):
        await self.broadcast_to_topic(principal_topic(user_id), event)

    async def send_heartbeat(self, user_id: str, websocket: WebSocket):
        heartbeat = build_event(
            "realtime:connection",
            EventType.HEARTBEAT,
            {"status": "ok", "user_id": user_id},
            refresh_required=False,
        )
        try:
            await websocket.send_json(heartbeat.model_dump(mode="json"))
        except Exception:
            self._remove_connection(websocket)


_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
