"""Splynx fup_limits → UsageAllowance cap import."""

from __future__ import annotations

from decimal import Decimal

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    UsageAllowance,
)
from app.services.migrations.sync_usage_caps_from_splynx import import_usage_caps

_GIB = 1024**3


def _offer(db, name, code, splynx_tariff_id):
    o = CatalogOffer(
        name=name,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
        splynx_tariff_id=splynx_tariff_id,
    )
    db.add(o)
    db.flush()
    return o


def _limit(tariff_id, *, gb=0, action="block", fixed_down=0):
    return {
        "tariff_id": tariff_id,
        "traffic_amount": int(gb * _GIB),
        "action": action,
        "fixed_down": fixed_down,
        "bonus_is_unlimited": "1" if gb == 0 else "0",
    }


def test_imports_caps_and_links_offers(db_session):
    capped = _offer(db_session, "20GB data", "p20", 5)
    throttled = _offer(db_session, "Capped 200", "p200", 56)
    _offer(db_session, "Unlimited Elite", "elite", 17)  # uncapped tariff
    db_session.commit()

    rows = [
        _limit(5, gb=20),  # 20 GB block
        _limit(56, gb=200, action="decrease", fixed_down=2048),  # throttle to 2 Mbps
        _limit(17, gb=0),  # uncapped → skipped
        _limit(999, gb=50),  # no matching offer
    ]
    summary = import_usage_caps(db_session, rows)
    assert summary == {"capped": 2, "uncapped_skipped": 1, "no_offer": 1}

    db_session.refresh(capped)
    allowance = db_session.get(UsageAllowance, capped.usage_allowance_id)
    assert allowance.included_gb == 20
    assert allowance.throttle_rate_mbps is None  # block, not throttle

    db_session.refresh(throttled)
    thr = db_session.get(UsageAllowance, throttled.usage_allowance_id)
    assert thr.included_gb == 200
    assert thr.throttle_rate_mbps == 2  # 2048 kbps → 2 Mbps


def test_cap_import_is_idempotent(db_session):
    offer = _offer(db_session, "20GB data", "p20b", 5)
    db_session.commit()

    import_usage_caps(db_session, [_limit(5, gb=20)])
    first_id = offer.usage_allowance_id
    assert first_id is not None
    allowance_count = db_session.query(UsageAllowance).count()

    # re-run with a changed cap — same allowance row, updated value
    import_usage_caps(db_session, [_limit(5, gb=40)])
    db_session.refresh(offer)
    assert offer.usage_allowance_id == first_id
    assert db_session.query(UsageAllowance).count() == allowance_count
    assert Decimal(
        str(db_session.get(UsageAllowance, first_id).included_gb)
    ) == Decimal("40.00")
