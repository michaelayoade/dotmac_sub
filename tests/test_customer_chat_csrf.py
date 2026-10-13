"""Cookie mutations require CSRF while native bearer authentication remains usable."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.csrf import require_browser_csrf_header


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"cookie", b"csrf_token=a")],
        [(b"cookie", b"csrf_token=a"), (b"x-csrf-token", b"b")],
    ],
)
def test_cookie_mutations_reject_missing_or_mismatched_token(
    headers: list[tuple[bytes, bytes]],
) -> None:
    with pytest.raises(HTTPException) as exc:
        require_browser_csrf_header(
            Request({"type": "http", "method": "POST", "headers": headers})
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        [(b"authorization", b"Bearer test-token")],
        [(b"cookie", b"csrf_token=a"), (b"x-csrf-token", b"a")],
    ],
)
def test_bearer_or_matching_cookie_header_pass(
    headers: list[tuple[bytes, bytes]],
) -> None:
    require_browser_csrf_header(
        Request({"type": "http", "method": "POST", "headers": headers})
    )


def test_chat_route_declares_cookie_csrf_guard() -> None:
    from app.api.me import router

    route = next(r for r in router.routes if r.path.endswith("/chat/messages"))
    assert any(
        d.call is require_browser_csrf_header for d in route.dependant.dependencies
    )
