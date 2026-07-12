"""Read-only CRM referral mirror: reconcile, read, and inbound events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.models.referral import ReferralMirror, ReferralProgramCache
from app.models.subscriber import Subscriber
from app.services import referrals_mirror


def _subscriber(db, crm_id: uuid.UUID | None = None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        display_name="Cust Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _fresh_cache(db, sub, **kw):
    cache = ReferralProgramCache(
        subscriber_id=sub.id,
        code=kw.get("code", "DOTMAC-AB12"),
        share_url=kw.get("share_url", "https://app.dotmac.io/r/DOTMAC-AB12"),
        program_enabled=kw.get("program_enabled", True),
        reward_amount=kw.get("reward_amount", Decimal("5000")),
        reward_currency="NGN",
        synced_at=datetime.now(UTC),
    )
    db.add(cache)
    db.commit()
    return cache


# ── read (from mirror, no CRM call when cache is fresh) ──────────────────────


def test_read_builds_payload_from_mirror(db_session):
    sub = _subscriber(db_session)
    _fresh_cache(db_session, sub)
    db_session.add(
        ReferralMirror(
            crm_referral_id="r1",
            subscriber_id=sub.id,
            referred_name="Ada",
            status="rewarded",
            reward_amount=Decimal("5000"),
            reward_currency="NGN",
            reward_status="paid",
            referral_created_at=datetime.now(UTC),
        )
    )
    db_session.add(
        ReferralMirror(
            crm_referral_id="r2",
            subscriber_id=sub.id,
            referred_name="Bem",
            status="pending",
        )
    )
    db_session.commit()

    out = referrals_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["code"] == "DOTMAC-AB12"
    assert out["share_url"].endswith("/r/DOTMAC-AB12")
    assert out["program"]["enabled"] is True
    assert out["program"]["reward_amount"] == "5000.00"  # Numeric(12,2) scale
    assert out["totals"]["total"] == 2
    assert out["totals"]["pending"] == 1
    assert out["totals"]["rewarded"] == 1
    assert out["totals"]["total_earned"] == "5000.00"
    assert {r["id"] for r in out["referrals"]} == {"r1", "r2"}


def test_read_serves_mirror_when_crm_unreachable(db_session):
    # No cache row → stale → tries reconcile; CRM down → serve empty gracefully.
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    from app.services.crm_client import CRMClientError

    with patch(
        "app.services.referrals_mirror.reconcile_subscriber",
        side_effect=CRMClientError("down"),
    ):
        out = referrals_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["totals"]["total"] == 0
    assert out["program"]["enabled"] is False


# ── reconcile (pull) ─────────────────────────────────────────────────────────


def test_reconcile_upserts_cache_and_rows(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    crm_resp = {
        "code": "DOTMAC-ZZ99",
        "share_url": "https://app.dotmac.io/r/DOTMAC-ZZ99",
        "program": {"enabled": True, "reward_amount": "5000", "reward_currency": "NGN"},
        "referrals": [
            {
                "id": "r1",
                "status": "qualified",
                "referred_name": "Ada",
                "reward_status": "pending",
                "created_at": "2026-06-01T10:00:00+00:00",
            },
        ],
    }
    client = MagicMock()
    client.get_portal_referrals.return_value = crm_resp
    with (
        patch("app.services.referrals_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.referrals_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        ok = referrals_mirror.reconcile_subscriber(db_session, str(sub.id))
    assert ok is True
    cache = db_session.get(ReferralProgramCache, sub.id)
    assert cache.code == "DOTMAC-ZZ99"
    row = db_session.query(ReferralMirror).filter_by(crm_referral_id="r1").one()
    assert row.status == "qualified"
    assert row.referred_name == "Ada"


def test_reconcile_noops_when_not_linked(db_session):
    sub = _subscriber(db_session, crm_id=None)
    with patch(
        "app.services.referrals_mirror.resolve_crm_subscriber_id", return_value=None
    ):
        assert referrals_mirror.reconcile_subscriber(db_session, str(sub.id)) is False


# ── webhook application ─────────────────────────────────────────────────────


def test_webhook_captured_creates_pending_row_via_crm_id(db_session):
    crm_id = uuid.uuid4()
    sub = _subscriber(db_session, crm_id=crm_id)
    out = referrals_mirror.apply_webhook(
        db_session,
        "referral.captured",
        {"crm_subscriber_id": str(crm_id), "referral_id": "r9", "referred_name": "Ada"},
    )
    assert out["status"] == "ok"
    row = db_session.query(ReferralMirror).filter_by(crm_referral_id="r9").one()
    assert row.status == "pending"
    assert row.subscriber_id == sub.id


def test_webhook_maps_by_local_subscriber_id(db_session):
    # The CRM knows the sub's own id (external_id) and sends it as subscriber_id.
    sub = _subscriber(db_session)  # no crm link
    out = referrals_mirror.apply_webhook(
        db_session,
        "referral.qualified",
        {"subscriber_id": str(sub.id), "referral_id": "r-loc"},
    )
    assert out["status"] == "ok"
    row = db_session.query(ReferralMirror).filter_by(crm_referral_id="r-loc").one()
    assert row.status == "qualified"
    assert row.subscriber_id == sub.id


def test_webhook_rewarded_mirrors_without_crediting(db_session):
    # The CRM already credited via /crm/credits; the webhook only mirrors status.
    crm_id = uuid.uuid4()
    _subscriber(db_session, crm_id=crm_id)
    with patch("app.services.push.send_push") as push:
        out = referrals_mirror.apply_webhook(
            db_session,
            "referral.rewarded",
            {
                "crm_subscriber_id": str(crm_id),
                "referral_id": "r9",
                "amount": "5000",
                "currency": "NGN",
            },
        )
    assert out["status"] == "ok"
    assert "credit_id" not in out  # we never credit here
    push.assert_called_once()
    row = db_session.query(ReferralMirror).filter_by(crm_referral_id="r9").one()
    assert row.status == "rewarded"
    assert row.reward_amount == Decimal("5000")
    assert row.reward_status == "paid"


def test_webhook_unmapped_subscriber_ignored(db_session):
    out = referrals_mirror.apply_webhook(
        db_session,
        "referral.captured",
        {"crm_subscriber_id": str(uuid.uuid4()), "referral_id": "rX"},
    )
    assert out["reason"] == "unmapped_subscriber"


def test_webhook_incomplete_ignored(db_session):
    out = referrals_mirror.apply_webhook(
        db_session, "referral.captured", {"referral_id": "rX"}
    )
    assert out["reason"] == "incomplete_payload"


def test_stale_read_serves_stale_and_refreshes_async(db_session):
    """A warm-but-stale referral read serves the mirror immediately and refreshes
    in the background instead of blocking on the CRM (P3-sub, cache variant)."""
    from datetime import timedelta

    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    cache = _fresh_cache(db_session, sub)
    cache.synced_at = datetime.now(UTC) - timedelta(hours=1)  # make it stale
    db_session.add(
        ReferralMirror(
            crm_referral_id="r-stale",
            subscriber_id=sub.id,
            referred_name="Old",
            status="pending",
        )
    )
    db_session.commit()

    enqueued: list = []
    with (
        patch("app.services.referrals_mirror.reconcile_subscriber") as recon,
        patch(
            "app.services.queue_adapter.enqueue_task",
            side_effect=lambda *a, **k: enqueued.append((a, k)),
        ),
    ):
        out = referrals_mirror.read_for_subscriber(db_session, str(sub.id))

    recon.assert_not_called()  # did NOT block on the CRM
    assert len(enqueued) == 1  # enqueued a background refresh
    assert out["totals"]["total"] == 1  # served the stale row immediately
    st = db_session.get(ReferralProgramCache, sub.id)
    assert (datetime.now(UTC) - st.synced_at.replace(tzinfo=UTC)).total_seconds() < 60
