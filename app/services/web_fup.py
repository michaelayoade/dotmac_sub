"""Web service helpers for FUP (Fair Usage Policy) configuration UI."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from sqlalchemy import func
from starlette.datastructures import FormData

from app.models.fup import (
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupRule,
)
from app.services import catalog as catalog_service
from app.services import control_registry
from app.services.common import coerce_uuid, validate_enum
from app.services.fup import _threshold_gb, fup_policies, simulate_fup

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
            {"value": e.value, "label": DIRECTION_LABELS[e.value]} for e in FupDirection
        ],
        "data_units": [
            {"value": e.value, "label": DATA_UNIT_LABELS[e.value]} for e in FupDataUnit
        ],
        "actions": [
            {"value": e.value, "label": ACTION_LABELS[e.value]} for e in FupAction
        ],
        "day_names": DAY_NAMES,
        "other_offers": other_offers,
        "consumption_period_labels": CONSUMPTION_PERIOD_LABELS,
        "direction_labels": DIRECTION_LABELS,
        "data_unit_labels": DATA_UNIT_LABELS,
        "action_labels": ACTION_LABELS,
    }


def redirect_to_fup_context(form: FormData, offer_id: str) -> str:
    """Resolve the post-action return URL for FUP forms."""
    return_to_raw = form.get("return_to")
    if isinstance(return_to_raw, str):
        return_to = return_to_raw.strip()
        if return_to.startswith("/admin/"):
            return return_to
    return f"/admin/catalog/offers/{offer_id}/fup"


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


_SUBMONTHLY_PERIODS = {"daily", "weekly"}


def _guard_submonthly_period(db: Session, consumption_period: str) -> None:
    """Gate daily/weekly FUP rules behind an explicit opt-in (#21 safeguard).

    Sub-monthly usage is samples/VM-derived (not billing-grade) and durable
    period buckets aren't in place yet, so a daily/weekly rule must not silently
    go live in prod. Off by default; ops enables the canonical
    ``usage.fup_submonthly_rules`` feature after validating the metrics source.
    """
    if consumption_period not in _SUBMONTHLY_PERIODS:
        return
    if not control_registry.is_enabled(db, "usage.fup_submonthly_rules"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Daily/weekly FUP rules are gated. Sub-monthly usage is "
                "samples-derived (not billing-grade); enable the "
                "'usage.fup_submonthly_rules' feature in System → Modules "
                "after validating the metrics source."
            ),
        )


def _parse_threshold_amount(raw: str) -> float:
    """Parse a FUP threshold, rejecting anything non-positive.

    A typo must never coerce to 0.0: a zero threshold is instantly exceeded and
    throttles/blocks every customer on the offer.
    """
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Threshold must be a positive number (got {raw!r}). A zero "
                "threshold would throttle or block every customer on the offer."
            ),
        )
    return value


def _parse_speed_reduction(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not 0 < value < 100:
        raise HTTPException(
            status_code=400,
            detail=f"Speed reduction must be a percentage between 1 and 99 (got {raw!r}).",
        )
    return value


def _parse_sort_order(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Sort order must be a whole number (got {raw!r}).",
        ) from None


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
    _guard_submonthly_period(db, consumption_period)
    direction = str(form.get("direction", "up_down"))
    threshold_amount_raw = str(form.get("threshold_amount", "0"))
    threshold_unit = str(form.get("threshold_unit", "gb"))
    action = str(form.get("action", "reduce_speed"))
    speed_reduction_raw = str(form.get("speed_reduction_percent", ""))

    threshold_amount = _parse_threshold_amount(threshold_amount_raw)

    speed_reduction_percent: float | None = None
    if action == "reduce_speed" and speed_reduction_raw:
        speed_reduction_percent = _parse_speed_reduction(speed_reduction_raw)

    # Parse new chaining/time fields
    time_start = _parse_time(str(form.get("time_start", "")))
    time_end = _parse_time(str(form.get("time_end", "")))
    enabled_by_raw = str(form.get("enabled_by_rule_id", "")).strip()
    enabled_by_rule_id = enabled_by_raw if enabled_by_raw else None
    sort_order = _parse_sort_order(str(form.get("sort_order", "0")) or "0")
    days_of_week_raw = form.getlist("days_of_week")
    days_of_week = [int(d) for d in days_of_week_raw if str(d).isdigit()] or None
    is_active = str(form.get("is_active", "")).lower() in {"on", "true", "1"}

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
        sort_order=sort_order,
        time_start=time_start,
        time_end=time_end,
        enabled_by_rule_id=enabled_by_rule_id,
        days_of_week=days_of_week,
        is_active=is_active,
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
        _guard_submonthly_period(db, consumption_period)
        kwargs["consumption_period"] = consumption_period

    direction = str(form.get("direction", ""))
    if direction:
        kwargs["direction"] = direction

    threshold_amount_raw = str(form.get("threshold_amount", ""))
    if threshold_amount_raw:
        kwargs["threshold_amount"] = _parse_threshold_amount(threshold_amount_raw)

    threshold_unit = str(form.get("threshold_unit", ""))
    if threshold_unit:
        kwargs["threshold_unit"] = threshold_unit

    action = str(form.get("action", ""))
    if action:
        kwargs["action"] = action

    speed_reduction_raw = str(form.get("speed_reduction_percent", ""))
    if action == "reduce_speed" and speed_reduction_raw:
        kwargs["speed_reduction_percent"] = _parse_speed_reduction(speed_reduction_raw)
    elif action and action != "reduce_speed":
        kwargs["speed_reduction_percent"] = None

    is_active = form.get("is_active")
    kwargs["is_active"] = is_active == "on" or is_active == "true"

    # Parse chaining/time fields
    time_start_raw = str(form.get("time_start", "")).strip()
    time_end_raw = str(form.get("time_end", "")).strip()
    kwargs["time_start"] = _parse_time(time_start_raw) if time_start_raw else None
    kwargs["time_end"] = _parse_time(time_end_raw) if time_end_raw else None

    enabled_by_raw = str(form.get("enabled_by_rule_id", "")).strip()
    kwargs["enabled_by_rule_id"] = enabled_by_raw if enabled_by_raw else None

    days_of_week_raw = form.getlist("days_of_week")
    kwargs["days_of_week"] = [
        int(d) for d in days_of_week_raw if str(d).isdigit()
    ] or None

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


def handle_clone_rules(db: Session, source_offer_id: str, target_offer_id: str) -> None:
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


def _form_scalar(form: FormData, key: str, default: str) -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def simulate_offer_fup(db: Session, offer_id: str, form: FormData) -> dict[str, object]:
    """Run a FUP simulation from submitted form values."""
    try:
        usage_gb = float(_form_scalar(form, "usage_gb", "0"))
        hour = int(_form_scalar(form, "hour", "12"))
        day = int(_form_scalar(form, "day", "-1"))
        billing_day = int(_form_scalar(form, "billing_day_elapsed", "15"))
        cycle_days = int(_form_scalar(form, "billing_cycle_days", "30"))
    except (ValueError, TypeError):
        return {"error": "Invalid parameters"}

    sim_time = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return simulate_fup(
        db,
        offer_id,
        current_usage_gb=usage_gb,
        current_time=sim_time,
        current_day=day if day >= 0 else None,
        billing_day_elapsed=billing_day,
        billing_cycle_days=cycle_days,
    )


# Bound the preview so a huge offer can't hang the request. We scan at most
# PREVIEW_SCAN_CAP active subscribers and expose whether the true active count
# exceeded that (``capped``), so operators know the count is a sampled floor.
PREVIEW_SCAN_CAP = 500
PREVIEW_SAMPLE_SIZE = 5


def _draft_threshold_gb(threshold_amount: float, threshold_unit: str) -> float:
    """Threshold in GB via the SAME conversion the evaluator uses.

    Builds a transient (unpersisted) ``FupRule`` so ``_threshold_gb`` — the exact
    MB/GB/TB math enforcement applies — does the conversion; we never reimplement it.
    """
    unit = validate_enum(threshold_unit, FupDataUnit, "threshold_unit")
    return _threshold_gb(
        FupRule(threshold_amount=threshold_amount, threshold_unit=unit)
    )


def preview_rule_impact(
    db: Session,
    offer_id: str,
    *,
    threshold_amount: str,
    threshold_unit: str = "gb",
    direction: str = "up_down",
    consumption_period: str = "monthly",
    action: str = "reduce_speed",
    scan_cap: int = PREVIEW_SCAN_CAP,
    sample_size: int = PREVIEW_SAMPLE_SIZE,
) -> dict[str, object]:
    """Read-only blast-radius preview for a draft FUP rule (no side effects).

    Counts how many ACTIVE subscribers on ``offer_id`` already meet/exceed the
    draft threshold in the rule's consumption window RIGHT NOW — i.e. how many
    the rule would immediately throttle/block/notify the moment it is saved. This
    surfaces the known footgun (a bad threshold that hits everyone) before save.

    Reuses the evaluator's threshold conversion and the FUP usage reader; the
    ``>=`` comparison mirrors ``evaluate_rules``. Direction is echoed back but
    -- like enforcement -- does not change the current reading (the window is
    period-based, not per-direction).
    """
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.fup_usage import get_fup_usage_gb_async, period_value

    # Soft-validate the draft params so the UI can render the message inline
    # instead of a hard 4xx (mirrors ``simulate_offer_fup``'s {"error": ...}).
    try:
        amount = float(threshold_amount)
    except (TypeError, ValueError):
        return {"error": f"Threshold must be a number (got {threshold_amount!r})."}
    if not math.isfinite(amount) or amount <= 0:
        return {"error": "Threshold must be a positive number."}
    try:
        validate_enum(threshold_unit, FupDataUnit, "threshold_unit")
        validate_enum(direction, FupDirection, "direction")
        validate_enum(consumption_period, FupConsumptionPeriod, "consumption_period")
        validate_enum(action, FupAction, "action")
    except HTTPException as exc:
        return {"error": exc.detail}

    offer_uid = coerce_uuid(offer_id)
    period = period_value(consumption_period)
    threshold_gb = _draft_threshold_gb(amount, threshold_unit)

    # True total of active subs on the offer (cheap COUNT — never capped).
    total_active = (
        db.query(func.count(Subscription.id))
        .filter(Subscription.offer_id == offer_uid)
        .filter(Subscription.status == SubscriptionStatus.active)
        .scalar()
    ) or 0

    # Bounded scan: evaluate at most ``scan_cap`` subs so a huge offer can't hang.
    subs = (
        db.query(Subscription)
        .filter(Subscription.offer_id == offer_uid)
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.id)
        .limit(scan_cap)
        .all()
    )

    now = datetime.now(UTC)

    if period == "monthly":
        from app.services.usage_summary import _current_bucket_used_gb

        used_values = [
            float(_current_bucket_used_gb(db, sub.id) or 0.0) for sub in subs
        ]
    else:

        async def _resolve() -> list:
            # Sequential (the sync Session is not concurrency-safe) but a single
            # event loop for the whole scan rather than one per subscriber.
            return [
                await get_fup_usage_gb_async(db, sub, period, now=now) for sub in subs
            ]

        windows = asyncio.run(_resolve())
        used_values = [float(window.used_gb or 0.0) for window in windows]

    matched = 0
    sample: list[dict[str, object]] = []
    for sub, used in zip(subs, used_values, strict=True):
        if used >= threshold_gb:
            matched += 1
            if len(sample) < sample_size:
                sample.append(
                    {
                        "subscription_id": str(sub.id),
                        "subscriber_id": str(sub.subscriber_id),
                        "used_gb": round(used, 2),
                    }
                )

    return {
        "matched_count": matched,
        "scanned_count": len(subs),
        "total_active_on_offer": int(total_active),
        "capped": int(total_active) > len(subs),
        "scan_cap": scan_cap,
        "threshold_gb": round(threshold_gb, 4),
        "consumption_period": period,
        "direction": direction,
        "action": action,
        "sample": sample,
    }
