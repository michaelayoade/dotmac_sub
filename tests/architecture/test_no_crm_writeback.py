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

#: The last remaining Sub -> CRM writes.
#:
#: They are the *write half* of a cutover whose *read half* has not happened:
#: ``quotes_mirror`` and ``referrals_mirror`` still serve reads behind
#: ``quotes_native_read_enabled`` / ``referrals_native_read_enabled``, both of
#: which default OFF. Deleting these before the reads are backfilled and cut
#: over would leave portal quote requests and referrals with nowhere to write.
#:
#: This set must only ever SHRINK. It goes to zero when the read cutover lands.
DEFERRED_MUTATIONS = {
    "request_portal_quote",
    "accept_portal_quote",
    "create_portal_referral",
}


def test_retired_crm_mutations_are_gone() -> None:
    assert RETIRED_MUTATIONS.isdisjoint(dir(CRMClient))


def test_deferred_mutations_are_still_present_and_tracked() -> None:
    """The last Sub -> CRM writes must not be silently dropped OR silently kept.

    Removing one without doing the read cutover would break portal quote
    requests and referrals -- the mirrors are still the read source. When the
    cutover lands these disappear and this set goes to zero, and that deletion
    is the visible signal that it happened.
    """
    assert DEFERRED_MUTATIONS <= set(dir(CRMClient))


def test_no_new_crm_business_writes_are_added() -> None:
    """Guard against a new Sub -> CRM write creeping in.

    ``create_portal_session`` is deliberately exempt: it mints an auth session,
    not a business mutation. CRM stays readable; it just does not get written.
    """
    write_verbs = (
        "create_",
        "update_",
        "delete_",
        "submit_",
        "post_",
        "accept_",
        "request_",
    )
    non_mutations = {"create_portal_session"}
    present = {
        name
        for name in dir(CRMClient)
        if not name.startswith("_") and name.startswith(write_verbs)
    }
    assert present - non_mutations == DEFERRED_MUTATIONS, (
        "CRMClient's write surface changed. Expected only the deferred portal "
        f"writes, found: {sorted(present - non_mutations)}"
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


# A guard asserting app/api/{me,reseller}.py no longer call
# ``quotes_mirror.request_quote`` / ``referrals_mirror.refer_a_friend`` belongs
# with the READ cutover, not here. Those calls are live and correct today: the
# mirrors are still the read source (flags default OFF), so the portal must
# still be able to write through them. Add that guard in the cutover PR, where
# it will actually hold.
