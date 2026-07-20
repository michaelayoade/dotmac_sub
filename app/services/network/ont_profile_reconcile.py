"""Reconciled Huawei ONT profile binding operations."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def reconcile_tr069_profile_binding(
    db: Session,
    ont_id: str,
    profile_id: int,
) -> tuple[bool, str]:
    """Apply and verify one OLT TR-069 profile through desired state."""
    from app.services.network.reconcile import reconcile_ont

    result = reconcile_ont(
        db,
        ont_id,
        proposed_change={"tr069_profile_id": profile_id},
        mode="sync",
        timeout_sec=90,
    )
    if not result.success:
        failure = result.failure
        return False, failure.message if failure else "TR-069 profile reconcile failed."

    message = f"TR-069 profile {profile_id} verified on OLT readback."
    try:
        from app.services.network.ont_provision_steps import wait_tr069_bootstrap

        wait_result = wait_tr069_bootstrap(db, ont_id)
        message = f"{message} {wait_result.message}"
    except Exception as exc:
        logger.warning(
            "Failed to queue TR-069 bootstrap wait after reconciled profile bind "
            "for ONT %s: %s",
            ont_id,
            exc,
        )
        message = f"{message} ACS inform wait could not be queued: {exc}"
    return True, message
