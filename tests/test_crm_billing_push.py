"""Billing snapshot push: snapshot shape, change-skip, failure isolation."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from app.services import crm_billing_push
from app.services.crm_billing_push import build_snapshot, push_billing_snapshots
from app.services.crm_client import CRMClientError


def _link(db, subscriber):
    subscriber.crm_subscriber_id = uuid4()
    db.commit()
    return subscriber


def _balance(monkeypatch, value):
    monkeypatch.setattr(
        "app.services.billing._common.get_account_credit_balance",
        lambda db, account_id: Decimal(value),
    )


def test_build_snapshot_shape(monkeypatch, db_session, subscriber):
    _balance(monkeypatch, "1500.50")

    snapshot = build_snapshot(db_session, subscriber)

    assert snapshot["balance"] == "1500.50"
    assert snapshot["currency"]
    assert snapshot["billing_cycle"]
    assert "next_bill_date" in snapshot


def test_push_sends_and_records_snapshot(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber)
    _balance(monkeypatch, "200")
    client = MagicMock()

    stats = push_billing_snapshots(db_session, client=client)
    db_session.refresh(subscriber)

    assert stats["pushed"] == 1
    client.update_subscriber.assert_called_once()
    crm_id, payload = client.update_subscriber.call_args[0]
    assert crm_id == str(subscriber.crm_subscriber_id)
    assert payload["balance"] == "200.00"
    assert subscriber.metadata_[crm_billing_push._SNAPSHOT_KEY] == payload


def test_push_skips_unchanged_snapshot(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber)
    _balance(monkeypatch, "200")
    client = MagicMock()

    first = push_billing_snapshots(db_session, client=client)
    second = push_billing_snapshots(db_session, client=client)

    assert first["pushed"] == 1
    assert second["pushed"] == 0
    assert second["unchanged"] == 1
    assert client.update_subscriber.call_count == 1


def test_push_isolates_failures(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber)
    _balance(monkeypatch, "200")
    client = MagicMock()
    client.update_subscriber.side_effect = CRMClientError("boom")

    stats = push_billing_snapshots(db_session, client=client)
    db_session.refresh(subscriber)

    assert stats["failed"] == 1
    assert not (subscriber.metadata_ or {}).get(crm_billing_push._SNAPSHOT_KEY)


def test_push_ignores_unlinked_subscribers(monkeypatch, db_session, subscriber):
    _balance(monkeypatch, "200")
    client = MagicMock()

    stats = push_billing_snapshots(db_session, client=client)

    assert stats["considered"] == 0
    client.update_subscriber.assert_not_called()
