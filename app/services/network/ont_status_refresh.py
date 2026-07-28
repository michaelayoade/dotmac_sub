"""Safe ONT status refresh admission for stale inventory projections."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services import app_cache
from app.services.device_operational_status import derive_ont_operational_status
from app.services.network.ont_runtime_status import (
    huawei_olt_status_pollable,
    queue_huawei_olt_status_poll,
)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_COOLDOWN_SECONDS = 120
DEFAULT_MAX_OLTS_PER_REQUEST = 4


@dataclass(frozen=True)
class OntStatusRefreshAdmission:
    """Summary of stale status refresh requests admitted from a read surface."""

    stale_onts: int = 0
    queued_olts: int = 0
    suppressed_recent_poll: int = 0
    suppressed_recent_request: int = 0
    skipped_non_huawei: int = 0
    skipped_missing_olt: int = 0
    skipped_inactive_ont: int = 0
    queue_errors: int = 0


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recently_polled(olt: OLTDevice, *, now: datetime, cooldown_seconds: int) -> bool:
    last_poll_at = _as_aware_utc(getattr(olt, "last_poll_at", None))
    return last_poll_at is not None and now - last_poll_at < timedelta(
        seconds=max(1, cooldown_seconds)
    )


def _claim_refresh_window(olt_id: str, *, cooldown_seconds: int, now: datetime) -> bool:
    """Best-effort short admission lock for UI-triggered refresh requests."""
    client = app_cache.get_cache_redis()
    if client is None:
        return False
    key = app_cache.cache_key("network", "ont_status_refresh", "olt", olt_id)
    payload = json.dumps({"olt_id": olt_id, "requested_at": now.isoformat()})
    try:
        claimed = client.set(
            key,
            payload,
            ex=max(1, int(cooldown_seconds)),
            nx=True,
        )
    except RedisError as exc:
        logger.debug("ont_status_refresh_claim_failed olt_id=%s error=%s", olt_id, exc)
        return False
    return bool(claimed)


def request_stale_ont_status_refreshes(
    db: Session,
    onts: Iterable[Any],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = DEFAULT_REFRESH_COOLDOWN_SECONDS,
    max_olts: int = DEFAULT_MAX_OLTS_PER_REQUEST,
) -> OntStatusRefreshAdmission:
    """Request safe bulk refreshes for stale ONT status rows.

    The ONT inventory page remains a DB read. This function only admits bounded
    background work for stale rows and never performs SSH or UISP I/O itself.
    Huawei ONTs are refreshed by the existing bulk OLT poller. UISP-managed or
    non-Huawei rows are left to their own scheduled sync sources.
    """
    current = now or datetime.now(UTC)
    stale_onts = 0
    skipped_missing_olt = 0
    skipped_non_huawei = 0
    skipped_inactive_ont = 0
    suppressed_recent_poll = 0
    candidate_olts: dict[str, OLTDevice] = {}

    for ont in onts:
        if not bool(getattr(ont, "is_active", False)):
            skipped_inactive_ont += 1
            continue
        operational = derive_ont_operational_status(ont, now=current)
        if operational.reason not in {
            "verification_not_started",
            "verification_expired",
        }:
            continue
        stale_onts += 1

        olt = getattr(ont, "olt_device", None)
        olt_id = getattr(ont, "olt_device_id", None) or getattr(olt, "id", None)
        if olt is None and olt_id is not None:
            olt = db.get(OLTDevice, olt_id)
        if olt is None or olt_id is None:
            skipped_missing_olt += 1
            continue

        if not huawei_olt_status_pollable(olt):
            skipped_non_huawei += 1
            continue

        if _recently_polled(olt, now=current, cooldown_seconds=cooldown_seconds):
            suppressed_recent_poll += 1
            continue

        candidate_olts.setdefault(str(olt_id), olt)

    queued_olts = 0
    suppressed_recent_request = 0
    queue_errors = 0
    admission_limit = max(0, int(max_olts))
    for olt_id in candidate_olts:
        if queued_olts >= admission_limit:
            break
        if not _claim_refresh_window(
            olt_id, cooldown_seconds=cooldown_seconds, now=current
        ):
            suppressed_recent_request += 1
            continue
        result = queue_huawei_olt_status_poll(
            olt_id, source="network.ont_status_refresh"
        )
        if result.queued:
            queued_olts += 1
        else:
            queue_errors += 1
            logger.warning(
                "ont_status_refresh_queue_failed olt_id=%s error=%s",
                olt_id,
                result.error,
            )

    return OntStatusRefreshAdmission(
        stale_onts=stale_onts,
        queued_olts=queued_olts,
        suppressed_recent_poll=suppressed_recent_poll,
        suppressed_recent_request=suppressed_recent_request,
        skipped_non_huawei=skipped_non_huawei,
        skipped_missing_olt=skipped_missing_olt,
        skipped_inactive_ont=skipped_inactive_ont,
        queue_errors=queue_errors,
    )
