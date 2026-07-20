"""Derive RouterOS privilege tier from a staff member's existing RBAC."""

from __future__ import annotations


def derive_router_tier(roles: set[str], permissions: set[str]) -> str | None:
    """Map existing roles/permissions to a RouterOS group.

    full  : 'admin' role, '*' wildcard, or 'router:admin'
    write : 'router:write' or 'router:push_config'
    read  : 'router:read'
    None  : not eligible for device login

    Router device-login intentionally mirrors the app's router RBAC tiers:
    read/write staff get constrained RouterOS groups, while router admins get
    full. Users without router permissions are not projected to RADIUS.
    """
    if "admin" in roles or "*" in permissions or "router:admin" in permissions:
        return "full"
    if {"router:write", "router:push_config"} & permissions:
        return "write"
    if "router:read" in permissions:
        return "read"
    return None
