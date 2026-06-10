"""Time-windowed usage summary for the customer self-service API.

Backs ``GET /me/usage-summary``. Combines three data sources to give an
accurate total + a bucketed series per window, working around the fact that
``radius_accounting_sessions`` stores one upserted row per session (cumulative
octets, no intra-session history):

  - sub-day windows (hour/today): integrate the ``BandwidthSample`` throughput
    series (RADIUS interim deltas land here) in Python -> bytes per bucket.
    Done in Python (not SQL ``date_trunc``) so it is storage-agnostic.
  - week/cycle charts: the same throughput series from the bandwidth pipeline,
    which routes to VictoriaMetrics for >24h ranges (Postgres hot retention is
    ~24h).
  - authoritative totals: ``QuotaBucket.used_gb`` for the billing cycle, and the
    sum of session octets for "all". These never depend on interim accounting.

When interim accounting isn't flowing (no ``BandwidthSample`` rows / no metrics
store), sub-day series come back empty and the total falls back to session
octets so the headline is never a misleading zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.models.usage import QuotaBucket, RadiusAccountingSession
from app.timezone import APP_TIMEZONE

logger = logging.getLogger(__name__)

PERIODS = ("hour", "today", "week", "cycle", "all")

# A gap between consecutive points beyond this multiple of the series' typical
# (median) spacing is treated as idle and skipped rather than filled with a flat
# throughput estimate. Relative to spacing — not a fixed cap — so coarse sources
# aren't wrongly dropped: VictoriaMetrics uses an hourly step for >30-day windows
# and RADIUS interim intervals can be 15-30 min, both of which a fixed 15-min cap
# would silently discard (blank chart / undercount).
_GAP_MULTIPLE = 3.0
# ...but never skip gaps shorter than this, so dense (1s/1m) sampling isn't
# fragmented by brief quiet stretches.
_MIN_GAP_CAP_SECONDS = 900
_GB_BYTES = 1024**3


def _subscription_ids(db: Session, subscriber_id: str) -> list:
    return [
        row[0]
        for row in db.query(Subscription.id)
        .filter(Subscription.subscriber_id == subscriber_id)
        .all()
    ]


def _as_utc(dt: datetime | None) -> datetime | None:
    """Treat a stored datetime as UTC. SQLite (tests) returns naive datetimes;
    Postgres returns tz-aware. Our columns store UTC either way."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Customer-facing status + relative severity for picking the worst state when a
# subscriber has more than one subscription.
_FUP_STATUS_MAP = {
    "blocked": ("blocked", 3),
    "throttled": ("throttled", 2),
    "notified": ("full_speed", 1),  # warned, but not yet speed-limited
    "none": ("full_speed", 0),
}


_PERIOD_WORDS = {"daily": "day", "weekly": "week", "monthly": "month"}


def _policy_terms_line(
    *, action: str, threshold_gb: float, reduction: float | None, period: str
) -> str:
    """Customer-readable policy terms, shown even while healthy — e.g.
    "Speed reduced to 25% after 500 GB each month"."""
    period_word = _PERIOD_WORDS.get(period, period)
    limit = f"{threshold_gb:g} GB"
    if action == "block":
        return f"Access pauses after {limit} each {period_word}"
    if reduction is not None:
        kept = max(0.0, 100.0 - reduction)
        return f"Speed reduces to {kept:g}% after {limit} each {period_word}"
    return f"Fair-usage limit applies after {limit} each {period_word}"


def _current_bucket_used_gb(db: Session, sub_id) -> float | None:
    """used_gb of the subscription's current-period quota bucket (no create)."""
    from app.models.usage import QuotaBucket

    now = datetime.now(UTC)
    bucket = (
        db.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == sub_id)
        .filter(QuotaBucket.period_start <= now)
        .filter(QuotaBucket.period_end > now)
        .order_by(QuotaBucket.period_start.desc())
        .first()
    )
    if bucket is None:
        return None
    return float(bucket.used_gb or 0)


