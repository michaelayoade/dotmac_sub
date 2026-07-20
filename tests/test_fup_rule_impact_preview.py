"""FUP rule impact preview (Tier-4 #5b).

Before an admin saves a draft FUP rule, ``preview_rule_impact`` counts how many
active subscribers on the offer already meet/exceed the draft threshold in the
rule's consumption window right now — the blast radius, surfaced pre-save so a
bad threshold that hits everyone can't slip through.

The preview reuses the evaluator's threshold conversion (``_threshold_gb``) and
the windowed usage reader (``get_fup_usage_gb_async``); these tests seed a few
subscribers with differing current usage and assert the count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.models.usage import QuotaBucket
from app.schemas.catalog import SubscriptionCreate
from app.services import catalog as catalog_service
from app.services.web_fup import preview_rule_impact


def _month_bounds(now):
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = (
        datetime(now.year + 1, 1, 1, tzinfo=UTC)
        if now.month == 12
        else datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    )
    return start, end


def _active_sub_with_usage(db, offer, *, used_gb: float, email: str):
    subscriber = Subscriber(first_name="Test", last_name="User", email=email)
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)

    sub = catalog_service.subscriptions.create(
        db,
        SubscriptionCreate(account_id=subscriber.id, offer_id=offer.id),
    )
    sub.status = SubscriptionStatus.active
    sub.billing_mode = BillingMode.postpaid
    db.commit()

    now = datetime.now(UTC)
    start, end = _month_bounds(now)
    db.add(
        QuotaBucket(
            subscription_id=sub.id,
            period_start=start,
            period_end=end,
            included_gb=Decimal("1000.00"),
            used_gb=Decimal(str(used_gb)),
            rollover_gb=Decimal("0.00"),
            overage_gb=Decimal("0.00"),
        )
    )
    db.commit()
    return sub


def test_preview_counts_only_subs_over_threshold(db_session, catalog_offer):
    # Two subs over 100 GB, one under.
    _active_sub_with_usage(db_session, catalog_offer, used_gb=150.0, email="a@x.io")
    _active_sub_with_usage(db_session, catalog_offer, used_gb=120.0, email="b@x.io")
    _active_sub_with_usage(db_session, catalog_offer, used_gb=40.0, email="c@x.io")

    result = preview_rule_impact(
        db_session,
        str(catalog_offer.id),
        threshold_amount="100",
        threshold_unit="gb",
        direction="up_down",
        consumption_period="monthly",
        action="reduce_speed",
    )

    assert result["matched_count"] == 2
    assert result["total_active_on_offer"] == 3
    assert result["scanned_count"] == 3
    assert result["capped"] is False
    assert result["threshold_gb"] == 100.0
    # Sample carries the offending subscribers (bounded to a handful).
    assert len(result["sample"]) == 2
    assert all(entry["used_gb"] >= 100.0 for entry in result["sample"])


def test_preview_threshold_that_hits_everyone(db_session, catalog_offer):
    # The footgun: a tiny threshold catches every active subscriber.
    _active_sub_with_usage(db_session, catalog_offer, used_gb=5.0, email="d@x.io")
    _active_sub_with_usage(db_session, catalog_offer, used_gb=0.5, email="e@x.io")

    result = preview_rule_impact(
        db_session,
        str(catalog_offer.id),
        threshold_amount="0.1",
        threshold_unit="gb",
        consumption_period="monthly",
        action="block",
    )

    assert result["matched_count"] == 2
    assert result["total_active_on_offer"] == 2


def test_preview_unit_conversion_reuses_evaluator(db_session, catalog_offer):
    # 500 GB threshold expressed as 0.5 TB — reuses _threshold_gb (no reimpl).
    _active_sub_with_usage(db_session, catalog_offer, used_gb=600.0, email="f@x.io")
    _active_sub_with_usage(db_session, catalog_offer, used_gb=400.0, email="g@x.io")

    result = preview_rule_impact(
        db_session,
        str(catalog_offer.id),
        threshold_amount="0.5",
        threshold_unit="tb",
        consumption_period="monthly",
    )

    assert result["threshold_gb"] == 512.0
    assert result["matched_count"] == 1


def test_preview_ignores_inactive_subscribers(db_session, catalog_offer):
    over = _active_sub_with_usage(
        db_session, catalog_offer, used_gb=150.0, email="h@x.io"
    )
    # Flip one sub to a non-active status: it must drop out of both counts.
    over.status = SubscriptionStatus.suspended
    db_session.commit()
    _active_sub_with_usage(db_session, catalog_offer, used_gb=150.0, email="i@x.io")

    result = preview_rule_impact(
        db_session,
        str(catalog_offer.id),
        threshold_amount="100",
        threshold_unit="gb",
    )

    assert result["total_active_on_offer"] == 1
    assert result["matched_count"] == 1


def test_preview_scan_cap_marks_capped(db_session, catalog_offer):
    for i in range(3):
        _active_sub_with_usage(
            db_session, catalog_offer, used_gb=150.0, email=f"cap{i}@x.io"
        )

    result = preview_rule_impact(
        db_session,
        str(catalog_offer.id),
        threshold_amount="100",
        threshold_unit="gb",
        scan_cap=2,
    )

    assert result["total_active_on_offer"] == 3
    assert result["scanned_count"] == 2
    assert result["capped"] is True
    assert result["matched_count"] == 2  # only scanned subs are counted


def test_preview_rejects_non_positive_threshold(db_session, catalog_offer):
    for bad in ["0", "-5", "abc", ""]:
        result = preview_rule_impact(
            db_session, str(catalog_offer.id), threshold_amount=bad
        )
        assert "error" in result
        assert "matched_count" not in result
