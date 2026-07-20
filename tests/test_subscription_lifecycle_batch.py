"""Canonical subscription lifecycle batch and admin UI contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jinja2 import Environment

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscription_lifecycle_schedule import SubscriptionLifecycleSchedule
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
)
from app.services.subscription_lifecycle_batch import (
    MAX_BATCH_SIZE,
    SubscriptionLifecycleBatchError,
    execute_subscription_batch,
    normalize_subscription_ids,
    preview_subscription_batch,
)
from app.services.web_catalog_subscription_workflows import (
    execute_bulk_lifecycle_response,
    preview_bulk_lifecycle_response,
)


def _subscription(db_session, subscriber, offer, *, status):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.postpaid,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def test_normalize_subscription_ids_deduplicates_and_bounds_batches():
    assert normalize_subscription_ids(" a, b,a, ,c ") == ("a", "b", "c")

    with pytest.raises(SubscriptionLifecycleBatchError, match="cannot exceed"):
        normalize_subscription_ids(str(index) for index in range(MAX_BATCH_SIZE + 1))


def test_batch_preview_preserves_each_item_eligibility_and_reviewed_head(
    db_session, subscriber, catalog_offer
):
    pending = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.pending
    )
    active = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )

    preview = preview_subscription_batch(
        db_session,
        [str(pending.id), str(active.id)],
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
    )

    assert preview.total == 2
    assert preview.eligible_count == 1
    assert preview.ineligible_count == 1
    assert set(preview.reviewed_heads) == {str(pending.id), str(active.id)}
    assert preview.items[0].proposed.status == "active"
    assert preview.items[1].eligibility_reasons == (
        "status_active_not_eligible_for_activate",
    )


def test_batch_execute_reports_partial_success_without_hiding_rejections(
    db_session, subscriber, catalog_offer
):
    pending = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.pending
    )
    active = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    ids = [str(pending.id), str(active.id)]
    preview = preview_subscription_batch(
        db_session,
        ids,
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
    )

    outcome = execute_subscription_batch(
        db_session,
        ids,
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        actor_id="operator-1",
        reviewed_heads=preview.reviewed_heads,
        idempotency_key="batch-activate-1",
    )

    db_session.refresh(pending)
    assert outcome.status == "partial"
    assert outcome.succeeded == 1
    assert outcome.count(SubscriptionCommandOutcomeStatus.applied) == 1
    assert outcome.count(SubscriptionCommandOutcomeStatus.rejected) == 1
    assert pending.status == SubscriptionStatus.active
    rejected = outcome.items[1]
    assert rejected.error_code == "status_active_not_eligible_for_activate"


def test_batch_execute_rejects_state_that_changed_after_preview(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.pending
    )
    preview = preview_subscription_batch(
        db_session,
        [str(subscription.id)],
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
    )
    subscription.updated_at = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
    db_session.commit()

    outcome = execute_subscription_batch(
        db_session,
        [str(subscription.id)],
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        actor_id="operator-1",
        reviewed_heads=preview.reviewed_heads,
        idempotency_key="stale-batch",
    )

    db_session.refresh(subscription)
    assert outcome.status == "rejected"
    assert outcome.items[0].status == SubscriptionCommandOutcomeStatus.superseded
    assert outcome.items[0].error_code == "subscription_head_changed"
    assert subscription.status == SubscriptionStatus.pending


def test_batch_retry_replays_without_duplicate_mutation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.pending
    )
    ids = [str(subscription.id)]
    preview = preview_subscription_batch(
        db_session,
        ids,
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
    )
    kwargs = {
        "kind": SubscriptionCommandKind.activate,
        "source": "admin:test",
        "actor_id": "operator-1",
        "reviewed_heads": preview.reviewed_heads,
        "idempotency_key": "retry-batch",
    }

    first = execute_subscription_batch(db_session, ids, **kwargs)
    replay = execute_subscription_batch(db_session, ids, **kwargs)

    assert first.items[0].status == SubscriptionCommandOutcomeStatus.applied
    assert replay.status == "completed"
    assert replay.items[0].status == SubscriptionCommandOutcomeStatus.skipped
    assert replay.items[0].replayed is True
    assert replay.items[0].error_code == "idempotent_replay"


def test_batch_schedules_deferred_status_commands_through_canonical_owner(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    ids = [str(subscription.id)]
    preview = preview_subscription_batch(
        db_session,
        ids,
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        effective_timing=SubscriptionEffectiveTiming.next_cycle,
    )

    outcome = execute_subscription_batch(
        db_session,
        ids,
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        actor_id="operator-1",
        effective_timing=SubscriptionEffectiveTiming.next_cycle,
        reviewed_heads=preview.reviewed_heads,
        idempotency_key="scheduled-batch",
    )

    assert outcome.status == "completed"
    assert outcome.items[0].status == SubscriptionCommandOutcomeStatus.scheduled
    schedule = db_session.query(SubscriptionLifecycleSchedule).one()
    assert schedule.command_kind == "suspend"
    assert schedule.effective_at.replace(tzinfo=UTC) == datetime(2026, 8, 1, tzinfo=UTC)


def test_admin_batch_workflow_serializes_preview_and_partial_outcomes(
    db_session, subscriber, catalog_offer
):
    pending = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.pending
    )
    active = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    ids = f"{pending.id},{active.id}"

    preview_payload, preview_status = preview_bulk_lifecycle_response(
        db_session,
        subscription_ids=ids,
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-1",
    )
    outcome_payload, outcome_status = execute_bulk_lifecycle_response(
        db_session,
        subscription_ids=ids,
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-1",
        reviewed_heads=json.dumps(preview_payload["reviewed_heads"]),
        idempotency_key="admin-batch",
    )

    assert preview_status == 200
    assert preview_payload["eligible_count"] == 1
    assert preview_payload["billing_impact"]["net_amounts"] == {"N/A": "0.00"}
    json.dumps(preview_payload)
    assert outcome_status == 200
    assert outcome_payload["status"] == "partial"
    assert outcome_payload["counts"]["applied"] == 1
    assert outcome_payload["counts"]["rejected"] == 1
    assert outcome_payload["skipped_ids"] == [str(active.id)]


def test_subscription_list_uses_reviewed_canonical_batch_ui():
    source = Path("templates/admin/catalog/subscriptions.html").read_text()
    Environment(autoescape=True).parse(source)

    assert "/bulk/lifecycle/preview" in source
    assert "reviewed_heads" in source
    assert "Idempotency-Key" in source
    assert "new Date(localValue).toISOString()" in source
    assert "kind === 'change_plan' ? 'date' : 'datetime-local'" in source
    assert "preview.billing_impact" in source
    assert "preview.access_impact" in source
    assert "result.items" in source
    assert "confirm(" not in source
    assert "alert(" not in source
    assert "include_suspended" not in source


def test_customer_suspended_service_action_uses_restore_command():
    source = Path("app/web/admin/customers.py").read_text()
    function = source.split("def customer_activate_suspended_services", 1)[1].split(
        "\n\n@router", 1
    )[0]

    assert "bulk_restore_response" in function
    assert "bulk_activate_response" not in function
