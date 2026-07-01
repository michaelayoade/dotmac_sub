from app.services.device_login import derive_router_tier


def test_admin_role_is_full():
    assert derive_router_tier({"admin"}, set()) == "full"


def test_router_admin_perm_is_full():
    assert derive_router_tier(set(), {"router:admin"}) == "full"


def test_wildcard_is_full():
    assert derive_router_tier(set(), {"*"}) == "full"


def test_write_perms_are_not_device_login_eligible():
    assert derive_router_tier(set(), {"router:write"}) is None
    assert derive_router_tier(set(), {"router:push_config"}) is None


def test_read_perm_is_not_device_login_eligible():
    assert derive_router_tier(set(), {"router:read"}) is None


def test_ineligible():
    assert derive_router_tier({"support"}, {"customer:read"}) is None


def test_full_beats_write_beats_read():
    assert (
        derive_router_tier(set(), {"router:admin", "router:write", "router:read"})
        == "full"
    )
