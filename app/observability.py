import logging
import os
import time
import uuid

from jose import JWTError, jwt
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics import REQUEST_COUNT, REQUEST_ERRORS, REQUEST_LATENCY

logger = logging.getLogger(__name__)


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _jwt_secret() -> str | None:
    secret = os.getenv("JWT_SECRET")
    if secret:
        return secret
    return None


def _jwt_algorithm() -> str:
    return os.getenv("JWT_ALGORITHM", "HS256")


def _extract_actor_id_from_jwt(token: str | None) -> str | None:
    if not token:
        return None
    secret = _jwt_secret()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[_jwt_algorithm()])
    except JWTError:
        return None
    subject = payload.get("sub")
    if subject:
        return str(subject)
    return None


def _request_path(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            return path
    return request.url.path


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        token = _extract_bearer_token(request)
        actor_id = scope["state"].get("actor_id") or _extract_actor_id_from_jwt(token)
        start = time.monotonic()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000.0
            path = _request_path(request)
            REQUEST_COUNT.labels(request.method, path, str(status_code)).inc()
            REQUEST_LATENCY.labels(request.method, path, str(status_code)).observe(
                duration_ms / 1000.0
            )
            REQUEST_ERRORS.labels(request.method, path, str(status_code)).inc()
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "actor_id": actor_id,
                    "path": path,
                    "method": request.method,
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise

        duration_ms = (time.monotonic() - start) * 1000.0
        path = _request_path(request)
        REQUEST_COUNT.labels(request.method, path, str(status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path, str(status_code)).observe(
            duration_ms / 1000.0
        )
        if status_code >= 500:
            REQUEST_ERRORS.labels(request.method, path, str(status_code)).inc()
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "actor_id": actor_id,
                "path": path,
                "method": request.method,
                "status": status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