def _fup_warn_ratio(db: Session) -> float:
    """Lowest configured usage-warning threshold (same knob the enforcement
    task warns at), default 0.8."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec
    from app.services.usage import _parse_warning_thresholds

    raw = settings_spec.resolve_value(
        db, SettingDomain.usage, "usage_warning_thresholds"
    )
    parsed = _parse_warning_thresholds(str(raw) if raw is not None else None)
    return float(parsed[0]) if parsed else 0.8


def _nearest_enforcement_rule(db: Session, offer_id):
    """The lowest-threshold active throttle/block rule for the offer — the one
    the customer will hit first. None when the offer has no active FUP policy."""
    from app.models.fup import FupAction, FupPolicy, FupRule

    rows = (
        db.query(FupRule)
        .join(FupPolicy, FupPolicy.id == FupRule.policy_id)
        .filter(FupPolicy.offer_id == offer_id)
        .filter(FupPolicy.is_active.is_(True))
        .filter(FupRule.is_active.is_(True))
        .filter(FupRule.action.in_([FupAction.reduce_speed, FupAction.block]))
        .all()
    )
    if not rows:
        return None
    from app.services.fup import _threshold_gb

    return min(rows, key=_threshold_gb)


def fup_summary(db: Session, subscriber_id: str) -> dict | None:
    """Customer-facing Fair-Usage status for the caller's subscriptions.

    Reads the per-subscription ``FupState`` the enforcement engine maintains and
    surfaces the most severe active state. Healthy subscribers still get the
    policy terms plus headroom (threshold_gb / used_gb / gb_until_throttle) so
    the app can pre-warn ("approaching") before enforcement instead of only
    reporting it after. Returns ``None`` when the caller has no subscriptions.
    """
    if db is None:
        return None
    from app.models.fup import FupRule
    from app.models.fup_state import FupState
    from app.services.fup_state import fup_state as fup_state_mgr

    subs = (
        db.query(Subscription.id, Subscription.offer_id)
        .filter(Subscription.subscriber_id == subscriber_id)
        .all()
    )
    if not subs:
        return None

    best: tuple[int, FupState] | None = None  # (severity, state)
    best_sub = None  # (sub_id, offer_id) the state belongs to
    for sub_id, offer_id in subs:
        state = fup_state_mgr.get(db, str(sub_id))
        if state is None:
            continue
        _, severity = _FUP_STATUS_MAP.get(state.action_status.value, ("full_speed", 0))
        if best is None or severity > best[0]:
            best = (severity, state)
            best_sub = (sub_id, offer_id)

    # Policy context (threshold / headroom / terms). When enforced, use the
    # enforced subscription; otherwise the one closest to its threshold.
    from app.services.fup import _threshold_gb as rule_threshold_gb

    context = None  # (ratio, used_gb, rule_row)
    context_candidates = [best_sub] if (best and best[0] >= 2 and best_sub) else subs
    for sub_id, offer_id in context_candidates:
        if offer_id is None:
            continue
        used = _current_bucket_used_gb(db, sub_id)
        rule = _nearest_enforcement_rule(db, offer_id)
        if rule is None:
            continue
        ratio = (used or 0.0) / rule_threshold_gb(rule)
        if context is None or ratio > context[0]:
            context = (ratio, used or 0.0, rule)

    threshold_gb = used_gb = gb_until = usage_ratio = None
    policy_summary = None
    if context is not None:
        usage_ratio, used_gb, rule_row = context
        threshold_gb = rule_threshold_gb(rule_row)
        gb_until = max(0.0, threshold_gb - used_gb)
        policy_summary = _policy_terms_line(
            action=rule_row.action.value,
            threshold_gb=threshold_gb,
            reduction=rule_row.speed_reduction_percent,
            period=rule_row.consumption_period.value,
        )

    if best is None or best[0] < 2:
        status = "full_speed"
        summary = None
        if usage_ratio is not None and usage_ratio >= _fup_warn_ratio(db):
            status = "approaching"
            summary = (
                f"{min(usage_ratio, 1.0):.0%} of your fair-use allowance used "
                f"— {gb_until:g} GB until it applies"
            )
        return {
            "status": status,
            "is_reduced": False,
            "threshold_gb": threshold_gb,
            "used_gb": used_gb,
            "gb_until_throttle": gb_until,
            "usage_ratio": usage_ratio,
            "policy_summary": policy_summary,
            "summary": summary,
        }

    state = best[1]
    status, _ = _FUP_STATUS_MAP.get(state.action_status.value, ("full_speed", 0))
    rule_obj = (
        db.query(FupRule).filter(FupRule.id == state.active_rule_id).first()
        if state.active_rule_id
        else None
    )
    reduction = state.speed_reduction_percent
    summary = None
    if rule_obj is not None:
        period = _PERIOD_WORDS.get(
            rule_obj.consumption_period.value, rule_obj.consumption_period.value
        )
        limit = f"{rule_obj.threshold_amount:g} {rule_obj.threshold_unit.value.upper()}"
        if status == "blocked":
            summary = f"Access paused after {limit} this {period}"
        elif reduction is not None:
            kept = max(0.0, 100.0 - reduction)
            summary = f"Speed reduced to {kept:g}% after {limit} this {period}"
        else:
            summary = f"Fair-usage limit reached after {limit} this {period}"

    return {
        "status": status,
        "is_reduced": status in {"throttled", "blocked"},
        "speed_reduction_percent": reduction,
        "active_rule_name": rule_obj.name if rule_obj is not None else None,
        "resets_at": _as_utc(state.cap_resets_at),
        "summary": summary,
        "threshold_gb": threshold_gb,
        "used_gb": used_gb,
        "gb_until_throttle": gb_until,
        "usage_ratio": usage_ratio,
        "policy_summary": policy_summary,
    }


def _subscriber_tz(db: Session, subscriber_id: str) -> ZoneInfo:
    """The subscriber's timezone, falling back to the deployment default, so
    "today" / daily buckets align to the customer's local day, not UTC."""
    name = db.query(Subscriber.timezone).filter(Subscriber.id == subscriber_id).scalar()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return APP_TIMEZONE


