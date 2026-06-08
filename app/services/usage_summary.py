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

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription
from app.models.usage import QuotaBucket, RadiusAccountingSession

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


def _truncate(ts: datetime, bucket: str) -> datetime:
    ts = ts.astimezone(UTC)
    if bucket == "minute":
        return ts.replace(second=0, microsecond=0)
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)  # day


def _integrate(points: list[tuple[datetime, float, float]], bucket: str) -> dict:
    """Integrate a throughput series (sample_at, rx_bps, tx_bps) into bytes per
    bucket. Volume in each segment = avg bits/s / 8 * elapsed seconds, attributed
    to the bucket of the segment's start.

    Gaps far larger than the series' typical spacing are treated as idle and
    skipped (see _GAP_MULTIPLE) — relative to spacing so regularly-spaced coarse
    series (hourly VM buckets, sparse interims) are kept rather than dropped.
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

    for i in range(len(points) - 1):
        t0, rx, tx = points[i]
        dt = (points[i + 1][0] - t0).total_seconds()
        if dt <= 0 or dt > cap:
            continue
        seg_bytes = (rx + tx) / 8.0 * dt
        key = _truncate(t0, bucket)
        out[key] = out.get(key, 0.0) + seg_bytes
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
        (_as_utc(r.sample_at), float(r.rx_bps or 0), float(r.tx_bps or 0))
        for r in rows
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
        {"bucket_start": k, "bytes": int(round(v))}
        for k, v in sorted(buckets.items())
    ]


async def get_usage_summary(
    db: Session, subscriber_id: str, period: str, now: datetime | None = None
) -> dict:
    """Build the usage summary for one window. ``period`` must be in PERIODS."""
    now = now or datetime.now(UTC)
    sub_ids = _subscription_ids(db, subscriber_id)

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
            start = min(_as_utc(b.period_start) for b in buckets)
            end = min(min(_as_utc(b.period_end) for b in buckets), now)
            used_gb = sum(float(b.used_gb or 0) for b in buckets)
            total = int(used_gb * _GB_BYTES)
            total_source, authoritative = "quota", True
        else:
            # No rated cycle on file — approximate a 30-day window from sessions.
            start, end = now - timedelta(days=30), now
            total = _session_octets(db, sub_ids, since=start)
            total_source, authoritative = "sessions", False
        series = _integrate(await _vm_points(db, sub_ids, start, end), "day")
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
        start = now.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end, bucket = now, "hour"
        points = _raw_samples(db, sub_ids, start, end)
    else:  # week
        start, end, bucket = now - timedelta(days=7), now, "day"
        points = await _vm_points(db, sub_ids, start, end)

    series = _integrate(points, bucket)
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
