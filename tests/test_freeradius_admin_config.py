# tests/test_freeradius_admin_config.py
# Static assertions that the admin-login virtual server config is correct and
# fully isolated from the subscriber RADIUS path.


def _read(p):
    with open(p) as f:
        return f.read()


def test_admin_site_listens_on_dedicated_ports():
    s = _read("config/freeradius/sites-enabled/admin-login")
    assert "1822" in s and "1823" in s
    assert "sql_admin" in s  # uses the admin sql instance, not the subscriber one


def test_admin_sql_targets_admin_tables():
    s = _read("config/freeradius/mods-enabled/sql_admin")
    assert "radcheck_admin" in s
    assert "radreply_admin" in s
    assert "radacct_admin" in s
    assert "radpostauth_admin" in s
    assert "radgroupcheck_admin" in s
    assert "radgroupreply_admin" in s
    assert "radusergroup_admin" in s
    assert "radcheck " not in s.replace(
        "radcheck_admin", ""
    )  # never the subscriber table


def test_admin_sql_has_no_bare_subscriber_tables():
    """After stripping all *_admin occurrences, no bare subscriber table name must remain."""
    import re

    s = _read("config/freeradius/mods-enabled/sql_admin")
    for name in [
        "radcheck",
        "radreply",
        "radacct",
        "radpostauth",
        "radgroupcheck",
        "radgroupreply",
        "radusergroup",
    ]:
        # Remove all _admin-suffixed occurrences first
        stripped = s.replace(f"{name}_admin", "")
        # Then assert the bare name is absent as a whole word
        # (allow trailing space, newline, quote, paren — i.e. not followed by _admin)
        assert not re.search(rf"\b{name}\b", stripped), (
            f"sql_admin still references bare subscriber table '{name}' — "
            "must use '{name}_admin' instead"
        )
