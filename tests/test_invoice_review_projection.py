"""Shared review carries authoritative amounts and hidden source identities."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.billing import TaxApplication
from app.services import future_invoice_authoring as owner
from app.services import web_billing_invoice_forms as projection


def test_catalogue_review_rebuilds_submitted_amounts_from_owner_preview(
    db_session, subscriber, monkeypatch
):
    item_query = owner.PreviewFutureInvoiceLineQuery(
        subscriber.id,
        "NGN",
        owner.CatalogueItemKind.add_on,
        uuid4(),
        uuid4(),
        datetime.now(UTC),
        quantity=Decimal("2"),
    )
    selection = owner.FutureInvoiceLineSelection(item_query, "owner-fingerprint")
    line = owner.FutureInvoiceLinePreview(
        description="Authoritative router",
        quantity=Decimal("2"),
        unit_price=Decimal("100"),
        tax_rate_id=None,
        tax_application=TaxApplication.exclusive,
        tax_total=Decimal("15"),
        total=Decimal("215"),
        subscription_id=None,
        starts_at=None,
        ends_at=None,
        period_kind=owner.FutureInvoicePeriodKind.one_time,
        selection=selection,
    )
    monkeypatch.setattr(
        owner, "confirm_future_invoice_line", lambda *args, **kwargs: line
    )
    client = [
        {
            "description": "Forged text",
            "quantity": "99",
            "unitPrice": "1",
            "catalogue_selection": owner.selection_to_wire(selection),
        }
    ]
    review = projection.build_future_invoice_review(
        db_session,
        query=projection.FutureInvoiceReviewRequest(
            account_id=subscriber.id,
            currency="NGN",
            line_items_json=json.dumps(client),
            draft_idempotency_key="stable-draft",
            action_url="/admin/billing/invoices/new",
            due_at="2030-01-10",
        ),
    )
    values = {hidden.key: hidden.value for hidden in review.action.hidden_values}
    submitted = json.loads(values["line_items_json"])
    assert submitted[0]["quantity"] == "2"
    assert submitted[0]["unitPrice"] == "100"
    assert submitted[0]["description"] == "Authoritative router"
    assert submitted[0]["catalogue_selection"]["fingerprint"] == "owner-fingerprint"
    assert review.total_display == "₦215.00"
    assert review.action.confirmation is not None
    assert values["draft_idempotency_key"] == "stable-draft"
