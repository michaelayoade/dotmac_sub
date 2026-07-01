"""Derive RouterOS privilege tier from a staff member's existing RBAC."""

from __future__ import annotations


def derive_router_tier(roles: set[str], permissions: set[str]) -> str | None:
    """Map existing roles/permissions to a RouterOS group.

    full  : 'admin' role, '*' wildcard, or 'router:admin'
    None  : not eligible for device login
    """
    if "admin" in roles or "*" in permissions or "router:admin" in permissions:
        return "full"
    return None
