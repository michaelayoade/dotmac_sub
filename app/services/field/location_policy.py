"""Sub-owned binding from operational settings to positioning policy."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.field.location_tracking import PositionObservationPolicy
from app.services.settings_spec import resolve_integer


def field_location_policy(db: Session) -> PositionObservationPolicy:
    """Resolve product policy without teaching positioning about Sub settings."""

    return PositionObservationPolicy(
        max_batch_size=resolve_integer(
            db,
            SettingDomain.field,
            "location_max_batch_size",
        ),
        max_future_skew=timedelta(
            seconds=resolve_integer(
                db,
                SettingDomain.field,
                "location_max_future_skew_seconds",
            )
        ),
        max_accuracy_m=float(
            resolve_integer(
                db,
                SettingDomain.field,
                "location_max_accuracy_meters",
            )
        ),
    )
