"""Read-only preview of the Wi-Fi security floor (WPA2-PSK, AES).

Counts, from stored desired state and the last stored ACS observation only (no
device I/O, no writes), how many ONTs the floor would change if
``ont_wifi_security_floor_enforced`` were switched on. Run it before enabling.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.ont_observation import OntObservation

from .actions import AcsSetWifiConfig
from .adapters import desired_from_ont_unit, observed_from_ont_observation
from .candidates import restrict_to_reconcile_candidates
from .planner import compute_plan


@dataclass(frozen=True, slots=True)
class WifiFloorPreview:
    examined: int
    would_change: int
    needs_psk_skipped: int
    already_meets_floor_or_unmanaged: int
    no_observation: int
    unreadable: int


def preview_wifi_security_floor(db: Session) -> WifiFloorPreview:
    counts: Counter[str] = Counter()
    onts = db.scalars(restrict_to_reconcile_candidates(select(OntUnit))).all()
    for ont in onts:
        counts["examined"] += 1
        row = db.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
        ).first()
        observed = observed_from_ont_observation(row)
        if observed is None:
            counts["no_observation"] += 1
            continue
        try:
            desired = replace(
                desired_from_ont_unit(db, ont), wifi_security_floor_enforced=True
            )
        except Exception:  # noqa: BLE001 - an unread ONT is reported, not counted
            counts["unreadable"] += 1
            continue
        plan = compute_plan(desired, observed, "sweep", force_proposed_writes=False)
        security_drift = [d for d in plan.drifts if d.field == "wifi_security_mode"]
        if not security_drift:
            counts["ok"] += 1
        elif any(
            isinstance(a, AcsSetWifiConfig) and a.security_mode for a in plan.actions
        ):
            counts["would_change"] += 1
        else:
            counts["needs_psk"] += 1
    return WifiFloorPreview(
        examined=counts["examined"],
        would_change=counts["would_change"],
        needs_psk_skipped=counts["needs_psk"],
        already_meets_floor_or_unmanaged=counts["ok"],
        no_observation=counts["no_observation"],
        unreadable=counts["unreadable"],
    )


__all__ = ["WifiFloorPreview", "preview_wifi_security_floor"]
