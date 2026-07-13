"""Tests for safe all-active subscriber-status drift reconcile.

See app/services/account_status_reconcile.py. Cohort = subscriber new/blocked
while ALL subscriptions are active; mixed-status accounts are excluded.
"""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_status_reconcile import (
    account_eligibility,
    find_blocked_all_active_account_ids,
    reconcile_account,
    reconcile_cohort,
)


def _subscriber(db, email, status=SubscriberStatus.blocked):
    s = Subscriber(first_name="D", last_name="R", email=email, status=status)
    db.add(s)
    db.flush()
    return s


def _sub(db, subscriber, offer, status):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"login-{subscriber.email.split('@')[0]}",
    )
    db.add(sub)
    db.flush()
    return sub


def test_finder_matches_blocked_all_active(db_session, catalog_offer):
    s = _subscriber(db_session, "drift@e.com")
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    ids = find_blocked_all_active_account_ids(db_session)
    assert str(s.id) in ids


def test_finder_matches_new_all_active(db_session, catalog_offer):
    s = _subscriber(db_session, "new-drift@e.com", status=SubscriberStatus.new)
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    ids = find_blocked_all_active_account_ids(db_session)

    assert str(s.id) in ids


def test_finder_excludes_mixed_status(db_session, catalog_offer):
    s = _subscriber(db_session, "mixed@e.com")
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    _sub(db_session, s, catalog_offer, SubscriptionStatus.suspended)
    db_session.commit()

    ids = find_blocked_all_active_account_ids(db_session)
    assert str(s.id) not in ids


def test_finder_excludes_already_active_subscriber(db_session, catalog_offer):
    s = _subscriber(db_session, "fine@e.com", status=SubscriberStatus.active)
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    ids = find_blocked_all_active_account_ids(db_session)
    assert str(s.id) not in ids


def test_finder_excludes_explicit_account_override(db_session, catalog_offer):
    s = _subscriber(db_session, "admin-blocked@e.com")
    s.lifecycle_override_status = SubscriberStatus.blocked
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    ids = find_blocked_all_active_account_ids(db_session)

    assert str(s.id) not in ids
    assert account_eligibility(db_session, str(s.id)) == (
        False,
        "explicit_lifecycle_override",
    )


def test_reconcile_account_flips_to_active(db_session, catalog_offer):
    s = _subscriber(db_session, "flip@e.com")
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    result = reconcile_account(db_session, str(s.id))
    assert result.changed is True
    assert result.prior_status == "blocked"
    assert result.new_status == "active"
    assert db_session.get(Subscriber, s.id).status == SubscriberStatus.active


def test_dry_run_mutates_nothing(db_session, catalog_offer):
    s = _subscriber(db_session, "dry@e.com")
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    summary = reconcile_cohort(db_session, dry_run=True)
    assert summary.candidates >= 1
    assert summary.changed == 0
    assert summary.radius_refreshed is False
    projected = next(r for r in summary.results if r.account_id == str(s.id))
    assert projected.new_status == "active"
    assert projected.changed is True
    assert db_session.get(Subscriber, s.id).status == SubscriberStatus.blocked


def test_eligibility_classifies_reasons(db_session, catalog_offer):
    blocked_ok = _subscriber(db_session, "ok@e.com")
    _sub(db_session, blocked_ok, catalog_offer, SubscriptionStatus.active)
    mixed = _subscriber(db_session, "mix@e.com")
    _sub(db_session, mixed, catalog_offer, SubscriptionStatus.active)
    _sub(db_session, mixed, catalog_offer, SubscriptionStatus.suspended)
    active = _subscriber(db_session, "act@e.com", status=SubscriberStatus.active)
    _sub(db_session, active, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    assert account_eligibility(db_session, str(blocked_ok.id)) == (True, None)
    ok, reason = account_eligibility(db_session, str(mixed.id))
    assert ok is False and "mixed" in reason
    ok, reason = account_eligibility(db_session, str(active.id))
    assert ok is False and "not_safe_prior_status" in reason


def test_account_ids_filters_ineligible_and_skips(db_session, catalog_offer):
    blocked_ok = _subscriber(db_session, "tok@e.com")
    _sub(db_session, blocked_ok, catalog_offer, SubscriptionStatus.active)
    mixed = _subscriber(db_session, "tmix@e.com")
    _sub(db_session, mixed, catalog_offer, SubscriptionStatus.active)
    _sub(db_session, mixed, catalog_offer, SubscriptionStatus.disabled)
    db_session.commit()

    # Targeted apply over both — only the eligible one is mutated; mixed skipped.
    summary = reconcile_cohort(
        db_session,
        account_ids=[str(blocked_ok.id), str(mixed.id)],
        dry_run=False,
        refresh_fn=lambda: None,
        coa_fn=lambda *a, **k: 0,
    )
    assert summary.candidates == 1
    assert summary.changed == 1
    assert len(summary.skipped) == 1
    assert summary.skipped[0]["account_id"] == str(mixed.id)
    assert "mixed" in summary.skipped[0]["reason"]
    # mixed stays blocked; eligible flipped
    assert db_session.get(Subscriber, mixed.id).status == SubscriberStatus.blocked
    assert db_session.get(Subscriber, blocked_ok.id).status == SubscriberStatus.active


def test_apply_refreshes_radius_and_kicks(db_session, catalog_offer):
    s = _subscriber(db_session, "apply@e.com")
    _sub(db_session, s, catalog_offer, SubscriptionStatus.active)
    db_session.commit()

    # Inject the refresh/CoA callables so the apply path is decoupled from the
    # RADIUS-sweep module (which may not be present on every base).
    calls = {"refresh": 0, "kicks": 0}

    def _refresh():
        calls["refresh"] += 1

    def _coa(db, subscription_id, *, reason):
        calls["kicks"] += 1
        return 1

    summary = reconcile_cohort(
        db_session, dry_run=False, refresh_fn=_refresh, coa_fn=_coa
    )
    assert summary.changed == 1
    assert summary.radius_refreshed is True
    assert calls["refresh"] == 1
    assert calls["kicks"] == 1
    assert db_session.get(Subscriber, s.id).status == SubscriberStatus.active
