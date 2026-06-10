"""CRM duplicate merge: re-pointing, safety guards, dry-run, metadata audit."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from app.services.crm_duplicate_merge import merge_duplicates


def _linked_subscriber(db, subscriber, alias_ids):
    subscriber.crm_subscriber_id = uuid4()
    subscriber.metadata_ = {"crm_alias_ids": [str(a) for a in alias_ids]}
    db.commit()
    return subscriber


def _client(alias_records, tickets_by_alias=None, work_orders_by_alias=None):
    client = MagicMock()
    client.get_subscriber.side_effect = lambda sid: alias_records[sid]
    client.list_tickets.side_effect = lambda subscriber_id=None, **kw: (
        (tickets_by_alias or {}).get(subscriber_id, [])
        if kw.get("offset", 0) == 0
        else []
    )
    client.list_work_orders.side_effect = lambda subscriber_id=None: (
        work_orders_by_alias or {}
    ).get(subscriber_id, [])
    return client


def test_live_merge_repoints_and_soft_deletes(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    ticket_id, wo_id = str(uuid4()), str(uuid4())
    client = _client(
        {alias_id: {"id": alias_id, "external_system": "erpnext"}},
        tickets_by_alias={alias_id: [{"id": ticket_id}]},
        work_orders_by_alias={alias_id: [{"id": wo_id}]},
    )

    stats = merge_duplicates(db_session, client=client, dry_run=False)
    db_session.refresh(subscriber)

    assert stats["merged"] == 1
    assert stats["tickets_moved"] == 1
    assert stats["work_orders_moved"] == 1
    client.update_ticket.assert_called_once_with(
        ticket_id, {"subscriber_id": str(subscriber.crm_subscriber_id)}
    )
    client.update_work_order.assert_called_once_with(
        wo_id, {"subscriber_id": str(subscriber.crm_subscriber_id)}
    )
    client.delete_subscriber.assert_called_once_with(alias_id)
    assert not (subscriber.metadata_ or {}).get("crm_alias_ids")
    assert subscriber.metadata_["crm_merged_alias_ids"] == [alias_id]


def test_dry_run_counts_without_writes(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    client = _client(
        {alias_id: {"id": alias_id, "external_system": "erpnext"}},
        tickets_by_alias={alias_id: [{"id": str(uuid4())}]},
    )

    stats = merge_duplicates(db_session, client=client, dry_run=True)
    db_session.refresh(subscriber)

    assert stats["merged"] == 1
    assert stats["tickets_moved"] == 1
    client.update_ticket.assert_not_called()
    client.delete_subscriber.assert_not_called()
    assert subscriber.metadata_["crm_alias_ids"] == [alias_id]


def test_non_erpnext_alias_is_never_merged(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    client = _client({alias_id: {"id": alias_id, "external_system": "splynx"}})

    stats = merge_duplicates(db_session, client=client, dry_run=False)
    db_session.refresh(subscriber)

    assert stats["alias_not_erpnext"] == 1
    assert stats["merged"] == 0
    client.delete_subscriber.assert_not_called()
    assert subscriber.metadata_["crm_alias_ids"] == [alias_id]


def test_limit_caps_processed_subscribers(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    client = _client({alias_id: {"id": alias_id, "external_system": "erpnext"}})

    stats = merge_duplicates(db_session, client=client, dry_run=True, limit=0)

    assert stats["subscribers"] == 0
