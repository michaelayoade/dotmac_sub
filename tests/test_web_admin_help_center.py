from __future__ import annotations

from typing import cast

from starlette.requests import Request

from app.web.admin import help_center as help_center_web
from app.web.auth.dependencies import WebAuthInfo


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/admin/help",
            "query_string": b"article=find-customer",
            "headers": [],
            "server": ("testserver", 443),
        }
    )


def _staff_auth() -> WebAuthInfo:
    return cast(
        WebAuthInfo,
        {
            "subscriber_id": "staff-id",
            "principal_id": "staff-id",
            "principal_type": "system_user",
            "session_id": "session-id",
            "roles": [],
            "scopes": [],
            "subscriber": object(),
            "access_expires_at": None,
        },
    )


def test_help_center_loads_permissions_before_selecting_guides(
    monkeypatch,
) -> None:
    request = _request()
    auth = _staff_auth()
    request.state.auth = auth
    captured: dict[str, object] = {}

    def load_customer_permissions(auth_info: dict, _db: object) -> frozenset[str]:
        auth_info["permission_keys"] = frozenset({"customer:read"})
        return auth_info["permission_keys"]

    def capture_template(_name: str, context: dict[str, object]):
        captured.update(context)
        return context

    monkeypatch.setattr(
        help_center_web,
        "load_permission_keys",
        load_customer_permissions,
    )
    monkeypatch.setattr(
        help_center_web.templates,
        "TemplateResponse",
        capture_template,
    )
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {},
    )
    monkeypatch.setattr(
        "app.web.admin.get_sidebar_stats",
        lambda _db: {},
    )

    help_center_web.help_center(
        request=request,
        article="find-customer",
        db=object(),
        auth=auth,
    )

    selected_article = cast(help_center_web.HelpArticle, captured["selected_article"])
    categories = cast(tuple[str, ...], captured["categories"])

    assert selected_article.id == "find-customer"
    assert captured["grouped_articles"]
    assert "Customers" in categories
    assert "Billing" not in categories
