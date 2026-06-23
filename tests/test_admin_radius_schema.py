"""Tests for the admin RADIUS schema DDL."""

import re


def _load_ddl():
    with open("config/freeradius/sql/admin_schema.sql") as f:
        return f.read()


def test_admin_tables_have_required_columns():
    ddl = _load_ddl()
    for tbl in ("radcheck_admin", "radreply_admin", "radacct_admin"):
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
