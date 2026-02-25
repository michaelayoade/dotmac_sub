"""Service helpers for ONT chart web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.olt_polling import get_signal_thresholds
from app.services.network.ont_metrics import (
    ChartData,
    get_signal_history,
    get_traffic_history,
)

logger = logging.getLogger(__name__)


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

    signal_chart = get_signal_history(ont.serial_number, time_range)
    traffic_chart = get_traffic_history(ont.serial_number, time_range)

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
