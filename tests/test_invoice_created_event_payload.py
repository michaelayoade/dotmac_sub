"""The invoice.created event payload carries the vars its email template needs.

Without invoice_number / amount / due_date the notification handler suppressed
the email as having unresolved template variables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services import billing_automation


def test_invoice_created_payload_has_template_vars(monkeypatch):
    captured: dict = {}

    def _capture(db, event_type, payload, **_kw):
        captured["payload"] = payload

    monkeypatch.setattr(billing_automation, "emit_event", _capture)

    invoice = SimpleNamespace(
        id="inv-1",
        account_id="acc-1",
        invoice_number="INV-000123",
        status=SimpleNamespace(value="issued"),
        currency="NGN",
        subtotal=Decimal("15000"),
        total=Decimal("15000"),
        billing_period_start=None,
        billing_period_end=None,
        due_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    billing_automation._emit_invoice_created_event(None, invoice, None)

    p = captured["payload"]
    assert p["invoice_number"] == "INV-000123"
    assert p["amount"] == "15000"
    assert p["due_date"] == "2026-07-20"
