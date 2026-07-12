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


def _authorize_block(s):
    """Return the body of the admin-login `authorize { ... }` section."""
    import re

    m = re.search(r"\bauthorize\s*\{(.*?)\n    \}", s, re.S)
    assert m, "authorize block not found in admin-login"
    return m.group(1)


def test_admin_realm_defines_scoped_clients():
    """Routers must be defined as clients of the admin realm (read_clients=no),
    scoped to the 1822/1823 listeners so the subscriber path is untouched."""
    s = _read("config/freeradius/sites-enabled/admin-login")
    assert "clients admin_clients" in s
    # Both listeners reference the scoped client list.
    assert s.count("clients = admin_clients") >= 2
    # Router management source subnets.
    assert "160.119.124.0/22" in s
    assert "102.220.188.0/22" in s


def test_admin_secret_is_env_ref_not_hardcoded():
    """Public repo: the shared secret must be an env reference, never a literal."""
    import re

    s = _read("config/freeradius/sites-enabled/admin-login")
    assert "secret = $ENV{ADMIN_RADIUS_SECRET}" in s
    for m in re.finditer(r"secret\s*=\s*(\S+)", s):
        assert m.group(1) == "$ENV{ADMIN_RADIUS_SECRET}", (
            f"hardcoded RADIUS secret in admin-login: {m.group(1)!r} — "
            "use $ENV{ADMIN_RADIUS_SECRET}"
        )


def test_compose_fails_fast_when_radius_secrets_are_missing():
    compose = _read("docker-compose.yml")
    assert "RADIUS_DB_PASS=${RADIUS_DB_PASS:?" in compose
    assert "ADMIN_RADIUS_SECRET=${ADMIN_RADIUS_SECRET:?" in compose


def test_admin_authorize_loads_password_before_mschap():
    """MS-CHAPv2 needs Cleartext-Password loaded before mschap runs, else it
    fails with 'mschap: No NT-Password. Cannot perform authentication'."""
    block = _authorize_block(_read("config/freeradius/sites-enabled/admin-login"))
    # Strip comment lines so words like "mschap" in comments don't skew ordering.
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "sql_admin" in code and "mschap" in code
    assert code.index("sql_admin") < code.index("mschap"), (
        "sql_admin must run before mschap in the admin-login authorize section"
    )


def test_admin_selects_mschap_auth_type():
    """RouterOS device login uses MS-CHAPv2; the realm must explicitly set
    Auth-Type := MS-CHAP or the authenticate MS-CHAP handler never matches."""
    import re

    s = _read("config/freeradius/sites-enabled/admin-login")
    assert "MS-CHAP-Challenge" in s
    assert re.search(r"Auth-Type\s*:=\s*MS-CHAP", s), (
        "authorize must select Auth-Type := MS-CHAP for MS-CHAPv2 requests"
    )
