"""Keep CRM as an inbound migration source, never a Sub write target."""

from __future__ import annotations

from pathlib import Path

from app.services.crm_client import CRMClient

ROOT = Path(__file__).resolve().parents[2]


#: Sub -> CRM business writes that are gone for good.
RETIRED_MUTATIONS = {
    "post_signed_webhook",
    "update_subscriber",
    "delete_subscriber",
    "create_ticket",
    "update_ticket",
    "create_ticket_comment",
    "update_work_order",
    "create_widget_session",
    "submit_portal_technician_rating",
}

#: The last remaining Sub -> CRM writes. They are the *write half* of a
#: flag-gated cutover whose *read half* has not happened: quotes_mirror and
#: referrals_mirror still serve reads behind `quotes_native_read_enabled` /
#: `referrals_native_read_enabled`, both defaulting OFF. Deleting these before
#: the reads are backfilled and cut over would leave portal quote requests and
#: referrals with nowhere to go.
#:
#: This set must only ever SHRINK. It goes to zero when the read cutover lands.
DEFERRED_MUTATIONS = {
    "request_portal_quote",
    "accept_portal_quote",
    "create_portal_referral",
}


def test_retired_crm_mutations_are_gone() -> None:
    assert RETIRED_MUTATIONS.isdisjoint(dir(CRMClient))


def test_deferred_mutations_are_the_only_remaining_writes() -> None:
    """No new Sub -> CRM write may be added, and the deferred ones may not be
    quietly forgotten.

    Anything on CRMClient that looks like a mutation must be explicitly listed
    in DEFERRED_MUTATIONS. A new write method fails this test; removing a
    deferred one requires deleting it from the set, which is the visible signal
    that the cutover advanced.
    """
    mutation_verbs = (
        "create_",
        "update_",
        "delete_",
        "submit_",
        "post_",
        "accept_",
        "request_",
    )
    present = {
        name
        for name in dir(CRMClient)
        if not name.startswith("_") and name.startswith(mutation_verbs)
    }
    assert present == DEFERRED_MUTATIONS, (
        "CRMClient write surface changed. Expected only the deferred portal "
        f"writes, found: {sorted(present)}"
    )


def test_outbound_crm_tasks_are_not_registered() -> None:
    sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "app/celery_app.py",
            "app/tasks/__init__.py",
            "app/services/scheduler_config.py",
            "app/services/events/dispatcher.py",
        )
    )
    for forbidden in (
        "push_ticket_to_crm",
        "push_comment_to_crm",
        "push_crm_billing_snapshots",
        "push_subscriber_change",
        "CrmSyncHandler",
    ):
        assert forbidden not in sources


def test_native_portal_writes_do_not_import_crm_mirrors() -> None:
    for path in ("app/api/me.py", "app/api/reseller.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "quotes_mirror.request_quote" not in source
        assert "referrals_mirror.refer_a_friend" not in source
