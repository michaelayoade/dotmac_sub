"""Outer transaction storage preserves xid8 precision without a schema default."""

from sqlalchemy import Column, FetchedValue, MetaData, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.models.transaction_provenance import OuterTransactionId
from scripts.migrations.outer_transaction_provenance import CreationParentTable


def test_xid8_storage_has_no_ddl_default_and_exact_decimal_precision():
    table = Table(
        "parent",
        MetaData(),
        Column(
            "creation_outer_xid",
            OuterTransactionId(),
            nullable=True,
            server_default=FetchedValue(),
        ),
    )
    pg = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    local = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    assert "creation_outer_xid xid8" in pg
    assert "VARCHAR(20)" in local
    assert "DEFAULT" not in pg and "DEFAULT" not in local
    assert (
        OuterTransactionId().process_result_value(
            18446744073709551615, postgresql.dialect()
        )
        == "18446744073709551615"
    )
    assert OuterTransactionId().process_result_value(None, postgresql.dialect()) is None


def test_creation_parent_enum_is_exact_bounded_set():
    assert {x.value for x in CreationParentTable} == {
        "outage_scope_revisions",
        "outage_incident_ticket_links",
        "support_resolution_requests",
        "completion_replacement_decisions",
        "project_field_completion_applications",
        "completion_criteria_revisions",
        "completion_submissions",
        "completion_review_decisions",
    }


def test_known_extension_accepts_only_nullable_native_xid8_without_generation():
    from unittest.mock import Mock

    import pytest

    from scripts.migrations.outer_transaction_provenance import (
        verify_optional_outer_transaction_column,
    )

    bind = Mock()
    bind.execute.return_value.first.return_value = (True, False, "", "", False)
    assert verify_optional_outer_transaction_column(
        bind, table_name=CreationParentTable.support_request
    )
    for invalid in (
        (False, False, "", "", False),
        (True, True, "", "", False),
        (True, False, "a", "", False),
        (True, False, "", "s", False),
        (True, False, "", "", True),
    ):
        bind.execute.return_value.first.return_value = invalid
        with pytest.raises(
            RuntimeError, match="Invalid outer transaction provenance column"
        ):
            verify_optional_outer_transaction_column(
                bind, table_name=CreationParentTable.support_request
            )
