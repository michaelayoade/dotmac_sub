"""Billing snapshot push: enqueue routing, change-skip, dead-letter, re-drive."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from app.services import crm_billing_push
from app.services.crm_billing_push import build_snapshot, push_billing_snapshots


def _link(db, subscriber, splynx_id=None):
    subscriber.crm_subscriber_id = uuid4()
    subscriber.splynx_customer_id = splynx_id
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


def test_splynx_subscriber_enqueues_splynx_webhook(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber, splynx_id=777)
    _balance(monkeypatch, "200")

    with patch("app.tasks.crm_sync.push_subscriber_change") as task:
        stats = push_billing_snapshots(db_session)

    assert stats["enqueued"] == 1
    args, kwargs = task.delay.call_args
    external_id, payload, system = args
    assert external_id == 777
    assert system == "splynx"
    assert payload["balance"] == "200.00"
    # The splynx mapper has no billing_cycle output — and the dedupe key is
    # exactly what we transmit, so the stamp/compare stay consistent.
    assert "billing_cycle" not in payload
    assert kwargs["billing_snapshot_subscriber_id"] == str(subscriber.id)


def test_native_subscriber_enqueues_generic_webhook(
    monkeypatch, db_session, subscriber
):
    _link(db_session, subscriber, splynx_id=None)
    _balance(monkeypatch, "200")

    with patch("app.tasks.crm_sync.push_subscriber_change") as task:
        stats = push_billing_snapshots(db_session)

    assert stats["enqueued"] == 1
    args, _ = task.delay.call_args
    external_id, payload, system = args
    assert external_id == str(subscriber.id)
    assert system == "dotmac"
    assert payload["billing_cycle"]


def test_push_skips_unchanged_snapshot(monkeypatch, db_session, subscriber):
    """Second run skips once the task has stamped the transmitted payload."""
    _link(db_session, subscriber, splynx_id=777)
    _balance(monkeypatch, "200")

    with patch("app.tasks.crm_sync.push_subscriber_change") as task:
        first = push_billing_snapshots(db_session)
        # Simulate the task stamping on delivery success.
        sent_payload = task.delay.call_args[0][1]
        subscriber.metadata_ = {crm_billing_push._SNAPSHOT_KEY: sent_payload}
        db_session.commit()
        second = push_billing_snapshots(db_session)

    assert first["enqueued"] == 1
    assert second["enqueued"] == 0
    assert second["unchanged"] == 1
    assert task.delay.call_count == 1


def test_push_ignores_unlinked_subscribers(monkeypatch, db_session, subscriber):
    _balance(monkeypatch, "200")

    with patch("app.tasks.crm_sync.push_subscriber_change") as task:
        stats = push_billing_snapshots(db_session)

    assert stats["considered"] == 0
    task.delay.assert_not_called()
