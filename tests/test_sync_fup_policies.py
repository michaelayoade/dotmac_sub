"""Splynx fup_policies → FupPolicy/FupRule import."""

from __future__ import annotations

import json
from datetime import time

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
)
from app.models.fup import FupAction, FupConsumptionPeriod, FupPolicy, FupRule
from app.services.migrations.sync_fup_policies_from_splynx import import_fup_policies


def _offer(db, code, splynx_tariff_id):
    o = CatalogOffer(
        name=code,
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


def _policy(id, tariff_id, action, conditions, *, percent=0):
    return {
        "id": id,
        "tariff_id": tariff_id,
        "action": action,
        "percent": percent,
        "conditions": json.dumps(conditions),
    }


def test_imports_volume_and_time_rules(db_session):
    o14 = _offer(db_session, "10GB cap", 14)
    o56 = _offer(db_session, "Capped 200", 56)
    o57 = _offer(db_session, "Night cap", 57)
    db_session.commit()

    rows = [
        _policy(
            15,
            14,
            "block",
            [{"type": "monthly", "direction": "updown", "amount": "10", "unit": "gb"}],
        ),
        _policy(
            42,
            56,
            "decrease",
            [
                {"type": "daily", "direction": "updown", "amount": "18", "unit": "gb"},
                {
                    "type": "monthly",
                    "direction": "updown",
                    "amount": "200",
                    "unit": "gb",
                },
            ],
            percent=50,
        ),
        _policy(
            43,
            57,
            "decrease",
            [{"type": "time", "from": "08:00", "to": "18:00"}],
            percent=10,
        ),
        _policy(
            99, 999, "block", [{"type": "monthly", "amount": "5", "unit": "gb"}]
        ),  # no offer
    ]
    summary = import_fup_policies(db_session, rows)
    assert summary == {"policies": 3, "rules": 4, "no_offer": 1, "skipped": 0}

    # 14: a single monthly block rule
    p14 = db_session.query(FupPolicy).filter_by(offer_id=o14.id).one()
    r14 = db_session.query(FupRule).filter_by(policy_id=p14.id).one()
    assert r14.consumption_period == FupConsumptionPeriod.monthly
    assert r14.threshold_amount == 10.0
    assert r14.action == FupAction.block

    # 56: two decrease rules (daily + monthly) at 50%
    p56 = db_session.query(FupPolicy).filter_by(offer_id=o56.id).one()
    rules56 = db_session.query(FupRule).filter_by(policy_id=p56.id).all()
    assert len(rules56) == 2
    assert all(r.action == FupAction.reduce_speed for r in rules56)
    assert all(r.speed_reduction_percent == 50.0 for r in rules56)

    # 57: a time-window throttle rule
    p57 = db_session.query(FupPolicy).filter_by(offer_id=o57.id).one()
    r57 = db_session.query(FupRule).filter_by(policy_id=p57.id).one()
    assert r57.time_start == time(8, 0)
    assert r57.time_end == time(18, 0)


def test_fup_import_is_idempotent(db_session):
    offer = _offer(db_session, "10GB cap", 14)
    db_session.commit()
    row = _policy(
        15,
        14,
        "block",
        [{"type": "monthly", "direction": "updown", "amount": "10", "unit": "gb"}],
    )

    import_fup_policies(db_session, [row])
    policy_count = db_session.query(FupPolicy).count()
    rule_count = db_session.query(FupRule).count()

    import_fup_policies(db_session, [row])  # re-run
    assert db_session.query(FupPolicy).count() == policy_count
    assert db_session.query(FupRule).count() == rule_count
