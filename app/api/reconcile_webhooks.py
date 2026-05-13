"""GenieACS event webhooks → reconcile_ont.

The reconciler's mode-gating treats certain device events as triggers for a
full configuration re-push (the ``bootstrap`` mode). The canonical case:
GenieACS sees a ``0 BOOTSTRAP`` event, meaning the ONT has just started a
fresh CWMP session — typically after a factory reset that wiped the
configured state.

GenieACS supports webhooks via the ``request`` provision builtin. A small
preset on the GenieACS side does, in pseudo-JS::

    if (event === "0 BOOTSTRAP") {
      request({
        url: "http://dotmac:8000/api/v1/reconcile/webhooks/genieacs/bootstrap",
        method: "POST",
        body: JSON.stringify({"device_id": ID})
      });
    }

This module receives that POST, resolves the device_id to an ``OntUnit``
serial via the last segment of the GenieACS id (``OUI-ProductClass-Serial``),
and runs ``reconcile_ont(mode="bootstrap")``. The reconcile happens
synchronously — GenieACS doesn't care how long the response takes (it
fire-and-forgets), so the operator-visible outcome lives on the
``OntUnit.sync_status`` field rather than in the webhook response.

Auth: the GenieACS instance lives inside the same WireGuard tunnel as the
DotMac app, and the webhook URL is configured per-preset on the
GenieACS side. The route accepts unauthenticated POSTs in the same way
``tr069_inform`` does — the network boundary is the security model.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OntUnit
from app.services.network.reconcile import (
    ReconcileFailureReason,
    reconcile_ont,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reconcile/webhooks", tags=["reconcile-webhooks"])


class GenieACSBootstrapPayload(BaseModel):
    """GenieACS preset POSTs this shape.

    ``device_id`` is the GenieACS internal id: ``{OUI}-{ProductClass}-{Serial}``.
    ``event`` is informational — we trust the preset to fire only on
    BOOTSTRAP, but we log it so an out-of-order or misconfigured preset is
    visible.
    """

    device_id: str
    event: str | None = None


def _serial_from_device_id(device_id: str) -> str | None:
    """Extract the trailing serial from a GenieACS device id.

    GenieACS ids are ``OUI-ProductClass-Serial`` — the serial is everything
    after the last ``-``. Returns None if the format doesn't match.
    """
    if not device_id or "-" not in device_id:
        return None
    return device_id.rsplit("-", 1)[-1]


@router.post(
    "/genieacs/bootstrap",
    summary="GenieACS BOOTSTRAP event → reconcile_ont(mode=bootstrap)",
)
def genieacs_bootstrap_webhook(
    payload: GenieACSBootstrapPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Receive a ``0 BOOTSTRAP`` event from GenieACS and reconcile the ONT.

    Returns a small JSON descriptor of the reconcile outcome. The webhook
    response is informational only — GenieACS doesn't act on it.
    """
    serial = _serial_from_device_id(payload.device_id)
    if not serial:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract serial from device_id={payload.device_id!r}",
        )

    ont = db.execute(
        select(OntUnit).where(OntUnit.serial_number == serial)
    ).scalar_one_or_none()
    if ont is None:
        # GenieACS knows about an ONT we don't have in our inventory yet.
        # Common during autofind: the device informs before the operator
        # authorizes it. Returning 200 (not 404) so GenieACS doesn't keep
        # retrying — the operator will pick it up via the autofind UI.
        logger.info(
            "genieacs_bootstrap_unknown_serial",
            extra={"device_id": payload.device_id, "serial": serial},
        )
        return {
            "status": "ignored",
            "reason": "unknown_serial",
            "device_id": payload.device_id,
        }

    logger.info(
        "genieacs_bootstrap_triggered",
        extra={
            "device_id": payload.device_id,
            "serial": serial,
            "ont_unit_id": str(ont.id),
            "event": payload.event,
        },
    )

    result = reconcile_ont(
        db,
        ont.id,
        proposed_change=None,
        mode="bootstrap",
    )

    return {
        "status": "ok" if result.success else "failed",
        "device_id": payload.device_id,
        "ont_unit_id": str(ont.id),
        "sync_status": result.sync_status,
        "actions_applied": [a.field for a in result.actions_applied],
        "failure_reason": (result.failure.reason if result.failure else None),
        # ACS_CR_FAILED carries operator-actionable instructions — flag it
        # so operator dashboards can surface the recovery hint.
        "actionable": (
            result.failure is not None
            and result.failure.reason == ReconcileFailureReason.ACS_CR_FAILED
        ),
    }
