"""RBAC: customer.account_recovery permissions exist and are admin-only."""

from __future__ import annotations

from scripts.seed.seed_rbac import (
    ADMIN_ONLY_PERMISSION_KEYS,
    DEFAULT_PERMISSIONS,
    ROLE_PERMISSIONS,
)

_KEYS = (
    "customer:account_recovery:read",
    "customer:account_recovery:restore",
    "customer:account_recovery:rebaseline",
)


def test_all_three_permissions_are_registered() -> None:
    registered = {key for key, _ in DEFAULT_PERMISSIONS}
    for key in _KEYS:
        assert key in registered, f"{key} missing from DEFAULT_PERMISSIONS"


def test_all_three_permissions_are_admin_only() -> None:
    for key in _KEYS:
        assert key in ADMIN_ONLY_PERMISSION_KEYS


def test_permission_key_grammar_uses_underscores_not_hyphens() -> None:
    for key in _KEYS:
        assert "-" not in key
        assert "_" in key or ":" in key


def test_no_non_admin_role_definition_lists_these_keys() -> None:
    """Seeded ONLY to admin: no other role's explicit permission list in
    `ROLE_PERMISSIONS` may include any of the three keys.

    `admin`'s entry is `[perm for perm, _ in DEFAULT_PERMISSIONS]` — every
    permission, including these three — so it is excluded here on purpose;
    every OTHER role must not name any of them.
    """
    for role_name, permissions in ROLE_PERMISSIONS.items():
        if role_name == "admin":
            continue
        overlap = set(permissions) & set(_KEYS)
        assert not overlap, f"role {role_name!r} must not list {overlap}"
