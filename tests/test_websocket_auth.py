from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from starlette.websockets import WebSocket

from app.websocket import auth as websocket_auth
from app.websocket.auth import (
    WEBSOCKET_AUTH_SUBPROTOCOL,
    _validated_access_identity,
    _websocket_token,
    accepted_auth_subprotocol,
)


def _socket(*, protocols: str = "", query: bytes = b"") -> WebSocket:
    headers = []
    if protocols:
        headers.append((b"sec-websocket-protocol", protocols.encode("ascii")))
    return WebSocket(
        {
            "type": "websocket",
            "path": "/ws/inbox",
            "query_string": query,
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "scheme": "ws",
        },
        receive=lambda: None,
        send=lambda _message: None,
    )


def test_websocket_token_prefers_non_url_auth_subprotocol() -> None:
    websocket = _socket(
        protocols="dotmac-auth, header.payload.signature",
        query=b"token=legacy-query-token",
    )

    assert _websocket_token(websocket) == "header.payload.signature"
    assert accepted_auth_subprotocol(websocket) == WEBSOCKET_AUTH_SUBPROTOCOL


def test_websocket_query_token_remains_a_rolling_deployment_fallback() -> None:
    websocket = _socket(query=b"token=legacy-query-token")

    assert _websocket_token(websocket) == "legacy-query-token"
    assert accepted_auth_subprotocol(websocket) is None


def test_nginx_disables_websocket_access_log() -> None:
    nginx = Path("deploy/nginx/selfcare.dotmac.io").read_text(encoding="utf-8")
    websocket_location = nginx.split("location /ws {", 1)[1].split("}", 1)[0]

    assert "access_log off;" in websocket_location


def test_access_identity_requires_live_session_bound_to_same_principal(
    monkeypatch,
) -> None:
    payload = {
        "principal_id": "staff-1",
        "principal_type": "system_user",
        "session_id": "session-1",
    }
    monkeypatch.setattr(websocket_auth, "decode_access_token", lambda *_args: payload)
    monkeypatch.setattr(
        websocket_auth,
        "validate_active_session",
        lambda *_args: (object(), object(), "system_user"),
    )

    assert _validated_access_identity(object(), "token") == (
        payload,
        "staff-1",
        "system_user",
        "session-1",
    )

    monkeypatch.setattr(
        websocket_auth,
        "validate_active_session",
        lambda *_args: (object(), object(), "subscriber"),
    )
    assert _validated_access_identity(object(), "token") is None


def test_access_identity_refuses_missing_or_revoked_backing_session(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        websocket_auth,
        "decode_access_token",
        lambda *_args: {
            "principal_id": "staff-1",
            "principal_type": "system_user",
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(websocket_auth, "validate_active_session", lambda *_args: None)

    assert _validated_access_identity(object(), "token") is None


def test_connected_socket_recheck_refuses_revoked_session(monkeypatch) -> None:
    @contextmanager
    def read_session():
        yield object()

    monkeypatch.setattr(
        websocket_auth.db_session_adapter,
        "read_session",
        read_session,
    )
    monkeypatch.setattr(websocket_auth, "validate_active_session", lambda *_args: None)

    assert not websocket_auth.websocket_auth_session_is_active(
        {
            "principal_id": "staff-1",
            "principal_type": "system_user",
            "session_id": "revoked-session",
        }
    )
