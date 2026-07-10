"""CRM duplicate merge: re-pointing, safety guards, dry-run, metadata audit.

Work orders are re-pointed natively (work_order_mirror.subscriber_id) — the
CRM-side WO reassignment stopped at the Phase 2 work-order SoT flip.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID, uuid4

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services.crm_duplicate_merge import merge_duplicates


def _linked_subscriber(db, subscriber, alias_ids):
    subscriber.crm_subscriber_id = uuid4()
    subscriber.metadata_ = {"crm_alias_ids": [str(a) for a in alias_ids]}
    db.commit()
    return subscriber


def _client(alias_records, tickets_by_alias=None):
    client = MagicMock()
    client.get_subscriber.side_effect = lambda sid: alias_records[sid]
    client.list_tickets.side_effect = lambda subscriber_id=None, **kw: (
        (tickets_by_alias or {}).get(subscriber_id, [])
        if kw.get("offset", 0) == 0
        else []
    )
    return client


def _alias_local_subscriber(db, alias_id, *, work_order_ids=()):
    """A historical duplicate local subscriber still linked to the alias CRM id,
    with mirror rows hanging off it."""
    duplicate = Subscriber(
        first_name="Dup",
        last_name="Licate",
        email=f"dup-{uuid4().hex[:8]}@example.com",
        crm_subscriber_id=UUID(str(alias_id)),
    )
    db.add(duplicate)
    db.flush()
    for wo_id in work_order_ids:
        db.add(
            WorkOrderMirror(
                subscriber_id=duplicate.id,
                crm_work_order_id=str(wo_id),
                title="Repair",
                status="scheduled",
            )
        )
    db.commit()
    return duplicate


def test_live_merge_repoints_and_soft_deletes(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    ticket_id = str(uuid4())
    client = _client(
        {alias_id: {"id": alias_id, "external_system": "erpnext"}},
        tickets_by_alias={alias_id: [{"id": ticket_id}]},
    )

    stats = merge_duplicates(db_session, client=client, dry_run=False)
    db_session.refresh(subscriber)

    assert stats["merged"] == 1
    assert stats["tickets_moved"] == 1
    assert stats["work_orders_moved"] == 0
    client.update_ticket.assert_called_once_with(
        ticket_id, {"subscriber_id": str(subscriber.crm_subscriber_id)}
    )
    # No CRM work-order calls post-flip (sub is the work-order SoT).
    client.list_work_orders.assert_not_called()
    client.update_work_order.assert_not_called()
    client.delete_subscriber.assert_called_once_with(alias_id)
    assert not (subscriber.metadata_ or {}).get("crm_alias_ids")
    assert subscriber.metadata_["crm_merged_alias_ids"] == [alias_id]


def test_live_merge_repoints_native_mirror_rows(db_session, subscriber):
    """Mirror rows hanging off a duplicate local subscriber (still linked to the
    alias CRM id) are re-pointed natively to the primary subscriber."""
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    wo_id = str(uuid4())
    _alias_local_subscriber(db_session, alias_id, work_order_ids=[wo_id])
    client = _client({alias_id: {"id": alias_id, "external_system": "erpnext"}})

    stats = merge_duplicates(db_session, client=client, dry_run=False)

    assert stats["merged"] == 1
    assert stats["work_orders_moved"] == 1
    client.update_work_order.assert_not_called()
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id=wo_id).one()
    assert row.subscriber_id == subscriber.id


def test_dry_run_counts_without_writes(db_session, subscriber):
    alias_id = str(uuid4())
    _linked_subscriber(db_session, subscriber, [alias_id])
    wo_id = str(uuid4())
    duplicate = _alias_local_subscriber(db_session, alias_id, work_order_ids=[wo_id])
    client = _client(
        {alias_id: {"id": alias_id, "external_system": "erpnext"}},
        tickets_by_alias={alias_id: [{"id": str(uuid4())}]},
    )

    stats = merge_duplicates(db_session, client=client, dry_run=True)
    db_session.refresh(subscriber)

    assert stats["merged"] == 1
    assert stats["tickets_moved"] == 1
    assert stats["work_orders_moved"] == 1
    client.update_ticket.assert_not_called()
    client.delete_subscriber.assert_not_called()
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id=wo_id).one()
    assert row.subscriber_id == duplicate.id  # untouched in dry-run
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
