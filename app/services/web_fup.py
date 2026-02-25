"""Web service helpers for FUP (Fair Usage Policy) configuration UI."""

from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING

from fastapi import Request
from starlette.datastructures import FormData

from app.models.fup import (
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
)
from app.services import catalog as catalog_service
from app.services.fup import fup_policies

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Display labels for enum values
CONSUMPTION_PERIOD_LABELS = {
    "monthly": "Monthly",
    "daily": "Daily",
    "weekly": "Weekly",
}

DIRECTION_LABELS = {
    "up": "Upload",
    "down": "Download",
    "up_down": "Upload + Download",
}

DATA_UNIT_LABELS = {
    "mb": "MB",
    "gb": "GB",
    "tb": "TB",
}

ACTION_LABELS = {
    "reduce_speed": "Reduce Speed",
    "block": "Block",
    "notify": "Notify Only",
}

DAY_NAMES = [
    (0, "Mon"),
    (1, "Tue"),
    (2, "Wed"),
    (3, "Thu"),
    (4, "Fri"),
    (5, "Sat"),
    (6, "Sun"),
]


def fup_context(request: Request, db: Session, offer_id: str) -> dict:
    """Build template context for the FUP configuration page.

    Args:
        request: The incoming HTTP request.
        db: Database session.
        offer_id: The catalog offer UUID.

    Returns:
        Dict of template context variables.
    """
    offer = catalog_service.offers.get(db=db, offer_id=offer_id)
    policy = fup_policies.get_or_create(db, offer_id)

    # Fetch all active offers for the clone dropdown (exclude current offer)
    all_offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status="active",
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    other_offers = [o for o in all_offers if str(o.id) != str(offer.id)]

    return {
        "offer": offer,
        "fup_policy": policy,
        "consumption_periods": [
            {"value": e.value, "label": CONSUMPTION_PERIOD_LABELS[e.value]}
            for e in FupConsumptionPeriod
        ],
        "directions": [
            {"value": e.value, "label": DIRECTION_LABELS[e.value]}
            for e in FupDirection
        ],
        "data_units": [
            {"value": e.value, "label": DATA_UNIT_LABELS[e.value]}
            for e in FupDataUnit
        ],
        "actions": [
            {"value": e.value, "label": ACTION_LABELS[e.value]}
            for e in FupAction
        ],
        "day_names": DAY_NAMES,
        "other_offers": other_offers,
        "consumption_period_labels": CONSUMPTION_PERIOD_LABELS,
        "direction_labels": DIRECTION_LABELS,
        "data_unit_labels": DATA_UNIT_LABELS,
        "action_labels": ACTION_LABELS,
    }


def _parse_time(value: str) -> time | None:
    """Parse HH:MM string into a time object, returning None on failure.

    Args:
        value: Time string in HH:MM format.

    Returns:
        A time object or None.
    """
    value = value.strip()
    if not value:
        return None
    try:
        parts = value.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        logger.warning("Invalid time value: %s", value)
        return None


def _parse_days_of_week(form: FormData, field_name: str) -> list[int] | None:
    """Extract multi-value day-of-week checkboxes from form data.

    Args:
        form: The submitted form data.
        field_name: The checkbox field name.

    Returns:
        List of day numbers (0-6) or None if none selected.
    """
    values = form.getlist(field_name)
    if not values:
        return None
    days: list[int] = []
    for v in values:
        try:
            day = int(v)
            if 0 <= day <= 6:
                days.append(day)
        except (ValueError, TypeError):
            continue
    return days if days else None


def handle_policy_update(db: Session, offer_id: str, form: FormData) -> None:
    """Update FUP policy accounting settings from form data.

    Args:
        db: Database session.
        offer_id: The catalog offer UUID.
        form: The submitted form data.
    """
    policy = fup_policies.get_or_create(db, offer_id)

    traffic_start = _parse_time(str(form.get("traffic_accounting_start", "")))
    traffic_end = _parse_time(str(form.get("traffic_accounting_end", "")))
    traffic_inverse = form.get("traffic_inverse_interval") == "on"
    traffic_days = _parse_days_of_week(form, "traffic_days_of_week")

    online_start = _parse_time(str(form.get("online_accounting_start", "")))
    online_end = _parse_time(str(form.get("online_accounting_end", "")))
    online_inverse = form.get("online_inverse_interval") == "on"
    online_days = _parse_days_of_week(form, "online_days_of_week")

    fup_policies.update_policy(
        db,
        str(policy.id),
        traffic_accounting_start=traffic_start,
        traffic_accounting_end=traffic_end,
        traffic_inverse_interval=traffic_inverse,
        traffic_days_of_week=traffic_days,
        online_accounting_start=online_start,
        online_accounting_end=online_end,
        online_inverse_interval=online_inverse,
        online_days_of_week=online_days,
    )
    logger.info("Updated FUP policy settings for offer %s", offer_id)


