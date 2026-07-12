"""Keep CRM as an inbound migration source, never a Sub write target."""

from __future__ import annotations

from pathlib import Path

from app.services.crm_client import CRMClient


ROOT = Path(__file__).resolve().parents[2]


def test_crm_client_exposes_no_business_mutations() -> None:
    forbidden = {
        "post_signed_webhook",
        "update_subscriber",
        "create_ticket",
        "update_ticket",
        "create_ticket_comment",
        "update_work_order",
        "delete_subscriber",
        "create_widget_session",
        "submit_portal_technician_rating",
        "create_portal_referral",
        "request_portal_quote",
        "accept_portal_quote",
    }
    assert forbidden.isdisjoint(dir(CRMClient))


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
