"""Optical budget validation for ONT provisioning.

Validates that ONT optical power readings are within acceptable ranges
before provisioning. This prevents provisioning ONTs with degraded fiber
connections that would result in unreliable service.

GPON Class B+ specifications:
- Minimum RX power (sensitivity): -28.0 dBm
- Maximum RX power (overload): -8.0 dBm
- Recommended margin: >= 3.0 dB above sensitivity
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.network import OntUnit

logger = logging.getLogger(__name__)

# GPON Class B+ specifications
MIN_RX_DBM = -28.0  # Sensitivity threshold
MAX_RX_DBM = -8.0  # Overload threshold
RECOMMENDED_MARGIN_DB = 3.0  # Minimum recommended margin above sensitivity
WARNING_MARGIN_DB = 5.0  # Margin below which we emit a warning


@dataclass
class OpticalBudgetResult:
    """Result of optical budget validation.

    Attributes:
        is_valid: True if optical budget is acceptable for provisioning.
        rx_power_dbm: The ONT's reported RX power (None if not available).
        margin_db: Margin above sensitivity threshold (None if not calculable).
        message: Human-readable description of the result.
        is_warning: True if valid but with a low margin warning.
    """

    is_valid: bool
    rx_power_dbm: float | None = None
    margin_db: float | None = None
    message: str = ""
    is_warning: bool = False

    @property
    def status(self) -> str:
        """Return a short status string."""
        if self.rx_power_dbm is None:
            return "unknown"
        if not self.is_valid:
            return "failed"
        if self.is_warning:
            return "warning"
        return "ok"


def validate_optical_budget(ont: OntUnit) -> OpticalBudgetResult:
    """Validate ONT optical power is within acceptable range.

    Checks:
    1. RX power is above sensitivity threshold (-28.0 dBm)
    2. RX power is below overload threshold (-8.0 dBm)
    3. Warns if margin is below recommended threshold (3.0 dB)

    Args:
        ont: The ONT to validate.

    Returns:
        OpticalBudgetResult with validation outcome.
    """
    # Get RX power reading
    rx_power = getattr(ont, "onu_rx_signal_dbm", None)

    # No reading available - allow provisioning but note uncertainty
    if rx_power is None:
        return OpticalBudgetResult(
            is_valid=True,
            rx_power_dbm=None,
            margin_db=None,
            message="No optical reading available. Provisioning allowed but signal quality is unknown.",
            is_warning=False,
        )

    # Convert to float if needed
    try:
        rx = float(rx_power)
    except (TypeError, ValueError):
        return OpticalBudgetResult(
            is_valid=True,
            rx_power_dbm=None,
            margin_db=None,
            message=f"Invalid optical reading format: {rx_power}. Provisioning allowed.",
            is_warning=True,
        )

    # Check for overload (too strong signal)
    if rx > MAX_RX_DBM:
        return OpticalBudgetResult(
            is_valid=False,
            rx_power_dbm=rx,
            margin_db=None,
            message=f"Optical overload: {rx:.1f} dBm exceeds maximum {MAX_RX_DBM:.1f} dBm. "
            f"Check for attenuator or fiber bend issues.",
            is_warning=False,
        )

    # Check for signal below sensitivity
    if rx < MIN_RX_DBM:
        return OpticalBudgetResult(
            is_valid=False,
            rx_power_dbm=rx,
            margin_db=rx - MIN_RX_DBM,  # Negative margin
            message=f"Below sensitivity: {rx:.1f} dBm is below minimum {MIN_RX_DBM:.1f} dBm. "
            f"Check fiber path for damage or excessive loss.",
            is_warning=False,
        )

    # Calculate margin
    margin = rx - MIN_RX_DBM

    # Check if margin is critically low (but still valid)
    if margin < RECOMMENDED_MARGIN_DB:
        return OpticalBudgetResult(
            is_valid=True,
            rx_power_dbm=rx,
            margin_db=margin,
            message=f"Low margin warning: {rx:.1f} dBm has only {margin:.1f} dB margin. "
            f"Recommended minimum is {RECOMMENDED_MARGIN_DB:.1f} dB. "
            f"Service may be unreliable during adverse conditions.",
            is_warning=True,
        )

    # Check if margin warrants a minor warning
    if margin < WARNING_MARGIN_DB:
        return OpticalBudgetResult(
            is_valid=True,
            rx_power_dbm=rx,
            margin_db=margin,
            message=f"Signal OK with {margin:.1f} dB margin. "
            f"Consider fiber path inspection if issues arise.",
            is_warning=True,
        )

    # Good signal
    return OpticalBudgetResult(
        is_valid=True,
        rx_power_dbm=rx,
        margin_db=margin,
        message=f"Optical signal OK: {rx:.1f} dBm with {margin:.1f} dB margin.",
        is_warning=False,
    )


def check_optical_budget_for_provisioning(
    ont: OntUnit,
    *,
    allow_low_margin: bool = False,
    allow_no_reading: bool = True,
) -> tuple[bool, str]:
    """Check if ONT optical budget allows provisioning.

    This is a convenience wrapper around validate_optical_budget() that
    returns a simple (success, message) tuple suitable for preflight checks.

    Args:
        ont: The ONT to validate.
        allow_low_margin: If True, allow provisioning with low margin warning.
        allow_no_reading: If True, allow provisioning when no reading is available.

    Returns:
        Tuple of (can_proceed, message).
    """
    result = validate_optical_budget(ont)

    # No reading
    if result.rx_power_dbm is None:
        if allow_no_reading:
            return True, result.message
        return False, "Optical reading required but not available"

    # Failed validation
    if not result.is_valid:
        return False, result.message

    # Valid with warning
    if result.is_warning and not allow_low_margin:
        return (
            False,
            f"Low optical margin: {result.message}. Set allow_low_margin=True to override.",
        )

    return True, result.message


def format_optical_status(ont: OntUnit) -> str:
    """Format ONT optical status for display.

    Args:
        ont: The ONT to check.

    Returns:
        Human-readable optical status string.
    """
    result = validate_optical_budget(ont)

    if result.rx_power_dbm is None:
        return "No reading"

    status_icon = {
        "ok": "\u2713",  # ✓
        "warning": "\u26a0",  # ⚠
        "failed": "\u2717",  # ✗
        "unknown": "?",
    }.get(result.status, "?")

    if result.margin_db is not None:
        return f"{status_icon} {result.rx_power_dbm:.1f} dBm ({result.margin_db:+.1f} dB margin)"

    return f"{status_icon} {result.rx_power_dbm:.1f} dBm"
