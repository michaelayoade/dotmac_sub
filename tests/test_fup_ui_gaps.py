"""Regression tests for FUP admin UI form wiring."""

from datetime import time

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData

from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.models.fup import FupRule
from app.schemas.catalog import CatalogOfferCreate, OfferVersionCreate
from app.services import catalog as catalog_service
from app.services.fup import fup_policies
from app.services.web_fup import handle_add_rule, handle_update_rule


def _create_offer(db_session, *, name: str, code: str):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name=name,
            code=code,
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=1,
            name=f"{name} v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    return offer


def test_handle_add_rule_respects_active_checkbox(db_session, catalog_offer):
    form = FormData(
        [
            ("name", "Disabled Draft Rule"),
            ("consumption_period", "monthly"),
            ("direction", "up_down"),
            ("threshold_amount", "100"),
            ("threshold_unit", "gb"),
            ("action", "notify"),
        ]
    )

    handle_add_rule(db_session, str(catalog_offer.id), form)

    rules = fup_policies.list_rules(
        db_session,
        str(fup_policies.get_or_create(db_session, str(catalog_offer.id)).id),
    )
    assert len(rules) == 1
    assert rules[0].is_active is False


def test_handle_update_rule_persists_time_chain_and_days(db_session, catalog_offer):
    policy = fup_policies.get_or_create(db_session, str(catalog_offer.id))
    parent = fup_policies.add_rule(
        db_session,
        str(policy.id),
        name="Warning",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=80,
        threshold_unit="gb",
        action="notify",
    )
    child = fup_policies.add_rule(
        db_session,
        str(policy.id),
        name="Throttle",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=100,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
    )

    form = FormData(
        [
            ("name", "Throttle"),
            ("consumption_period", "monthly"),
            ("direction", "up_down"),
            ("threshold_amount", "100"),
            ("threshold_unit", "gb"),
            ("action", "reduce_speed"),
            ("speed_reduction_percent", "60"),
            ("time_start", "22:00"),
            ("time_end", "06:00"),
            ("enabled_by_rule_id", str(parent.id)),
            ("days_of_week", "0"),
            ("days_of_week", "1"),
            ("is_active", "on"),
        ]
    )

    handle_update_rule(db_session, str(child.id), form)

    refreshed = db_session.get(FupRule, child.id)
    assert refreshed is not None
    assert refreshed.speed_reduction_percent == 60
    assert refreshed.time_start == time(22, 0)
    assert refreshed.time_end == time(6, 0)
    assert str(refreshed.enabled_by_rule_id) == str(parent.id)
    assert refreshed.days_of_week == [0, 1]


def test_clone_rules_preserves_extended_rule_fields(db_session):
    source_offer = _create_offer(db_session, name="Source FUP Plan", code="SRC-FUP")
    target_offer = _create_offer(db_session, name="Target FUP Plan", code="TGT-FUP")

    source_policy = fup_policies.get_or_create(db_session, str(source_offer.id))
    target_policy = fup_policies.get_or_create(db_session, str(target_offer.id))

    warning = fup_policies.add_rule(
        db_session,
        str(source_policy.id),
        name="Warning",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=80,
        threshold_unit="gb",
        action="notify",
        time_start=time(8, 0),
        time_end=time(18, 0),
        days_of_week=[0, 1, 2, 3, 4],
        is_active=False,
    )
    db_session.refresh(warning)

    throttle = fup_policies.add_rule(
        db_session,
        str(source_policy.id),
        name="Throttle",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=100,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=40,
        enabled_by_rule_id=str(warning.id),
    )
    throttle.cooldown_minutes = 30
    db_session.commit()

    cloned = fup_policies.clone_rules_from(
        db_session, str(source_offer.id), str(target_policy.id)
    )

    assert len(cloned) == 2

    cloned_by_name = {rule.name: rule for rule in cloned}
    cloned_warning = cloned_by_name["Warning"]
    cloned_throttle = cloned_by_name["Throttle"]

    assert cloned_warning.time_start == time(8, 0)
    assert cloned_warning.time_end == time(18, 0)
    assert cloned_warning.days_of_week == [0, 1, 2, 3, 4]
    assert cloned_warning.is_active is False
    assert cloned_throttle.cooldown_minutes == 30
    assert str(cloned_throttle.enabled_by_rule_id) == str(cloned_warning.id)


def _rule_form(threshold: str, **overrides) -> FormData:
    fields = {
        "name": "Guard Rule",
        "consumption_period": "monthly",
        "direction": "up_down",
        "threshold_amount": threshold,
        "threshold_unit": "gb",
        "action": "notify",
    }
    fields.update(overrides)
    return FormData(list(fields.items()))


@pytest.mark.parametrize("bad", ["1O0", "", "0", "-5", "nan", "inf"])
def test_add_rule_rejects_non_positive_threshold(db_session, catalog_offer, bad):
    # A typo must 400, never silently coerce to a 0-GB threshold that would
    # throttle/block every customer on the offer.
    with pytest.raises(HTTPException) as exc:
        handle_add_rule(db_session, str(catalog_offer.id), _rule_form(bad))
    assert exc.value.status_code == 400
    policy = fup_policies.get_or_create(db_session, str(catalog_offer.id))
    assert fup_policies.list_rules(db_session, str(policy.id)) == []


def test_update_rule_rejects_bad_threshold(db_session, catalog_offer):
    handle_add_rule(db_session, str(catalog_offer.id), _rule_form("100"))
    policy = fup_policies.get_or_create(db_session, str(catalog_offer.id))
    rule = fup_policies.list_rules(db_session, str(policy.id))[0]

    with pytest.raises(HTTPException) as exc:
        handle_update_rule(
            db_session, str(rule.id), FormData([("threshold_amount", "2O0")])
        )
    assert exc.value.status_code == 400
    db_session.refresh(rule)
    assert float(rule.threshold_amount) == 100.0


def test_add_rule_rejects_out_of_range_speed_reduction(db_session, catalog_offer):
    with pytest.raises(HTTPException) as exc:
        handle_add_rule(
            db_session,
            str(catalog_offer.id),
            _rule_form("100", action="reduce_speed", speed_reduction_percent="150"),
        )
    assert exc.value.status_code == 400


def test_add_rule_rejects_non_numeric_sort_order(db_session, catalog_offer):
    with pytest.raises(HTTPException) as exc:
        handle_add_rule(
            db_session,
            str(catalog_offer.id),
            _rule_form("100", sort_order="first"),
        )
    assert exc.value.status_code == 400
