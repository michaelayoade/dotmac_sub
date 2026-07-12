"""Read-only CRM quote mirror: reconcile, read, and inbound webhooks."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.subscriber import Subscriber
from app.services import quotes_mirror


def _subscriber(db, crm_id: uuid.UUID | None = None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _crm_quote(**kw):
    item = {
        "id": "q1",
        "status": "draft",
        "currency": "NGN",
        "total": "75000.00",
        "deposit_percent": 50,
        "deposit_amount": "37500.00",
        "deposit_paid": False,
        "feasibility": {
            "coverage": "covered",
            "feasible": True,
            "distance_meters": 800.0,
        },
        "estimate_provisional": False,
        "address": "12 Test St, Wuse",
        "latitude": 9.07,
        "longitude": 7.49,
        "line_items": [
            {"description": "Fiber installation (base)", "unit_price": "50000.00"}
        ],
        "created_at": "2026-06-29T10:00:00+00:00",
    }
    item.update(kw)
    return item


def _crm_resp(**kw):
    return {"quotes": [_crm_quote(**kw)], "total": 1}


def test_reconcile_upserts_and_marks_synced(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_quotes.return_value = _crm_resp()
    with (
        patch("app.services.quotes_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
    ):
        ok = quotes_mirror.reconcile_subscriber(db_session, str(sub.id))
    assert ok is True
    row = db_session.query(QuoteMirror).filter_by(crm_quote_id="q1").one()
    assert row.status == "draft"
    assert row.total == "75000.00"
    assert row.deposit_amount == "37500.00"
    assert row.feasibility_coverage == "covered"
    assert db_session.get(QuoteSyncState, sub.id) is not None


def test_read_counts_open_and_returns_payload(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_quotes.return_value = _crm_resp()
    with (
        patch("app.services.quotes_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
    ):
        out = quotes_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 1
    assert out["open"] == 1
    q = out["quotes"][0]
    assert q["deposit_amount"] == "37500.00"
    assert q["line_items"][0]["unit_price"] == "50000.00"


def test_read_serves_mirror_when_crm_unreachable(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    from app.services.crm_client import CRMClientError

    with patch(
        "app.services.quotes_mirror.reconcile_subscriber",
        side_effect=CRMClientError("down"),
    ):
        out = quotes_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 0


def test_webhook_accepted_upserts_and_pushes(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        out = quotes_mirror.apply_webhook(
            db_session,
            "quote.accepted",
            {"subscriber_id": str(sub.id), "quote_id": "q9", "status": "accepted"},
        )
    assert out["status"] == "ok"
    push.assert_called_once()
    row = db_session.query(QuoteMirror).filter_by(crm_quote_id="q9").one()
    assert row.status == "accepted"


def test_webhook_unmapped_ignored(db_session):
    out = quotes_mirror.apply_webhook(
        db_session,
        "quote.created",
        {"subscriber_id": str(uuid.uuid4()), "quote_id": "qX"},
    )
    assert out["reason"] == "unmapped_subscriber"


def test_webhook_unknown_event_ignored(db_session):
    sub = _subscriber(db_session)
    out = quotes_mirror.apply_webhook(
        db_session,
        "quote.archived",
        {"subscriber_id": str(sub.id), "quote_id": "q9"},
    )
    assert out["status"] == "ignored"
