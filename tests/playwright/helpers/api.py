from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from playwright.sync_api import APIRequestContext, APIResponse

JSON_HEADERS = {"Content-Type": "application/json"}
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def api_get(
    context: APIRequestContext,
    url: str,
    headers: Mapping[str, str] | None = None,
) -> APIResponse:
    return context.get(url, headers=dict(headers or {}))


def api_post_json(
    context: APIRequestContext,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> APIResponse:
    merged = dict(JSON_HEADERS)
    if headers:
        merged.update(headers)
    return context.post(url, data=json.dumps(payload), headers=merged)


def api_post_form(
    context: APIRequestContext,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    follow_redirects: bool = False,
) -> APIResponse:
    merged = dict(headers or {})
    form_payload: dict[str, str | float | bool] = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            form_payload[key] = value
        elif isinstance(value, (float, str)):
            form_payload[key] = value
        elif isinstance(value, int):
            # Playwright's stubs don't accept int here; encode as str.
            form_payload[key] = str(value)
        else:
            form_payload[key] = str(value)
    # Use form= parameter for form-encoded data (Playwright handles Content-Type)
    # Don't follow redirects by default for API tests (use max_redirects=0)
    max_redirects = 0 if not follow_redirects else 30
    return context.post(url, form=form_payload, headers=merged, max_redirects=max_redirects)
