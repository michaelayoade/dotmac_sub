from app.services.device_login import derive_router_tier

def test_admin_role_is_full():
    assert derive_router_tier({"admin"}, set()) == "full"
def test_router_admin_perm_is_full():
    assert derive_router_tier(set(), {"router:admin"}) == "full"
def test_wildcard_is_full():
    assert derive_router_tier(set(), {"*"}) == "full"
def test_write_perms():
    assert derive_router_tier(set(), {"router:write"}) == "write"
    assert derive_router_tier(set(), {"router:push_config"}) == "write"
def test_read_perm():
    assert derive_router_tier(set(), {"router:read"}) == "read"
def test_ineligible():
    assert derive_router_tier({"support"}, {"customer:read"}) is None
def test_full_beats_write_beats_read():
    assert derive_router_tier(set(), {"router:admin", "router:write", "router:read"}) == "full"
