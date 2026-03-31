"""ONT authorization service — OLT serial registration with DB state tracking.

This module handles the single atomic action of registering (or removing)
an ONT serial on an OLT port. It wraps the raw SSH functions with DB
updates to ``OntUnit.authorization_status``.

Authorization is decoupled from provisioning: authorizing an ONT registers
it on the OLT and assigns an ONT-ID, but does NOT configure service-ports,
management IP, TR-069, or PPPoE. Those are provisioning steps the operator
triggers separately.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.network import (
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntUnit,
)
from app.services.network.ont_provision_steps import StepResult

logger = logging.getLogger(__name__)


class OntAuthorizationService:
    """Manages ONT authorization lifecycle on OLT devices."""

    @staticmethod
    def authorize(
        db: Session,
        ont_id: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> StepResult:
        """Register an ONT serial on its assigned OLT.

        Wraps ``olt_ssh.authorize_ont()`` with DB state tracking.
        Sets ``authorization_status = authorized`` on success.

        Does NOT trigger any provisioning steps (service-ports, TR-069,
        PPPoE, etc.) — those are handled independently.

        Args:
            db: Database session.
            ont_id: OntUnit primary key.
            line_profile_id: OLT line profile ID for authorization.
            service_profile_id: OLT service profile ID for authorization.

        Returns:
            StepResult with success/failure details.
        """
        from app.services.network.ont_provision_steps import resolve_olt_context

        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("authorize", False, err, critical=True)

        from app.services.network.olt_ssh import authorize_ont as ssh_authorize

        ok, msg, olt_ont_id = ssh_authorize(
            ctx.olt,
            ctx.fsp,
            ctx.ont.serial_number,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
        )

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_authorized,
            )

            ctx.ont.authorization_status = OntAuthorizationStatus.pending
            if olt_ont_id is not None:
                ctx.ont.external_id = str(olt_ont_id)

            verification = verify_ont_authorized(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                ctx.ont.authorization_status = OntAuthorizationStatus.authorized
                db.flush()
                logger.info(
                    "ONT %s authorized on OLT %s (ONT-ID %s)",
                    ctx.ont.serial_number, ctx.olt.name, olt_ont_id,
                )
            else:
                ctx.ont.provisioning_status = OntProvisioningStatus.drift_detected
                db.flush()
                logger.warning(
                    "ONT %s authorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult("authorize", False, verification.message, critical=True)
        else:
            logger.warning(
                "ONT %s authorization failed on OLT %s: %s",
                ctx.ont.serial_number, ctx.olt.name, msg,
            )

        return StepResult("authorize", ok, msg, critical=True)

    @staticmethod
    def deauthorize(db: Session, ont_id: str) -> StepResult:
        """Remove an ONT registration from its OLT.

        Wraps ``olt_ssh_ont.delete_ont_registration()`` with DB state tracking.
        Sets ``authorization_status = unauthorized`` on success.

        Args:
            db: Database session.
            ont_id: OntUnit primary key.

        Returns:
            StepResult with success/failure details.
        """
        from app.services.network.olt_ssh_ont import delete_ont_registration
        from app.services.network.ont_provision_steps import resolve_olt_context

        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("deauthorize", False, err, critical=True)

        ok, msg = delete_ont_registration(ctx.olt, ctx.fsp, ctx.olt_ont_id)

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_absent,
            )

            ctx.ont.authorization_status = OntAuthorizationStatus.pending
            verification = verify_ont_absent(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=ctx.olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                ctx.ont.authorization_status = OntAuthorizationStatus.unauthorized
                db.flush()
                logger.info(
                    "ONT %s deauthorized from OLT %s",
                    ctx.ont.serial_number, ctx.olt.name,
                )
            else:
                ctx.ont.provisioning_status = OntProvisioningStatus.drift_detected
                db.flush()
                logger.warning(
                    "ONT %s deauthorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult("deauthorize", False, verification.message, critical=True)
        else:
            logger.warning(
                "ONT %s deauthorization failed on OLT %s: %s",
                ctx.ont.serial_number, ctx.olt.name, msg,
            )

        return StepResult("deauthorize", ok, msg, critical=True)

    @staticmethod
    def check_status(db: Session, ont_id: str) -> StepResult:
        """Query the OLT to verify the ONT's current authorization state.

        Returns:
            StepResult with the current authorization state in the message.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return StepResult("check_status", False, "ONT not found", critical=False)

        current = ont.authorization_status
        status_str = current.value if current else "unknown"
        return StepResult(
            "check_status", True,
            f"Current authorization status: {status_str}",
            critical=False,
        )


# Singleton
ont_authorization = OntAuthorizationService()
