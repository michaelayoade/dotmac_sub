"""Import FUP policies/rules from Splynx ``fup_policies`` into FupPolicy/FupRule.

The simple per-plan monthly cap is migrated separately (UsageAllowance). This
brings the *enforcement* rules — Splynx ``fup_policies`` (a JSON ``conditions``
array of monthly/daily/time thresholds with a block/decrease action) — into our
``FupPolicy`` (one per offer) + ``FupRule`` (one per condition), which the FUP
engine (``evaluate_fup_rules``) actually evaluates to throttle/block.

Mapped to the offer via ``splynx_tariff_id``. Idempotent: one policy per offer,
rules keyed by a deterministic name (re-run updates in place).
"""

from __future__ import annotations

import json
import logging
from datetime import time as dtime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer
from app.models.fup import (
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupPolicy,
    FupRule,
)

logger = logging.getLogger(__name__)

_ACTION = {"block": FupAction.block, "decrease": FupAction.reduce_speed}
_DIRECTION = {
    "updown": FupDirection.up_down,
    "up_down": FupDirection.up_down,
    "up": FupDirection.up,
    "down": FupDirection.down,
}
_UNIT = {"mb": FupDataUnit.mb, "gb": FupDataUnit.gb, "tb": FupDataUnit.tb}
_PERIOD = {
    "monthly": FupConsumptionPeriod.monthly,
    "daily": FupConsumptionPeriod.daily,
    "weekly": FupConsumptionPeriod.weekly,
}


def _parse_time(value: object) -> dtime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw == "24:00":
        raw = "23:59"
    try:
        hh, mm = raw.split(":")[:2]
        return dtime(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _rules_from_conditions(
    conditions: object, action: FupAction, percent: float | None
):
    try:
        conds = json.loads(conditions) if isinstance(conditions, str) else conditions
    except (json.JSONDecodeError, TypeError):
        return
    for cond in conds or []:
        ctype = str(cond.get("type") or "").lower()
        direction = _DIRECTION.get(str(cond.get("direction") or "").lower())
        if direction is None:
            direction = FupDirection.up_down
        if ctype in _PERIOD:
            unit = _UNIT.get(str(cond.get("unit") or "gb").lower(), FupDataUnit.gb)
            try:
                amount = float(cond.get("amount") or 0)
            except (ValueError, TypeError):
                amount = 0.0
            yield {
                "consumption_period": _PERIOD[ctype],
                "direction": direction,
                "threshold_amount": amount,
                "threshold_unit": unit,
                "action": action,
                "speed_reduction_percent": percent,
                "time_start": None,
                "time_end": None,
            }
        elif ctype == "time":
            yield {
                "consumption_period": FupConsumptionPeriod.daily,
                "direction": direction,
                "threshold_amount": 0.0,
                "threshold_unit": FupDataUnit.gb,
                "action": action,
                "speed_reduction_percent": percent,
                "time_start": _parse_time(cond.get("from")),
                "time_end": _parse_time(cond.get("to")),
            }


def import_fup_policies(db: Session, rows: list[dict], *, commit: bool = True) -> dict:
    summary = {"policies": 0, "rules": 0, "no_offer": 0, "skipped": 0}
    for row in rows:
        action = _ACTION.get(str(row.get("action") or "").lower())
        if action is None:
            summary["skipped"] += 1
            continue
        offer = db.scalars(
            select(CatalogOffer).where(
                CatalogOffer.splynx_tariff_id == row.get("tariff_id")
            )
        ).first()
        if offer is None:
            summary["no_offer"] += 1
            continue

        try:
            percent: float | None = float(row.get("percent") or 0) or None
        except (ValueError, TypeError):
            percent = None

        rule_dicts = list(
            _rules_from_conditions(row.get("conditions"), action, percent)
        )
        if not rule_dicts:
            summary["skipped"] += 1
            continue

        policy = db.scalars(
            select(FupPolicy).where(FupPolicy.offer_id == offer.id)
        ).first()
        if policy is None:
            policy = FupPolicy(
                offer_id=offer.id, is_active=True, notes="Imported from Splynx"
            )
            db.add(policy)
            db.flush()
        summary["policies"] += 1

        for idx, rd in enumerate(rule_dicts):
            name = (
                f"{rd['consumption_period'].value}-"
                f"{rd['threshold_amount']:g}{rd['threshold_unit'].value}-"
                f"{rd['action'].value}"
            )[:120]
            rule = db.scalars(
                select(FupRule).where(
                    FupRule.policy_id == policy.id, FupRule.name == name
                )
            ).first()
            if rule is None:
                rule = FupRule(policy_id=policy.id, name=name, sort_order=idx)
                db.add(rule)
            rule.consumption_period = rd["consumption_period"]
            rule.direction = rd["direction"]
            rule.threshold_amount = rd["threshold_amount"]
            rule.threshold_unit = rd["threshold_unit"]
            rule.action = rd["action"]
            rule.speed_reduction_percent = rd["speed_reduction_percent"]
            rule.time_start = rd["time_start"]
            rule.time_end = rd["time_end"]
            rule.is_active = True
            summary["rules"] += 1

    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("splynx_fup_policy_import_complete", extra={"summary": summary})
    return summary


def sync_fup_policies_from_splynx(db: Session, *, commit: bool = True) -> dict:
    """Fetch Splynx fup_policies and import them. Requires the Splynx env."""
    from app.services.migrations.db_connections import fetch_all, splynx_connection

    with splynx_connection() as conn:
        rows = fetch_all(conn, "SELECT * FROM fup_policies")
    return import_fup_policies(db, list(rows), commit=commit)
