"""Local self-serve quote mirror service: reconcile, read, request, webhooks."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.models.audit import AuditEvent
from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.subscriber import Subscriber
from app.services import quotes_mirror
from app.services.crm_client import CRMClientError
from app.services.integrations.installations import InstallationError


@contextmanager
def _transport_enabled():
    """Satisfy the portal-quote command precondition.

    ``request_quote``/``accept_quote`` now refuse before touching the network
    unless the CRM quote-command capability binding is enabled, so any test
    that exercises the happy path has to say so explicitly.
    """
    with patch(
        "app.services.quotes_mirror.installations.require_enabled_capability_binding",
        return_value=MagicMock(),
    ):
        yield


def _refusals(db, action: str) -> list[AuditEvent]:
    return db.query(AuditEvent).filter(AuditEvent.action == action).all()


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
        patch("app.services.quotes_mirror.capability_client", return_value=client),
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
        patch("app.services.quotes_mirror.capability_client", return_value=client),
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


def test_read_result_marks_missing_crm_binding_unavailable(db_session):
    from app.services.integrations.installations import InstallationError

    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    with patch(
        "app.services.quotes_mirror.reconcile_subscriber",
        side_effect=InstallationError("no enabled binding"),
    ):
        result = quotes_mirror.read_for_subscriber_result(db_session, str(sub.id))

    assert result.state == quotes_mirror.QuoteReadState.unavailable
    assert result.payload == {"quotes": [], "total": 0, "open": 0}


def test_request_quote_write_through_mirrors_result(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.request_portal_quote.return_value = _crm_quote(id="qNEW", status="draft")
    with (
        _transport_enabled(),
        patch("app.services.quotes_mirror.capability_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
    ):
        out = quotes_mirror.request_quote(
            db_session, str(sub.id), latitude=9.07, longitude=7.49, address="12 Test St"
        )
    assert out["id"] == "qNEW"
    client.request_portal_quote.assert_called_once()
    row = db_session.query(QuoteMirror).filter_by(crm_quote_id="qNEW").one()
    assert row.feasibility_coverage == "covered"


def test_request_quote_requires_crm_link(db_session):
    import pytest
    from fastapi import HTTPException

    sub = _subscriber(db_session)  # no crm_subscriber_id
    with patch(
        "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value=None
    ):
        with pytest.raises(HTTPException) as exc:
            quotes_mirror.request_quote(
                db_session, str(sub.id), latitude=1.0, longitude=2.0
            )
    assert exc.value.status_code == 400


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
    assert push.call_args.kwargs["intent"].intent_code == "quote.accepted"
    assert push.call_args.kwargs["intent"].subject_id == "q9"
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


# ---------------------------------------------------------------------------
# Portal quote COMMANDS fail closed
#
# CRM/Omni was decommissioned on 2026-08-29. These two commands are the last
# Sub -> CRM business writes and they sit on a customer-money path, so a dead
# transport must produce an explicit, audited refusal -- never a hang, never a
# 200 with an empty body, and never a mirrored row for a command nobody
# acknowledged.
# ---------------------------------------------------------------------------


def test_request_quote_refuses_when_the_transport_is_not_enabled(db_session):
    """No enabled capability binding is a REFUSAL, decided without a network call."""
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    with (
        patch(
            "app.services.quotes_mirror.installations."
            "require_enabled_capability_binding",
            side_effect=InstallationError("no enabled binding"),
        ),
        patch("app.services.quotes_mirror.capability_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
        pytest.raises(quotes_mirror.PortalQuoteCommandError) as exc,
    ):
        quotes_mirror.request_quote(
            db_session, str(sub.id), latitude=9.07, longitude=7.49
        )

    assert exc.value.code == "sales.portal_quote.transport_unavailable"
    # Decided locally: the dead transport is never dialled, so the caller is
    # refused immediately instead of blocking on a connect timeout.
    client.request_portal_quote.assert_not_called()
    assert db_session.query(QuoteMirror).count() == 0
    refusals = _refusals(db_session, quotes_mirror.PORTAL_QUOTE_REQUEST_REFUSED)
    assert len(refusals) == 1
    assert refusals[0].is_success is False
    assert refusals[0].metadata_["error_code"] == (
        "sales.portal_quote.transport_unavailable"
    )


def test_accept_quote_refuses_and_mirrors_nothing_when_the_transport_fails(db_session):
    """A transport failure on the acceptance leaves no partial mirror state."""
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.accept_portal_quote.side_effect = CRMClientError("connection refused")
    with (
        _transport_enabled(),
        patch("app.services.quotes_mirror.capability_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
        pytest.raises(quotes_mirror.PortalQuoteCommandError) as exc,
    ):
        quotes_mirror.accept_quote(
            db_session,
            str(sub.id),
            "q-dead",
            deposit_reference="ref_1",
            deposit_amount="37500.00",
        )

    assert exc.value.code == "sales.portal_quote.transport_failed"
    assert db_session.query(QuoteMirror).count() == 0
    refusals = _refusals(db_session, quotes_mirror.PORTAL_QUOTE_ACCEPT_REFUSED)
    assert len(refusals) == 1
    assert refusals[0].metadata_["quote_id"] == "q-dead"


def test_accept_quote_refuses_an_unacknowledged_response(db_session):
    """The silent-success regression: a 200 with an empty payload is a failure.

    The old code returned ``{}`` here, so a customer whose deposit had already
    been recorded saw a successful HTTP 200 for an acceptance that never
    happened.
    """
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.accept_portal_quote.return_value = {}
    with (
        _transport_enabled(),
        patch("app.services.quotes_mirror.capability_client", return_value=client),
        patch(
            "app.services.quotes_mirror.resolve_crm_subscriber_id", return_value="crm-1"
        ),
        pytest.raises(quotes_mirror.PortalQuoteCommandError) as exc,
    ):
        quotes_mirror.accept_quote(
            db_session,
            str(sub.id),
            "q-silent",
            deposit_reference="ref_1",
            deposit_amount="37500.00",
        )

    assert exc.value.code == "sales.portal_quote.command_not_acknowledged"
    assert db_session.query(QuoteMirror).count() == 0


def test_portal_quote_refusal_message_leaks_no_transport_detail(db_session):
    """The customer-facing message names no endpoint, host or capability."""
    message = quotes_mirror.PORTAL_QUOTE_UNAVAILABLE_MESSAGE.lower()
    for forbidden in ("crm", "omni", "http", "://", "capability", "binding"):
        assert forbidden not in message
    # It must still say the two things a charged customer needs to hear.
    assert "nothing was charged" in message
    assert "no quote was" in message


def test_ensure_portal_quote_commands_available_passes_when_enabled(db_session):
    with _transport_enabled():
        quotes_mirror.ensure_portal_quote_commands_available(db_session)
