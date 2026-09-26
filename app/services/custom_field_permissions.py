"""Pure permission-key matching shared by Custom Fields adapters and owner."""

from __future__ import annotations


def permission_granted(permission_keys: frozenset[str], permission: str) -> bool:
    """Honor exact, domain wildcard, and administrator grants."""

    if "*" in permission_keys or permission in permission_keys:
        return True
    parts = permission.split(":")
    wildcard_keys = {f"{':'.join(parts[:index])}:*" for index in range(1, len(parts))}
    return bool(wildcard_keys & permission_keys)


__all__ = ["permission_granted"]
