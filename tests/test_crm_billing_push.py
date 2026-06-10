"""Billing snapshot push: webhook routing, change-skip, failure isolation."""

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


def test_splynx_subscriber_pushes_splynx_webhook(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber, splynx_id=777)
    _balance(monkeypatch, "200")

    with patch(
        "app.services.crm_webhook.push_subscriber_change", return_value="ok"
    ) as push:
        stats = push_billing_snapshots(db_session)
    db_session.refresh(subscriber)

    assert stats["pushed"] == 1
    external_id, payload, system = push.call_args[0]
    assert external_id == 777
    assert system == "splynx"
    assert payload["balance"] == "200.00"
    # the splynx mapper has no billing_cycle output
    assert "billing_cycle" not in payload
    # the recorded snapshot still includes it, for change detection
    assert subscriber.metadata_[crm_billing_push._SNAPSHOT_KEY]["billing_cycle"]


def test_native_subscriber_pushes_generic_webhook(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber, splynx_id=None)
    _balance(monkeypatch, "200")

    with patch(
        "app.services.crm_webhook.push_subscriber_change", return_value="ok"
    ) as push:
        stats = push_billing_snapshots(db_session)

    assert stats["pushed"] == 1
    external_id, payload, system = push.call_args[0]
    assert external_id == str(subscriber.id)
    assert system == "dotmac"
    assert payload["billing_cycle"]


def test_push_skips_unchanged_snapshot(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber, splynx_id=777)
    _balance(monkeypatch, "200")

    with patch(
        "app.services.crm_webhook.push_subscriber_change", return_value="ok"
    ) as push:
        first = push_billing_snapshots(db_session)
        second = push_billing_snapshots(db_session)

    assert first["pushed"] == 1
    assert second["pushed"] == 0
    assert second["unchanged"] == 1
    assert push.call_count == 1


def test_push_isolates_failures(monkeypatch, db_session, subscriber):
    _link(db_session, subscriber, splynx_id=777)
    _balance(monkeypatch, "200")

    with patch("app.services.crm_webhook.push_subscriber_change", return_value=None):
        stats = push_billing_snapshots(db_session)
    db_session.refresh(subscriber)

    assert stats["failed"] == 1
    assert not (subscriber.metadata_ or {}).get(crm_billing_push._SNAPSHOT_KEY)


def test_push_ignores_unlinked_subscribers(monkeypatch, db_session, subscriber):
    _balance(monkeypatch, "200")

    with patch(
        "app.services.crm_webhook.push_subscriber_change", return_value="ok"
    ) as push:
        stats = push_billing_snapshots(db_session)

    assert stats["considered"] == 0
    push.assert_not_called()