def handle_add_rule(db: Session, offer_id: str, form: FormData) -> None:
    """Add a new FUP rule from form data.

    Args:
        db: Database session.
        offer_id: The catalog offer UUID.
        form: The submitted form data.
    """
    policy = fup_policies.get_or_create(db, offer_id)

    name = str(form.get("name", "")).strip()
    consumption_period = str(form.get("consumption_period", "monthly"))
    direction = str(form.get("direction", "up_down"))
    threshold_amount_raw = str(form.get("threshold_amount", "0"))
    threshold_unit = str(form.get("threshold_unit", "gb"))
    action = str(form.get("action", "reduce_speed"))
    speed_reduction_raw = str(form.get("speed_reduction_percent", ""))

    try:
        threshold_amount = float(threshold_amount_raw)
    except ValueError:
        threshold_amount = 0.0

    speed_reduction_percent: float | None = None
    if action == "reduce_speed" and speed_reduction_raw:
        try:
            speed_reduction_percent = float(speed_reduction_raw)
        except ValueError:
            speed_reduction_percent = None

    fup_policies.add_rule(
        db,
        str(policy.id),
        name=name,
        consumption_period=consumption_period,
        direction=direction,
        threshold_amount=threshold_amount,
        threshold_unit=threshold_unit,
        action=action,
        speed_reduction_percent=speed_reduction_percent,
    )
    logger.info("Added FUP rule for offer %s", offer_id)


def handle_update_rule(db: Session, rule_id: str, form: FormData) -> None:
    """Update an existing FUP rule from form data.

    Args:
        db: Database session.
        rule_id: The FUP rule UUID.
        form: The submitted form data.
    """
    kwargs: dict = {}

    name = str(form.get("name", "")).strip()
    if name:
        kwargs["name"] = name

    consumption_period = str(form.get("consumption_period", ""))
    if consumption_period:
        kwargs["consumption_period"] = consumption_period

    direction = str(form.get("direction", ""))
    if direction:
        kwargs["direction"] = direction

    threshold_amount_raw = str(form.get("threshold_amount", ""))
    if threshold_amount_raw:
        try:
            kwargs["threshold_amount"] = float(threshold_amount_raw)
        except ValueError:
            pass

    threshold_unit = str(form.get("threshold_unit", ""))
    if threshold_unit:
        kwargs["threshold_unit"] = threshold_unit

    action = str(form.get("action", ""))
    if action:
        kwargs["action"] = action

    speed_reduction_raw = str(form.get("speed_reduction_percent", ""))
    if action == "reduce_speed" and speed_reduction_raw:
        try:
            kwargs["speed_reduction_percent"] = float(speed_reduction_raw)
        except ValueError:
            pass
    elif action and action != "reduce_speed":
        kwargs["speed_reduction_percent"] = None

    is_active = form.get("is_active")
    kwargs["is_active"] = is_active == "on" or is_active == "true"

    fup_policies.update_rule(db, rule_id, **kwargs)
    logger.info("Updated FUP rule %s", rule_id)


def handle_delete_rule(db: Session, rule_id: str) -> None:
    """Delete an FUP rule.

    Args:
        db: Database session.
        rule_id: The FUP rule UUID.
    """
    fup_policies.delete_rule(db, rule_id)
    logger.info("Deleted FUP rule %s", rule_id)


def handle_clone_rules(
    db: Session, source_offer_id: str, target_offer_id: str
) -> None:
    """Clone FUP rules from one offer to another.

    Args:
        db: Database session.
        source_offer_id: The offer UUID to copy rules from.
        target_offer_id: The offer UUID to copy rules into.
    """
    target_policy = fup_policies.get_or_create(db, target_offer_id)
    fup_policies.clone_rules_from(db, source_offer_id, str(target_policy.id))
    logger.info(
        "Cloned FUP rules from offer %s to offer %s",
        source_offer_id,
        target_offer_id,
    )
