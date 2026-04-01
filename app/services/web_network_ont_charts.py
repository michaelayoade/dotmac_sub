"""Service helpers for ONT chart web routes."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OntAssignment, OntUnit
from app.services.network.olt_polling import get_signal_thresholds
from app.services.network.ont_metrics import (
    ChartData,
    ChartSeries,
    get_signal_history,
    get_traffic_history,
)

logger = logging.getLogger(__name__)
_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


_RANGE_HOURS: dict[str, int] = {
    "6h": 6,
    "24h": 24,
    "7d": 168,
    "30d": 720,
}


def _build_signal_fallback_from_ont(ont: OntUnit, time_range: str) -> ChartData:
    """Build a one-point signal chart from current ONT snapshot fields."""
    timestamp = getattr(ont, "signal_updated_at", None)
    if timestamp is None:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No signal history data available for this ONT.",
        )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    series = []
    if getattr(ont, "onu_rx_signal_dbm", None) is not None:
        series.append(
            ChartSeries(
                label="ONU Rx (dBm)",
                timestamps=[ts],
                values=[float(ont.onu_rx_signal_dbm)],
            )
        )
    if getattr(ont, "olt_rx_signal_dbm", None) is not None:
        series.append(
            ChartSeries(
                label="OLT Rx (dBm)",
                timestamps=[ts],
                values=[float(ont.olt_rx_signal_dbm)],
            )
        )

    if not series:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No signal history data available for this ONT.",
        )

    return ChartData(
        series=series,
        time_range=time_range,
        available=True,
        error="Showing latest signal snapshot while historical series are unavailable.",
    )


def _build_traffic_fallback_from_bandwidth_samples(
    db: Session, ont: OntUnit, time_range: str
) -> ChartData:
    """Build traffic chart from sampled subscription bandwidth when VM counters are missing."""
    assignment_stmt = (
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True))
        .order_by(OntAssignment.assigned_at.desc(), OntAssignment.created_at.desc())
        .limit(1)
    )
    assignment = db.scalars(assignment_stmt).first()
    if not assignment or not assignment.subscriber_id:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No active subscriber assignment was found for fallback sampling."
            ),
        )

    # Find active subscription for this subscriber to lookup bandwidth samples
    subscription_stmt = (
        select(Subscription)
        .where(
            Subscription.subscriber_id == assignment.subscriber_id,
            Subscription.status == SubscriptionStatus.active,
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = db.scalars(subscription_stmt).first()
    if not subscription:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No active subscription found for the assigned subscriber."
            ),
        )

    now = datetime.now(UTC)
    start = now - timedelta(hours=_RANGE_HOURS.get(time_range, 24))
    sample_stmt = (
        select(BandwidthSample)
        .where(
            BandwidthSample.subscription_id == subscription.id,
            BandwidthSample.sample_at >= start,
            BandwidthSample.sample_at <= now,
        )
        .order_by(BandwidthSample.sample_at.asc())
    )
    samples = db.scalars(sample_stmt).all()
    if not samples:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No bandwidth_samples exist for the assigned subscription in the selected time range."
            ),
        )

    timestamps: list[str] = []
    rx_values: list[float] = []
    tx_values: list[float] = []
    for sample in samples:
        sample_at = sample.sample_at
        if sample_at.tzinfo is None:
            sample_at = sample_at.replace(tzinfo=UTC)
        timestamps.append(sample_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        rx_values.append(float(sample.rx_bps or 0))
        tx_values.append(float(sample.tx_bps or 0))

    return ChartData(
        series=[
            ChartSeries(
                label="Download (bps)", timestamps=timestamps, values=rx_values
            ),
            ChartSeries(label="Upload (bps)", timestamps=timestamps, values=tx_values),
        ],
        time_range=time_range,
        available=True,
        error="Showing subscription bandwidth_samples fallback while VM traffic counters are unavailable.",
    )


def _build_empty_traffic_snapshot(time_range: str, reason: str) -> ChartData:
    """Render a one-point zeroed traffic chart when live series are absent."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ChartData(
        series=[
            ChartSeries(label="Download (bps)", timestamps=[timestamp], values=[0.0]),
            ChartSeries(label="Upload (bps)", timestamps=[timestamp], values=[0.0]),
        ],
        time_range=time_range,
        available=True,
        error=reason,
    )


