"""Invoice/credit-note line ``metadata`` must accept the ORM's JSONB dicts.

The ORM stores ``metadata_`` as JSONB (dicts like the subscription line
context), but the schemas declared it ``str | None`` — so ``InvoiceRead``
serialization failed for every invoice with line metadata and
``GET /api/v1/invoices`` returned 500 for all API consumers (the ERP AR
sync first hit this; the staff web UI doesn't use the route).
"""

import uuid

from app.schemas.billing import (
    CreditNoteLineBase,
    CreditNoteLineUpdate,
    InvoiceLineBase,
    InvoiceLineUpdate,
)

_DICT_META = {
    "kind": "base_subscription",
    "period_start": "2026-06-06T00:00:00+00:00",
    "period_end": "2026-07-06T00:00:00+00:00",
}


def test_invoice_line_metadata_accepts_dict_and_str():
    base = {"invoice_id": uuid.uuid4(), "description": "Internet service"}
    assert InvoiceLineBase(**base, metadata_=_DICT_META).metadata_ == _DICT_META
    assert InvoiceLineBase(**base, metadata_='{"k": 1}').metadata_ == '{"k": 1}'
    assert InvoiceLineBase(**base).metadata_ is None
    assert InvoiceLineUpdate(metadata_=_DICT_META).metadata_ == _DICT_META


def test_credit_note_line_metadata_accepts_dict_and_str():
    base = {"credit_note_id": uuid.uuid4(), "description": "Service credit"}
    assert CreditNoteLineBase(**base, metadata_=_DICT_META).metadata_ == _DICT_META
    assert CreditNoteLineUpdate(metadata_=_DICT_META).metadata_ == _DICT_META


def test_metadata_serializes_under_public_alias():
    line = InvoiceLineBase(
        invoice_id=uuid.uuid4(), description="x", metadata_=_DICT_META
    )
    dumped = line.model_dump(by_alias=True)
    assert dumped["metadata"] == _DICT_META
    assert "metadata_" not in dumped
