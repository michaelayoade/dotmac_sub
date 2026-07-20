"""Tests for the admin RADIUS schema DDL."""

import re


def _load_ddl():
    with open("config/freeradius/sql/admin_schema.sql") as f:
        return f.read()


def test_admin_tables_have_required_columns():
    ddl = _load_ddl()
    for tbl in (
        "radcheck_admin",
        "radreply_admin",
        "radacct_admin",
        "radpostauth_admin",
        "radgroupcheck_admin",
        "radgroupreply_admin",
        "radusergroup_admin",
    ):
        assert re.search(rf"create table[^;]*{tbl}", ddl, re.I), f"{tbl} missing"
    assert "username" in ddl and "attribute" in ddl and "value" in ddl
    assert "acctsessionid" in ddl and "nasipaddress" in ddl


def _radacct_admin_block(ddl: str) -> str:
    """Return just the radacct_admin CREATE TABLE block."""
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS radacct_admin\s*\((.+?)\);",
        ddl,
        re.I | re.S,
    )
    assert m, "radacct_admin CREATE TABLE block not found"
    return m.group(1)


def test_radacct_admin_has_full_accounting_columns():
    """radacct_admin must mirror all columns that FreeRADIUS 3.x writes."""
    ddl = _load_ddl()
    block = _radacct_admin_block(ddl)

    required_columns = [
        "acctinputoctets",
        "acctoutputoctets",
        "framedipaddress",
        "calledstationid",
        "nasporttype",
        # Additional columns from the canonical FreeRADIUS radacct schema
        "acctuniqueid",
        "acctinterval",
        "acctauthentic",
        "connectinfo_start",
        "connectinfo_stop",
        "acctterminatecause",
        "framedprotocol",
        "framedipv6address",
        "framedipv6prefix",
        "framedinterfaceid",
        "delegatedipv6prefix",
        "class",
    ]

    for col in required_columns:
        assert col in block, (
            f"radacct_admin is missing column '{col}' — "
            "FreeRADIUS accounting INSERTs/UPDATEs will fail without it"
        )


def test_radacct_admin_accepts_full_length_nas_port_id():
    block = _radacct_admin_block(_load_ddl())
    assert re.search(r"nasportid\s+VARCHAR\(253\)", block, re.I)


def test_customer_radacct_and_upgrade_accept_full_length_nas_port_id():
    with open("config/freeradius/schema.sql") as schema_file:
        schema = schema_file.read()
    assert re.search(r"nasportid\s+VARCHAR\(253\)", schema, re.I)

    with open(
        "config/freeradius/upgrade_003_radacct_nasportid_capacity.sql"
    ) as upgrade_file:
        upgrade = upgrade_file.read()
    assert "ALTER COLUMN nasportid TYPE VARCHAR(253)" in upgrade
    assert "radacct_admin" in upgrade


def test_app_session_models_accept_full_length_nas_port_id():
    from app.models.radius_active_session import RadiusActiveSession
    from app.models.usage import RadiusAccountingSession

    assert RadiusAccountingSession.__table__.c.nas_port_id.type.length == 253
    assert RadiusActiveSession.__table__.c.nas_port_id.type.length == 253
