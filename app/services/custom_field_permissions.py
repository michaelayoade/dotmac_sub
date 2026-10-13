"""Pure permission-key matching shared by Custom Fields adapters and owner."""

from __future__ import annotations

from app.services.permission_matching import permission_keys_allow


def permission_granted(permission_keys: frozenset[str], permission: str) -> bool:
    """Honor exact, domain wildcard, and administrator grants."""

    return permission_keys_allow(permission_keys, permission)


__all__ = ["permission_granted"]
