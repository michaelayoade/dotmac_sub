"""Shared helper functions for admin system web routes."""

from __future__ import annotations

import json

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import IntegrityError


def is_admin_request(request) -> bool:
    auth = getattr(request.state, "auth", {}) or {}
    roles = auth.get("roles") or []
    return any(str(role).lower() == "admin" for role in roles)


def form_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def linked_user_labels(db, subscriber_id) -> list[str]:
    """Check for linked data that would prevent user deletion."""
    return []


def blocked_delete_response(request, linked: list[str], detail: str | None = None):
    if detail is None:
        if linked:
            detail = f"Cannot delete user. Linked to: {', '.join(linked)}."
        else:
            detail = "Cannot delete user. Linked records exist."
    if request.headers.get("HX-Request"):
        trigger = {
            "showToast": {
                "type": "error",
                "title": "Delete blocked",
                "message": detail,
            }
        }
        return Response(status_code=409, headers={"HX-Trigger": json.dumps(trigger)})
    raise HTTPException(status_code=409, detail=detail)


def humanize_integrity_error(exc: IntegrityError) -> str:
    raw = str(getattr(exc, "orig", exc) or "").lower()
    if "user_credentials" in raw and "username" in raw and "already exists" in raw:
        return "Username already exists. Choose a different username or email."
    if "people" in raw and "email" in raw and "already exists" in raw:
        return "Email already exists. Use a different email address."
    if "unique" in raw and "username" in raw:
        return "Username already exists. Choose a different username or email."
    if "unique" in raw and "email" in raw:
        return "Email already exists. Use a different email address."
    return "Could not save this user because the record already exists."


def error_banner(message: str, status_code: int = 409) -> HTMLResponse:
    return HTMLResponse(
        '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">'
        f"{message}"
        "</div>",
        status_code=status_code,
    )
