"""Compatibility facade for reconciled ONT remote-access changes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.network.ont_action_common import ActionResult, get_ont_or_error


def set_wan_remote_access(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    protocol: str = "ssh",
    port: int | None = None,
) -> ActionResult:
    """Route legacy callers through desired state and verified readback."""
    if protocol not in {"ssh", "telnet"}:
        return ActionResult(
            success=False, message="Protocol must be 'ssh' or 'telnet'."
        )
    if port not in (None, 22 if protocol == "ssh" else 23):
        return ActionResult(
            success=False,
            message="Custom WAN management ports are not supported by policy.",
        )
    if protocol == "telnet" and enabled:
        return ActionResult(
            success=False,
            message="WAN Telnet is prohibited; use the time-bounded SSH control.",
        )
    if protocol == "ssh":
        from app.services.network.ont_features import OntFeatureService

        return OntFeatureService.toggle_wan_remote_access(db, ont_id, enabled=enabled)

    ont, error = get_ont_or_error(db, ont_id)
    if error or ont is None:
        return error or ActionResult(success=False, message="ONT not found.")

    from app.services.network.reconcile import reconcile_ont
    from app.services.network.reconcile.adapters import desired_from_ont_unit

    desired = desired_from_ont_unit(db, ont)
    result = reconcile_ont(
        db,
        ont_id,
        proposed_change={
            "wan_remote_access_enabled": desired.wan_remote_access_enabled,
            "wan_remote_access_expires_at": desired.wan_remote_access_expires_at,
            "wan_remote_access_source_cidrs": desired.wan_remote_access_source_cidrs,
        },
        mode="sync",
    )
    return ActionResult(
        success=result.success,
        message=(
            "WAN Telnet disabled and verified."
            if result.success
            else (
                result.failure.message
                if result.failure
                else "Remote-access reconcile failed."
            )
        ),
        data={"sync_status": result.sync_status},
    )


def set_wan_remote_access_best_effort(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    protocol: str = "ssh",
    port: int | None = None,
) -> ActionResult:
    """Retain the old API name without bypassing verification."""
    return set_wan_remote_access(
        db,
        ont_id,
        enabled=enabled,
        protocol=protocol,
        port=port,
    )