def _truncate(ts: datetime, bucket: str, tz: ZoneInfo) -> datetime:
    """Floor ``ts`` to the bucket boundary in ``tz`` (so days/hours align to the
    subscriber's local clock), returned as the canonical UTC instant.

    DST-safe: the boundary is built as a *naive* local wall time and then
    localized fresh, so its UTC offset is resolved for the boundary itself rather
    than carried over from ``ts`` (which can differ across a transition, e.g. an
    afternoon in DST vs. that day's standard-time midnight). ``fold=0`` collapses
    an ambiguous wall time — the hour repeated at a fall-back transition — to a
    single canonical bucket instead of two same-labelled bars."""
    local = ts.astimezone(tz)
    if bucket == "minute":
        wall = local.replace(second=0, microsecond=0, tzinfo=None)
    elif bucket == "hour":
        wall = local.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    else:  # day
        wall = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return wall.replace(tzinfo=tz, fold=0).astimezone(UTC)


def _integrate(
    points: list[tuple[datetime, float, float]],
    bucket: str,
    tz: ZoneInfo,
    end: datetime | None = None,
) -> dict:
    """Integrate a throughput series (sample_at, rx_bps, tx_bps) into bytes per
    bucket. Volume in each segment = avg bits/s / 8 * elapsed seconds, attributed
    to the bucket of the segment's start.

    Gaps far larger than the series' typical spacing are treated as idle and
    skipped (see _GAP_MULTIPLE) — relative to spacing so regularly-spaced coarse
    series (hourly VM buckets, sparse interims) are kept rather than dropped.

    When ``end`` is given, the last observed rate is carried forward to the
    window end (same gap cap), so the current bucket reflects usage right up to
    "now" instead of stopping at the last sample. The cap means a stale last
    sample (idle/offline) won't fabricate a tail.
    """
    out: dict[datetime, float] = {}
    if len(points) < 2:
        return out

    gaps = sorted(
        dt
        for dt in (
            (points[i + 1][0] - points[i][0]).total_seconds()
            for i in range(len(points) - 1)
        )
        if dt > 0
    )
    typical = gaps[len(gaps) // 2] if gaps else 0.0
    cap = max(typical * _GAP_MULTIPLE, _MIN_GAP_CAP_SECONDS)

    def _add(t0: datetime, rx: float, tx: float, dt: float) -> None:
        if dt <= 0 or dt > cap:
            return
        key = _truncate(t0, bucket, tz)
        out[key] = out.get(key, 0.0) + (rx + tx) / 8.0 * dt

    for i in range(len(points) - 1):
        t0, rx, tx = points[i]
        _add(t0, rx, tx, (points[i + 1][0] - t0).total_seconds())

    if end is not None:
        tl, rx, tx = points[-1]
        _add(tl, rx, tx, (end - tl).total_seconds())
    return out


def _raw_samples(
    db: Session, sub_ids: list, start: datetime, end: datetime
) -> list[tuple[datetime, float, float]]:
    """Raw bandwidth samples for the window, storage-agnostic (no date_trunc)."""
    if not sub_ids:
        return []
    rows = (
        db.query(
            BandwidthSample.sample_at,
            BandwidthSample.rx_bps,
            BandwidthSample.tx_bps,
        )
        .filter(
            BandwidthSample.subscription_id.in_(sub_ids),
            BandwidthSample.sample_at >= start,
            BandwidthSample.sample_at < end,
        )
        .order_by(BandwidthSample.sample_at.asc())
        .all()
    )
    return [
        (dt, float(r.rx_bps or 0), float(r.tx_bps or 0))
        for r in rows
        if (dt := _as_utc(r.sample_at)) is not None
    ]


async def _vm_points(
    db: Session, sub_ids: list, start: datetime, end: datetime
) -> list[tuple[datetime, float, float]]:
    """Throughput points from the bandwidth pipeline (VictoriaMetrics for >24h),
    merged across the subscriber's subscriptions. Best-effort: returns [] if the
    metrics store is unavailable."""
    from app.services.bandwidth import BandwidthSamples

    merged: list[tuple[datetime, float, float]] = []
    for sub_id in sub_ids:
        try:
            result = await BandwidthSamples.get_bandwidth_series(
                db, str(sub_id), start_at=start, end_at=end, interval="auto"
            )
        except Exception as exc:  # pragma: no cover - depends on metrics store
            logger.debug("usage-summary VM series failed for %s: %s", sub_id, exc)
            continue
        for p in result.get("data", []):
            ts = _coerce_ts(p.get("timestamp"))
            if ts is None:
                continue
            merged.append(
                (ts, float(p.get("rx_bps") or 0), float(p.get("tx_bps") or 0))
            )
    merged.sort(key=lambda x: x[0])
    return merged


def _coerce_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _session_octets(db: Session, sub_ids: list, since: datetime | None) -> int:
    if not sub_ids:
        return 0
    q = db.query(
        func.coalesce(func.sum(RadiusAccountingSession.input_octets), 0)
        + func.coalesce(func.sum(RadiusAccountingSession.output_octets), 0)
    ).filter(RadiusAccountingSession.subscription_id.in_(sub_ids))
    if since is not None:
        q = q.filter(RadiusAccountingSession.session_start >= since)
    return int(q.scalar() or 0)


def _current_quota(db: Session, sub_ids: list, now: datetime):
    """The quota buckets covering ``now`` across the subscriber's subscriptions."""
    if not sub_ids:
        return []
    return (
        db.query(QuotaBucket)
        .filter(
            QuotaBucket.subscription_id.in_(sub_ids),
            QuotaBucket.period_start <= now,
            QuotaBucket.period_end >= now,
        )
        .all()
    )


def _series_payload(buckets: dict) -> list[dict]:
    return [
        {"bucket_start": k, "bytes": int(round(v))} for k, v in sorted(buckets.items())
    ]


async def get_usage_summary(
    db: Session, subscriber_id: str, period: str, now: datetime | None = None
) -> dict:
    """Build the usage summary for one window. ``period`` must be in PERIODS."""
    now = now or datetime.now(UTC)
    sub_ids = _subscription_ids(db, subscriber_id)
    tz = _subscriber_tz(db, subscriber_id)

    if period == "all":
        total = _session_octets(db, sub_ids, since=None)
        first = (
            db.query(func.min(RadiusAccountingSession.session_start))
            .filter(RadiusAccountingSession.subscription_id.in_(sub_ids))
            .scalar()
            if sub_ids
            else None
        )
        return {
            "period": period,
            "start": (_as_utc(first) or now),
            "end": now,
            "total_bytes": total,
            "total_source": "sessions",
            "is_authoritative": True,
            "bucket": None,
            "series": [],
        }

    if period == "cycle":
        buckets = _current_quota(db, sub_ids, now)
        if buckets:
            starts = [d for b in buckets if (d := _as_utc(b.period_start)) is not None]
            ends = [d for b in buckets if (d := _as_utc(b.period_end)) is not None]
            start = min(starts) if starts else now
            end = min(min(ends), now) if ends else now
            used_gb = sum(float(b.used_gb or 0) for b in buckets)
            total = int(used_gb * _GB_BYTES)
            total_source, authoritative = "quota", True
        else:
            # No rated cycle on file — approximate a 30-day window from sessions.
            start, end = now - timedelta(days=30), now
            total = _session_octets(db, sub_ids, since=start)
            total_source, authoritative = "sessions", False
        series = _integrate(
            await _vm_points(db, sub_ids, start, end), "day", tz, end=end
        )
        return {
            "period": period,
            "start": start,
            "end": end,
            "total_bytes": total,
            "total_source": total_source,
            "is_authoritative": authoritative,
            "bucket": "day",
            "series": _series_payload(series),
        }

    # Sub-day / week windows — throughput series drives both chart and total.
    if period == "hour":
        start, end, bucket = now - timedelta(hours=1), now, "minute"
        points = _raw_samples(db, sub_ids, start, end)
    elif period == "today":
        # Local midnight in the subscriber's tz, expressed as a UTC instant.
        local_midnight = now.astimezone(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start = local_midnight.astimezone(UTC)
        end, bucket = now, "hour"
        points = _raw_samples(db, sub_ids, start, end)
    else:  # week
        start, end, bucket = now - timedelta(days=7), now, "day"
        points = await _vm_points(db, sub_ids, start, end)

    series = _integrate(points, bucket, tz, end=end)
    total = int(round(sum(series.values())))
    total_source, authoritative = "samples", False
    if total == 0 and not series:
        # Interim accounting / metrics store not flowing — don't report a false
        # zero; fall back to session octets started in the window.
        total = _session_octets(db, sub_ids, since=start)
        total_source = "sessions"
    return {
        "period": period,
        "start": start,
        "end": end,
        "total_bytes": total,
        "total_source": total_source,
        "is_authoritative": authoritative,
        "bucket": bucket,
        "series": _series_payload(series),
    }