def _query_vm_range(
    query: str, start: datetime, end: datetime, step: str
) -> list[dict]:
    """Execute a PromQL range query against VictoriaMetrics."""
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{_VM_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "step": step,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("VictoriaMetrics traffic query failed: %s", exc)
        return []
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("result", [])


def _vm_result_to_series(result: dict, label: str) -> ChartSeries:
    timestamps: list[str] = []
    values: list[float | None] = []
    for ts, val in result.get("values", []):
        dt = datetime.fromtimestamp(float(ts), tz=UTC)
        timestamps.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            values.append(None)
    return ChartSeries(label=label, timestamps=timestamps, values=values)


def _build_traffic_from_vm_subscription_aggregates(
    db: Session, ont: OntUnit, time_range: str
) -> ChartData:
    """Build traffic chart from VM subscription aggregates for assigned ONTs."""
    assignment_stmt = (
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True))
        .order_by(OntAssignment.assigned_at.desc(), OntAssignment.created_at.desc())
        .limit(1)
    )
    assignment = db.scalars(assignment_stmt).first()
    if not assignment or not assignment.subscriber_id:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No active subscriber assignment was found for aggregate lookup."
            ),
        )

    # Find active subscription for this subscriber
    subscription_stmt = (
        select(Subscription)
        .where(
            Subscription.subscriber_id == assignment.subscriber_id,
            Subscription.status == SubscriptionStatus.active,
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = db.scalars(subscription_stmt).first()
    if not subscription:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No active subscription found for the assigned subscriber."
            ),
        )

    now = datetime.now(UTC)
    start = now - timedelta(hours=_RANGE_HOURS.get(time_range, 24))
    step = {
        "6h": "2m",
        "24h": "5m",
        "7d": "30m",
        "30d": "2h",
    }.get(time_range, "5m")
    subscription_id = str(subscription.id)

    rx_query = f'bandwidth_rx_bps_avg{{subscription_id="{subscription_id}"}}'
    tx_query = f'bandwidth_tx_bps_avg{{subscription_id="{subscription_id}"}}'
    rx_results = _query_vm_range(rx_query, start, now, step)
    tx_results = _query_vm_range(tx_query, start, now, step)

    series: list[ChartSeries] = []
    if rx_results:
        series.append(_vm_result_to_series(rx_results[0], "Download (bps)"))
    if tx_results:
        series.append(_vm_result_to_series(tx_results[0], "Upload (bps)"))

    if not series:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available for this ONT. "
                "No bandwidth_rx_bps_avg/bandwidth_tx_bps_avg series exist for the assigned subscription."
            ),
        )

    return ChartData(
        series=series,
        time_range=time_range,
        available=True,
        error=(
            "Showing subscription aggregate traffic series "
            "(bandwidth_rx_bps_avg / bandwidth_tx_bps_avg)."
        ),
    )


def charts_tab_data(
    db: Session,
    ont_id: str,
    time_range: str = "24h",
) -> dict[str, object]:
    """Build context for the Charts tab partial template.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        time_range: Time range string (6h, 24h, 7d, 30d).

    Returns:
        Template context dict with chart data.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {
            "signal_chart": ChartData(error="ONT not found."),
            "traffic_chart": ChartData(error="ONT not found."),
            "time_range": time_range,
            "ont_id": ont_id,
        }

    # Validate time range
    valid_ranges = {"6h", "24h", "7d", "30d"}
    if time_range not in valid_ranges:
        time_range = "24h"

    signal_chart = get_signal_history(
        ont.serial_number,
        time_range,
        ont_id=str(ont.id),
    )
    if not signal_chart.available or not signal_chart.series:
        signal_chart = _build_signal_fallback_from_ont(ont, time_range)
    traffic_chart = get_traffic_history(
        ont.serial_number,
        time_range,
        ont_id=str(ont.id),
    )
    if not traffic_chart.available or not traffic_chart.series:
        traffic_chart = _build_traffic_from_vm_subscription_aggregates(
            db, ont, time_range
        )
    if not traffic_chart.available or not traffic_chart.series:
        traffic_chart = _build_traffic_fallback_from_bandwidth_samples(
            db, ont, time_range
        )
    if not traffic_chart.available:
        existing_error = (traffic_chart.error or "").strip() or (
            "No live traffic history data is available for this ONT yet."
        )
        traffic_chart = _build_empty_traffic_snapshot(
            time_range,
            f"{existing_error} Showing a zeroed live placeholder until traffic metrics arrive.",
        )

    # Get thresholds for chart reference lines
    warn_thresh, crit_thresh = get_signal_thresholds(db)

    return {
        "signal_chart": signal_chart,
        "traffic_chart": traffic_chart,
        "time_range": time_range,
        "ont_id": ont_id,
        "warn_threshold": warn_thresh,
        "crit_threshold": crit_thresh,
    }
