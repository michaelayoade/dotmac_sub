import logging
import os
import time
import uuid

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

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
    if route and hasattr(route, "path"):
        return route.path
    return request.url.path


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        token = _extract_bearer_token(request)
        actor_id = getattr(request.state, "actor_id", None) or _extract_actor_id_from_jwt(
            token
        )
        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
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
        response.headers["x-request-id"] = request_id
        return response
