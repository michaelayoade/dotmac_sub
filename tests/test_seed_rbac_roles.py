from scripts.seed.seed_rbac import (
    ADMIN_ONLY_PERMISSION_KEYS,
    DEFAULT_ROLES,
    ROLE_PERMISSIONS,
)


def test_technical_support_role_is_seeded_for_subscription_suspension():
    roles = {name for name, _ in DEFAULT_ROLES}

    assert "technical_support" in roles
    assert ROLE_PERMISSIONS["technical_support"] == [
        "customer:read",
        "subscription:read",
        "subscription:suspend",
    ]
    assert not (set(ROLE_PERMISSIONS["technical_support"]) & ADMIN_ONLY_PERMISSION_KEYS)
