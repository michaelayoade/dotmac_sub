"""Request metadata helpers.

Intentionally free of app-internal imports so both the API layer (``main``) and
the service layer (``auth_flow``) can use it without an import cycle.
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
    """Best-effort real client IP.

    Behind our reverse proxy the socket peer is the proxy itself (e.g. a Docker
    bridge gateway like ``172.20.255.1``), so prefer the left-most
    ``X-Forwarded-For`` entry — the original client — when present. Falls back to
    the socket peer. Mirrors the login rate-limiter so session rows and rate
    limits agree on "who" the caller is.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
