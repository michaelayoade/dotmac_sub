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
from app.services.network.ont_provisioning.context import resolve_olt_context
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_status_transitions import (
    set_authorization_status,
    set_provisioning_status,
)

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

        Wraps the OLT protocol adapter with DB state tracking.
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
        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("authorize", False, err, critical=True)

        from app.services.network.olt_profile_resolution import (
            ensure_ont_service_profile_match,
            resolve_authorization_profiles_from_db,
        )
        from app.services.network.olt_protocol_adapters import get_protocol_adapter
        from app.services.network.ont_bundle_assignments import resolve_assigned_bundle

        if line_profile_id is None or service_profile_id is None:
            profiles_ok, profiles_msg, profiles = (
                resolve_authorization_profiles_from_db(
                    db,
                    ctx.olt,
                    profile=resolve_assigned_bundle(db, ctx.ont),
                )
            )
            if not profiles_ok or profiles is None:
                return StepResult("authorize", False, profiles_msg, critical=True)
            line_profile_id = profiles.line_profile_id
            service_profile_id = profiles.service_profile_id

        auth_result = get_protocol_adapter(ctx.olt).authorize_ont(
            ctx.fsp,
            ctx.ont.serial_number,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
        )
        ok = auth_result.success
        msg = auth_result.message
        olt_ont_id = auth_result.ont_id

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_authorized,
            )

            set_authorization_status(
                ctx.ont, OntAuthorizationStatus.pending, strict=False
            )
            if olt_ont_id is not None:
                ctx.ont.external_id = str(olt_ont_id)

            verification = verify_ont_authorized(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                if olt_ont_id is not None:
                    match_ok, match_msg = ensure_ont_service_profile_match(
                        ctx.olt,
                        fsp=ctx.fsp,
                        ont_id=olt_ont_id,
                    )
                    if not match_ok:
                        set_provisioning_status(
                            ctx.ont,
                            OntProvisioningStatus.drift_detected,
                            strict=False,
                        )
                        db.flush()
                        return StepResult("authorize", False, match_msg, critical=True)
                set_authorization_status(
                    ctx.ont, OntAuthorizationStatus.authorized, strict=False
                )
                db.flush()
                logger.info(
                    "ONT %s authorized on OLT %s (ONT-ID %s)",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    olt_ont_id,
                )
            else:
                set_provisioning_status(
                    ctx.ont,
                    OntProvisioningStatus.drift_detected,
                    strict=False,
                )
                db.flush()
                logger.warning(
                    "ONT %s authorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult(
                    "authorize", False, verification.message, critical=True
                )
        else:
            logger.warning(
                "ONT %s authorization failed on OLT %s: %s",
                ctx.ont.serial_number,
                ctx.olt.name,
                msg,
            )

        return StepResult("authorize", ok, msg, critical=True)

    @staticmethod
    def deauthorize(db: Session, ont_id: str) -> StepResult:
        """Remove an ONT registration from its OLT.

        Wraps the OLT protocol adapter with DB state tracking.
        Sets ``authorization_status = unauthorized`` on success.

        Args:
            db: Database session.
            ont_id: OntUnit primary key.

        Returns:
            StepResult with success/failure details.
        """
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("deauthorize", False, err, critical=True)

        deauth_result = get_protocol_adapter(ctx.olt).deauthorize_ont(
            ctx.fsp,
            ctx.olt_ont_id,
        )
        ok = deauth_result.success
        msg = deauth_result.message

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_absent,
            )

            set_authorization_status(
                ctx.ont, OntAuthorizationStatus.pending, strict=False
            )
            verification = verify_ont_absent(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=ctx.olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                set_authorization_status(
                    ctx.ont, OntAuthorizationStatus.deauthorized, strict=False
                )
                db.flush()
                logger.info(
                    "ONT %s deauthorized from OLT %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                )
            else:
                set_provisioning_status(
                    ctx.ont,
                    OntProvisioningStatus.drift_detected,
                    strict=False,
                )
                db.flush()
                logger.warning(
                    "ONT %s deauthorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult(
                    "deauthorize", False, verification.message, critical=True
                )
        else:
            logger.warning(
                "ONT %s deauthorization failed on OLT %s: %s",
                ctx.ont.serial_number,
                ctx.olt.name,
                msg,
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
            "check_status",
            True,
            f"Current authorization status: {status_str}",
            critical=False,
        )


# Singleton
ont_authorization = OntAuthorizationService()
