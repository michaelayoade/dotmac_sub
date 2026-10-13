"""Frozen 681 descriptors use PostgreSQL format_type spellings exactly."""

import pytest
from sqlalchemy.dialects import postgresql

from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.shared_outage_completion import (
    CompletionReplacementOutageProof,
    SupportResolutionSharedIncident,
    SupportResolutionSharedWorkScope,
)
from scripts.migrations.shared_outage_schema_contract import SHARED_OUTAGE_TABLES


@pytest.mark.parametrize(
    "model",
    (
        OutageComplaintCorrelationReceipt,
        SupportResolutionSharedIncident,
        SupportResolutionSharedWorkScope,
        CompletionReplacementOutageProof,
    ),
)
def test_all_frozen_columns_match_exact_postgres_type_and_nullability(model):
    # Test-only metadata comparison: the migration itself retains frozen literals.
    expected = next(x for x in SHARED_OUTAGE_TABLES if x.name == model.__tablename__)
    columns = tuple(
        (
            column.name,
            column.type.compile(dialect=postgresql.dialect())
            .lower()
            .replace("varchar", "character varying"),
            not column.nullable,
        )
        for column in model.__table__.columns
    )
    assert expected.columns == columns
    assert all(type_name == type_name.lower() for _, type_name, _ in expected.columns)


def test_receipt_reason_uses_exact_format_type_text_spelling():
    receipt = next(
        x
        for x in SHARED_OUTAGE_TABLES
        if x.name == "outage_complaint_correlation_receipts"
    )
    assert next(x for x in receipt.columns if x[0] == "reason") == (
        "reason",
        "text",
        True,
    )
