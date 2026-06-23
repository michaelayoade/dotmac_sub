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
