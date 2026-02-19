"""Async request parsing dependencies for sync route handlers."""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import HTTPException, Request
from starlette.datastructures import FormData


async def parse_form_data(request: Request) -> FormData:
    return await request.form()


async def parse_json_body(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object payload is required")
    return payload


def parse_form_data_sync(request: Request) -> FormData:
    """Read form data from sync handlers running in threadpool."""
    return anyio.from_thread.run(request.form)


def parse_json_body_sync(request: Request) -> dict[str, Any]:
    """Read JSON body from sync handlers running in threadpool."""
    payload = anyio.from_thread.run(request.json)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object payload is required")
    return payload
