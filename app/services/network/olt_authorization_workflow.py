"""OLT autofind authorization workflow helpers.

The admin OLT service module exposes public wrappers for compatibility, while
the workflow implementation lives here to keep authorization, post-auth follow
up, and ONT record/assignment helpers isolated from broader OLT admin logic.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_assignment_alignment import (
    align_ont_assignment_to_authoritative_fsp,
)

logger = logging.getLogger(__name__)

# Authorization workflow constants — configurable via DomainSettings (provisioning domain).
# Module-level variables kept for backward compatibility with test patching.
from app.services.network.provisioning_settings import (
    DEFAULTS as _PROVISIONING_DEFAULTS,
)
from app.services.network.provisioning_settings import (
    get_autofind_freshness_sec,
    get_force_reauthorize_attempts,
    get_force_reauthorize_retry_delay,
)

FORCE_REAUTHORIZE_AUTOFIND_ATTEMPTS = _PROVISIONING_DEFAULTS.force_reauthorize_autofind_attempts
FORCE_REAUTHORIZE_AUTOFIND_RETRY_DELAY_SECONDS = _PROVISIONING_DEFAULTS.force_reauthorize_retry_delay_sec
AUTOFIND_CANDIDATE_FRESHNESS_SECONDS = _PROVISIONING_DEFAULTS.autofind_candidate_freshness_sec


def _is_serial_already_registered_message(message: str | None) -> bool:
    lowered = str(message or "").lower()
    return "sn already exists" in lowered or "serial already exists" in lowered


@dataclass
class AutofindValidationResult:
    """Result of validating/refreshing autofind candidate data."""

    success: bool
    candidate: object | None
    steps_added: list[tuple[str, bool, str, float]]  # (name, success, message, started_at)
    error_message: str | None = None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _autofind_candidate_is_usable(
    candidate: object | None,
    *,
    require_seen_after: datetime | None = None,
    now: datetime | None = None,
    freshness_seconds: int = AUTOFIND_CANDIDATE_FRESHNESS_SECONDS,
) -> bool:
    """Return whether cached autofind data is recent enough for authorization.

    Cached candidates are the read model for UI and normal authorization. Force
    reauthorize is stricter: after deleting an existing registration, the
    candidate must have been seen after that delete verification.
    """
    if candidate is None:
        return False
    last_seen_at = _as_utc(getattr(candidate, "last_seen_at", None))
    if last_seen_at is None:
        return require_seen_after is None
    required_at = _as_utc(require_seen_after)
    if required_at is not None and last_seen_at < required_at:
        return False
    current_time = now or datetime.now(UTC)
    return last_seen_at >= current_time - timedelta(seconds=freshness_seconds)


def _resolve_authorized_autofind_candidate(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> tuple[bool, str]:
    """Best-effort candidate cleanup after OLT authorization is verified."""
    from app.services import (
        web_network_ont_autofind as web_network_ont_autofind_service,
    )

    try:
        web_network_ont_autofind_service.resolve_candidate_authorized(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
        )
        return True, "Marked the discovered ONT as authorized."
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning(
            "Failed to immediately resolve autofind candidate for %s on %s %s: %s",
            serial_number,
            olt_id,
            fsp,
            exc,
        )
        return False, f"Failed to mark discovered ONT as authorized: {exc}"


def _configure_management_ip_for_authorization(
    db: Session,
    *,
    olt: OLTDevice,
    fsp: str,
    ont_id_on_olt: int,
) -> tuple[bool, str]:
    """Configure ONT management IP/VLAN so it can reach the ACS server.

    Uses the OLT's provisioning profile settings (mgmt_vlan_tag, mgmt_ip_mode)
    to configure management connectivity. This enables the ONT to contact
    the TR-069 ACS after authorization.

    Returns:
        (success, message) tuple. Returns (True, "skipped") if profile has
        no management VLAN configured.
    """
    from sqlalchemy import desc, select

    from app.models.network import OntProvisioningProfile
    from app.services.network.olt_ssh_ont import (
        configure_ont_internet_config,
        configure_ont_iphost,
    )

    # Get the OLT's provisioning profile (is_default first, then most recent)
    stmt = (
        select(OntProvisioningProfile)
        .where(
            OntProvisioningProfile.olt_device_id == olt.id,
            OntProvisioningProfile.is_active.is_(True),
        )
        .order_by(
            desc(OntProvisioningProfile.is_default),
            desc(OntProvisioningProfile.updated_at),
            desc(OntProvisioningProfile.created_at),
        )
    )
    profile = db.scalars(stmt).first()
    if profile is None:
        logger.info(
            "Skipping management IP config for ONT on %s: no provisioning profile found for OLT %s",
            fsp,
            olt.name,
        )
        return True, "Skipped: no provisioning profile configured for this OLT."

    mgmt_vlan_tag = getattr(profile, "mgmt_vlan_tag", None)
    if mgmt_vlan_tag is None:
        logger.info(
            "Skipping management IP config for ONT on %s: profile '%s' has no mgmt_vlan_tag",
            fsp,
            profile.name,
        )
        return True, f"Skipped: profile '{profile.name}' has no management VLAN configured."

    # Determine IP mode (default to DHCP)
    mgmt_ip_mode_raw = getattr(profile, "mgmt_ip_mode", None)
    mgmt_ip_mode = "dhcp"
    if mgmt_ip_mode_raw is not None:
        if hasattr(mgmt_ip_mode_raw, "value"):
            mgmt_ip_mode = mgmt_ip_mode_raw.value
        else:
            mgmt_ip_mode = str(mgmt_ip_mode_raw)

    logger.info(
        "Configuring management IP for ONT on %s %s: VLAN %d, mode %s (profile '%s')",
        olt.name,
        fsp,
        mgmt_vlan_tag,
        mgmt_ip_mode,
        profile.name,
    )

    # Configure management IP via OLT SSH
    iphost_ok, iphost_msg = configure_ont_iphost(
        olt,
        fsp,
        ont_id_on_olt,
        vlan_id=int(mgmt_vlan_tag),
        ip_mode=mgmt_ip_mode,
    )
    if not iphost_ok:
        logger.warning(
            "Management IP config failed for ONT on %s %s: %s",
            olt.name,
            fsp,
            iphost_msg,
        )
        return False, f"Management IP config failed: {iphost_msg}"

    # Activate TCP stack if internet_config_ip_index is set
    internet_config_ip_index = getattr(profile, "internet_config_ip_index", None)
    if internet_config_ip_index is not None:
        logger.info(
            "Activating internet-config for ONT on %s %s: ip-index %d",
            olt.name,
            fsp,
            internet_config_ip_index,
        )
        ic_ok, ic_msg = configure_ont_internet_config(
            olt,
            fsp,
            ont_id_on_olt,
            ip_index=int(internet_config_ip_index),
        )
        if not ic_ok:
            logger.warning(
                "Internet-config activation failed for ONT on %s %s: %s",
                olt.name,
                fsp,
                ic_msg,
            )
            # Continue anyway - iphost config succeeded
            return True, f"{iphost_msg} (internet-config failed: {ic_msg})"
        return True, f"{iphost_msg}; {ic_msg}"

    return True, iphost_msg


def _validate_autofind_candidate(
    db: Session,
    *,
    olt_id: str,
    olt_name: str | None,
    fsp: str,
    serial_number: str,
    require_seen_after: datetime | None,
    autofind_freshness: int,
    deleted_existing: bool,
    reauthorize_attempts: int,
    reauthorize_retry_delay: float,
) -> AutofindValidationResult:
    """Validate autofind candidate, refreshing cache if needed.

    This handles the complex retry logic when force_reauthorize deletes an
    existing registration and we need to wait for the ONT to reappear in autofind.

    Returns:
        AutofindValidationResult with candidate if found, or error details.
    """
    from app.services.web_network_ont_autofind import sync_olt_autofind_candidates

    steps: list[tuple[str, bool, str, float]] = []

    matched_candidate = get_autofind_candidate_by_serial(
        db, olt_id, serial_number, fsp=fsp
    )

    if _autofind_candidate_is_usable(
        matched_candidate,
        require_seen_after=require_seen_after,
        freshness_seconds=autofind_freshness,
    ):
        return AutofindValidationResult(
            success=True,
            candidate=matched_candidate,
            steps_added=steps,
        )

    # Candidate is stale or missing — need to refresh
    if matched_candidate is not None:
        logger.info(
            "Cached autofind candidate is stale for authorization; refreshing olt_id=%s fsp=%s serial=%s last_seen_at=%s require_seen_after=%s",
            olt_id,
            fsp,
            serial_number,
            getattr(matched_candidate, "last_seen_at", None),
            require_seen_after,
        )

    refresh_started_at = monotonic()
    sync_ok, sync_message, _sync_stats = sync_olt_autofind_candidates(db, olt_id)
    if not sync_ok:
        steps.append(("Refresh autofind cache", False, f"Autofind refresh failed: {sync_message}", refresh_started_at))
        return AutofindValidationResult(
            success=False,
            candidate=None,
            steps_added=steps,
            error_message=f"Autofind refresh failed: {sync_message}",
        )
    steps.append(("Refresh autofind cache", True, sync_message, refresh_started_at))

    matched_candidate = get_autofind_candidate_by_serial(
        db, olt_id, serial_number, fsp=fsp
    )
    if _autofind_candidate_is_usable(
        matched_candidate,
        require_seen_after=require_seen_after,
        freshness_seconds=autofind_freshness,
    ):
        return AutofindValidationResult(
            success=True,
            candidate=matched_candidate,
            steps_added=steps,
        )

    # Still not usable — if we deleted an existing registration, retry
    if deleted_existing:
        for attempt in range(2, reauthorize_attempts + 1):
            sleep(reauthorize_retry_delay)
            retry_started_at = monotonic()
            sync_ok, sync_message, _sync_stats = sync_olt_autofind_candidates(db, olt_id)
            if not sync_ok:
                steps.append((
                    "Refresh autofind cache",
                    False,
                    f"Autofind refresh failed while waiting for force rediscovery: {sync_message}",
                    retry_started_at,
                ))
                return AutofindValidationResult(
                    success=False,
                    candidate=None,
                    steps_added=steps,
                    error_message=f"Autofind refresh failed while waiting for force rediscovery: {sync_message}",
                )
            steps.append((
                "Refresh autofind cache",
                True,
                f"Retry {attempt}/{reauthorize_attempts}: {sync_message}",
                retry_started_at,
            ))
            matched_candidate = get_autofind_candidate_by_serial(
                db, olt_id, serial_number, fsp=fsp
            )
            if _autofind_candidate_is_usable(
                matched_candidate,
                require_seen_after=require_seen_after,
                freshness_seconds=autofind_freshness,
            ):
                return AutofindValidationResult(
                    success=True,
                    candidate=matched_candidate,
                    steps_added=steps,
                )

        # Exhausted retries after deleting existing
        logger.warning(
            "Force ONT authorization stopped after delete because autofind did not rediscover the ONT olt_id=%s olt_name=%s fsp=%s serial=%s attempts=%s",
            olt_id,
            olt_name,
            fsp,
            serial_number,
            reauthorize_attempts,
        )
        return AutofindValidationResult(
            success=False,
            candidate=None,
            steps_added=steps,
            error_message="Force authorize deleted the existing registration, but the ONT was not rediscovered in autofind on the requested port after retrying. Check the physical link and rescan before authorizing.",
        )

    # No deleted existing and still not usable
    logger.warning(
        "ONT authorization validation failed after autofind refresh olt_id=%s olt_name=%s fsp=%s serial=%s",
        olt_id,
        olt_name,
        fsp,
        serial_number,
    )
    return AutofindValidationResult(
        success=False,
        candidate=None,
        steps_added=steps,
        error_message="The discovered ONT entry is no longer active for that port/serial after refreshing autofind data.",
    )


@dataclass
class AuthorizationStepResult:
    """Result of a single step in the authorization workflow."""

    step: int
    name: str
    success: bool
    message: str
    duration_ms: int = 0


@dataclass
class AuthorizationWorkflowResult:
    """Aggregate result of a full authorization workflow."""

    success: bool
    message: str
    steps: list[AuthorizationStepResult] = field(default_factory=list)
    ont_unit_id: str | None = None
    ont_id_on_olt: int | None = None
    status: str = "error"
    completed_authorization: bool = False
    follow_up_operation_id: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        """Serialize result to a JSON-safe dict."""
        return {
            "success": self.success,
            "message": self.message,
            "ont_unit_id": self.ont_unit_id,
            "ont_id_on_olt": self.ont_id_on_olt,
            "status": self.status,
            "completed_authorization": self.completed_authorization,
            "follow_up_operation_id": self.follow_up_operation_id,
            "duration_ms": self.duration_ms,
            "steps": [
                {
                    "step": s.step,
                    "name": s.name,
                    "success": s.success,
                    "message": s.message,
                    "duration_ms": s.duration_ms,
                }
                for s in self.steps
            ],
        }


def _build_authorization_failure(
    steps: list[AuthorizationStepResult],
    step_number: int,
    name: str,
    message: str,
    *,
    ont_unit_id: str | None = None,
    ont_id_on_olt: int | None = None,
) -> AuthorizationWorkflowResult:
    """Build a failure result with an appended failing step."""
    steps.append(
        AuthorizationStepResult(
            step=step_number,
            name=name,
            success=False,
            message=message,
        )
    )
    return AuthorizationWorkflowResult(
        success=False,
        message=f"Authorization failed at step {step_number}: {name}",
        steps=steps,
        ont_unit_id=ont_unit_id,
        ont_id_on_olt=ont_id_on_olt,
    )


def authorize_autofind_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
):
    """Authorize an unregistered ONT on an OLT with a fail-fast workflow.

    Args:
        db: Database session
        olt_id: UUID of the OLT
        fsp: Frame/Slot/Port (e.g., "0/1/13")
        serial_number: ONT serial number
        force_reauthorize: If True, delete any existing registration of this
            serial on the OLT before authorizing on the specified port.
    """
    from app.services.network import olt_ssh as olt_ssh_service
    from app.services.network import olt_ssh_ont as olt_ssh_ont_service
    from app.services.network.olt_write_reconciliation import (
        verify_ont_absent,
        verify_ont_authorized,
    )

    # Get configurable settings from DomainSettings (or use defaults)
    autofind_freshness = get_autofind_freshness_sec(db)
    reauthorize_attempts = get_force_reauthorize_attempts(db)
    reauthorize_retry_delay = get_force_reauthorize_retry_delay(db)

    steps: list[AuthorizationStepResult] = []
    started_at = monotonic()

    def _step_duration_ms(step_started_at: float) -> int:
        return max(0, int((monotonic() - step_started_at) * 1000))

    def _append_step(
        name: str,
        success: bool,
        message: str,
        *,
        step_started_at: float,
    ) -> int:
        step = len(steps) + 1
        steps.append(
            AuthorizationStepResult(
                step=step,
                name=name,
                success=success,
                message=message,
                duration_ms=_step_duration_ms(step_started_at),
            )
        )
        return step

    def _finalize(result, *, failure_detail: str | None = None):
        result.duration_ms = max(0, int((monotonic() - started_at) * 1000))
        logger.info(
            "ONT authorization workflow finished olt_id=%s fsp=%s serial=%s success=%s duration_ms=%s failed_step=%s failure_detail=%s",
            olt_id,
            fsp,
            serial_number,
            result.success,
            result.duration_ms,
            next((step.step for step in result.steps if not step.success), None),
            failure_detail,
        )
        return result

    def _fail(
        name: str,
        message: str,
        *,
        step_started_at: float | None = None,
        ont_unit_id: str | None = None,
        ont_id_on_olt: int | None = None,
        status: str = "error",
        completed_authorization: bool = False,
    ):
        if step_started_at is not None:
            step = _append_step(
                name,
                False,
                message,
                step_started_at=step_started_at,
            )
            result = AuthorizationWorkflowResult(
                success=False,
                message=(
                    f"Authorization completed on OLT, but follow-up failed at step {step}: {name}"
                    if status == "warning"
                    else f"Authorization failed at step {step}: {name}"
                ),
                steps=steps,
                ont_unit_id=ont_unit_id,
                ont_id_on_olt=ont_id_on_olt,
                status=status,
                completed_authorization=completed_authorization,
            )
        else:
            result = _build_authorization_failure(
                steps,
                len(steps) + 1,
                name,
                message,
                ont_unit_id=ont_unit_id,
                ont_id_on_olt=ont_id_on_olt,
            )
            result.status = status
            result.completed_authorization = completed_authorization
        return _finalize(result, failure_detail=message)

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return _fail("Authorize ONT on OLT", "OLT not found")

    authorization_profiles = None
    if force_reauthorize:
        profile_started_at = monotonic()
        from app.services.network.olt_profile_resolution import (
            resolve_authorization_profiles_from_db,
        )

        profiles_ok, profiles_msg, authorization_profiles = (
            resolve_authorization_profiles_from_db(db, olt)
        )
        if not profiles_ok or authorization_profiles is None:
            return _fail(
                "Resolve OLT authorization profiles",
                profiles_msg,
                step_started_at=profile_started_at,
            )
        profile_message = authorization_profiles.message
        if authorization_profiles.warnings:
            profile_message += " " + " ".join(authorization_profiles.warnings)
        _append_step(
            "Resolve OLT authorization profiles",
            True,
            profile_message,
            step_started_at=profile_started_at,
        )

    require_autofind_seen_after: datetime | None = None

    # Handle force reauthorize: delete existing registration first
    if force_reauthorize:
        force_started_at = monotonic()
        find_ok, find_msg, existing = olt_ssh_ont_service.find_ont_by_serial(
            olt, serial_number
        )
        if not find_ok:
            return _fail(
                "Find existing ONT registration",
                f"Failed to search for existing registration: {find_msg}",
                step_started_at=force_started_at,
            )
        if existing:
            logger.info(
                "Force reauthorize: deleting existing ONT registration serial=%s from %s port %s ont_id=%d",
                serial_number,
                olt.name,
                existing.fsp,
                existing.onu_id,
            )
            delete_ok, delete_msg = olt_ssh_ont_service.deauthorize_ont(
                olt, existing.fsp, existing.onu_id
            )
            if not delete_ok:
                return _fail(
                    "Delete existing ONT registration",
                    f"Failed to delete existing registration on {existing.fsp}: {delete_msg}",
                    step_started_at=force_started_at,
                )
            absence = verify_ont_absent(
                olt,
                fsp=existing.fsp,
                ont_id=existing.onu_id,
                serial_number=serial_number,
            )
            if not absence.success:
                return _fail(
                    "Verify existing ONT removal",
                    absence.message,
                    step_started_at=force_started_at,
                )
            require_autofind_seen_after = datetime.now(UTC)
            _append_step(
                "Delete existing ONT registration",
                True,
                f"Deleted existing registration from {existing.fsp} (ONT-ID {existing.onu_id}). {absence.message}",
                step_started_at=force_started_at,
            )
        else:
            _append_step(
                "Check existing ONT registration",
                True,
                "No existing registration found for this serial",
                step_started_at=force_started_at,
            )

    # Track whether force reauthorize deleted a stale registration.
    deleted_existing = any(
        s.name == "Delete existing ONT registration" and s.success for s in steps
    )

    # Validate autofind candidate (refreshing cache if needed)
    validate_started_at = monotonic()
    validation = _validate_autofind_candidate(
        db,
        olt_id=olt_id,
        olt_name=getattr(olt, "name", None),
        fsp=fsp,
        serial_number=serial_number,
        require_seen_after=require_autofind_seen_after,
        autofind_freshness=autofind_freshness,
        deleted_existing=deleted_existing,
        reauthorize_attempts=reauthorize_attempts,
        reauthorize_retry_delay=reauthorize_retry_delay,
    )

    # Apply steps from validation (refresh cache retries, etc.)
    for step_name, step_success, step_message, step_started in validation.steps_added:
        _append_step(step_name, step_success, step_message, step_started_at=step_started)
        if not step_success:
            return _finalize(
                AuthorizationWorkflowResult(
                    success=False,
                    message=f"Authorization failed at step {len(steps)}: {step_name}",
                    steps=steps,
                ),
                failure_detail=step_message,
            )

    if not validation.success:
        return _fail(
            "Validate discovered ONT row",
            validation.error_message or "Autofind validation failed",
            step_started_at=validate_started_at,
        )

    _append_step(
        "Validate discovered ONT row",
        True,
        "Validated discovered ONT row."
        if not validation.steps_added
        else "Validated discovered ONT row after refreshing autofind data.",
        step_started_at=validate_started_at,
    )

    from app.services.network.olt_profile_resolution import (
        ensure_ont_service_profile_match,
    )

    if authorization_profiles is None:
        profile_started_at = monotonic()
        from app.services.network.olt_profile_resolution import (
            resolve_authorization_profiles_from_db,
        )

        profiles_ok, profiles_msg, authorization_profiles = (
            resolve_authorization_profiles_from_db(db, olt)
        )
        if not profiles_ok or authorization_profiles is None:
            return _fail(
                "Resolve OLT authorization profiles",
                profiles_msg,
                step_started_at=profile_started_at,
            )
        profile_message = authorization_profiles.message
        if authorization_profiles.warnings:
            profile_message += " " + " ".join(authorization_profiles.warnings)
        _append_step(
            "Resolve OLT authorization profiles",
            True,
            profile_message,
            step_started_at=profile_started_at,
        )

    authorize_started_at = monotonic()
    ok, msg, ont_id = olt_ssh_service.authorize_ont(
        olt,
        fsp,
        serial_number,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
    )
    if not ok or ont_id is None:
        failure_message = msg
        if ok and ont_id is None:
            failure_message = "ONT was authorized, but ONT-ID could not be determined from the OLT response."
            logger.warning(
                "Could not determine ONT-ID for authorized serial %s on %s %s",
                serial_number,
                olt.name,
                fsp,
            )
        elif _is_serial_already_registered_message(msg):
            duplicate_verification = verify_ont_authorized(
                olt,
                fsp=fsp,
                ont_id=None,
                serial_number=serial_number,
            )
            if duplicate_verification.success:
                verified_ont_id = (
                    duplicate_verification.details.get("ont_id")
                    if duplicate_verification.details
                    else None
                )
                ont_id = (
                    int(verified_ont_id)
                    if isinstance(verified_ont_id, int | str)
                    and str(verified_ont_id).isdigit()
                    else None
                )
                recovery_message = "ONT serial was already registered on the OLT; reusing the existing registration."
                if ont_id is not None:
                    recovery_message += f" Resolved ONT-ID {ont_id} on {fsp}."
                else:
                    recovery_message += f" Verified existing registration on {fsp}."
                logger.info(
                    "ONT authorization recovered existing registration olt_id=%s olt_name=%s fsp=%s serial=%s ont_id=%s",
                    olt_id,
                    getattr(olt, "name", None),
                    fsp,
                    serial_number,
                    ont_id,
                )
                _append_step(
                    "Authorize ONT on OLT",
                    True,
                    recovery_message,
                    step_started_at=authorize_started_at,
                )
            else:
                return _fail(
                    "Authorize ONT on OLT",
                    "OLT reported the serial already exists, but readback could not confirm a matching ONT registration "
                    f"on {fsp}: {duplicate_verification.message}",
                    step_started_at=authorize_started_at,
                )
        else:
            return _fail(
                "Authorize ONT on OLT",
                failure_message,
                step_started_at=authorize_started_at,
            )
    if steps and steps[-1].name == "Authorize ONT on OLT" and steps[-1].success:
        pass
    else:
        _append_step(
            "Authorize ONT on OLT",
            True,
            f"{msg} Resolved ONT-ID {ont_id} on {fsp}.",
            step_started_at=authorize_started_at,
        )

    verify_started_at = monotonic()
    verification = verify_ont_authorized(
        olt,
        fsp=fsp,
        ont_id=ont_id,
        serial_number=serial_number,
    )
    if not verification.success:
        return _fail(
            "Verify authorization on OLT",
            verification.message,
            step_started_at=verify_started_at,
            ont_id_on_olt=ont_id,
        )
    _append_step(
        "Verify authorization on OLT",
        True,
        verification.message,
        step_started_at=verify_started_at,
    )

    match_started_at = monotonic()
    if ont_id is None:
        return _fail(
            "Verify ONT service profile match",
            "ONT ID on OLT is not available",
            step_started_at=match_started_at,
            ont_id_on_olt=ont_id,
            status="warning",
            completed_authorization=True,
        )
    match_ok, match_msg = ensure_ont_service_profile_match(
        olt,
        fsp=fsp,
        ont_id=ont_id,
    )
    if not match_ok:
        return _fail(
            "Verify ONT service profile match",
            match_msg,
            step_started_at=match_started_at,
            ont_id_on_olt=ont_id,
            status="warning",
            completed_authorization=True,
        )
    _append_step(
        "Verify ONT service profile match",
        True,
        match_msg,
        step_started_at=match_started_at,
    )

    ont_record_started_at = monotonic()
    ont_unit_id, create_msg = create_or_find_ont_for_authorized_serial(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        ont_id_on_olt=ont_id,
        olt_run_state=(
            str(verification.details.get("run_state") or "")
            if verification.details
            else None
        ),
    )
    if ont_unit_id is None:
        return _fail(
            "Create or find ONT record",
            create_msg,
            step_started_at=ont_record_started_at,
            ont_id_on_olt=ont_id,
            status="warning",
            completed_authorization=True,
        )
    _append_step(
        "Create or find ONT record",
        True,
        create_msg,
        step_started_at=ont_record_started_at,
    )

    resolve_started_at = monotonic()
    resolve_ok, resolve_msg = _resolve_authorized_autofind_candidate(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
    )
    if not resolve_ok:
        return _fail(
            "Resolve autofind candidate",
            resolve_msg,
            step_started_at=resolve_started_at,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id,
            status="warning",
            completed_authorization=True,
        )
    _append_step(
        "Resolve autofind candidate",
        True,
        resolve_msg,
        step_started_at=resolve_started_at,
    )

    queue_started_at = monotonic()
    if ont_id is None:
        return _fail(
            "Queue post-authorization sync",
            "ONT ID on OLT is not available",
            step_started_at=queue_started_at,
            ont_unit_id=ont_unit_id,
            status="warning",
            completed_authorization=True,
        )
    queue_ok, queue_msg, follow_up_operation_id = queue_post_authorization_follow_up(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        ont_id_on_olt=ont_id,
    )
    if not queue_ok:
        return _fail(
            "Queue post-authorization sync",
            queue_msg,
            step_started_at=queue_started_at,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id,
            status="warning",
            completed_authorization=True,
        )
    _append_step(
        "Queue post-authorization sync",
        True,
        queue_msg,
        step_started_at=queue_started_at,
    )

    return _finalize(
        AuthorizationWorkflowResult(
            success=True,
            message="ONT authorization completed. Post-authorization sync is running in the background.",
            steps=steps,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id,
            status="success",
            completed_authorization=True,
            follow_up_operation_id=follow_up_operation_id,
        )
    )


def authorize_autofind_ont_audited(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    request: Request | None = None,
) -> AuthorizationWorkflowResult:
    from app.services.network.action_logging import log_network_action_result

    result = authorize_autofind_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
    )
    status = getattr(result, "status", "success" if result.success else "error")
    log_olt_audit_event(
        db,
        request=request,
        action="force_authorize_ont" if force_reauthorize else "authorize_ont",
        entity_id=olt_id,
        metadata={
            "result": status,
            "message": result.message,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
        },
        status_code=200 if status in {"success", "warning"} else 500,
        is_success=status == "success",
    )
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id,
        action="Force Authorize ONT" if force_reauthorize else "Authorize ONT",
        success=result.success,
        message=result.message,
        metadata={
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
        },
    )
    return result


def run_post_authorization_follow_up(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> tuple[bool, str, list[dict[str, object]]]:
    """Run non-critical reconciliation after successful OLT authorization."""
    steps: list[dict[str, object]] = []

    def _add_step(name: str, success: bool, message: str) -> None:
        steps.append({"name": name, "success": success, "message": message})

    assignment_ok, assignment_msg = ensure_assignment_and_pon_port_for_authorized_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    _add_step("Create or link assignment and PON port", assignment_ok, assignment_msg)
    if not assignment_ok:
        return False, assignment_msg, steps

    from app.services.network.olt_targeted_sync import sync_authorized_ont_from_olt_snmp

    sync_ok, sync_msg, _sync_stats = sync_authorized_ont_from_olt_snmp(
        db,
        olt_id=olt_id,
        ont_unit_id=ont_unit_id,
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        serial_number=serial_number,
    )
    _add_step("Sync this ONT from OLT SNMP", sync_ok, sync_msg)
    if not sync_ok:
        return False, sync_msg, steps

    resolve_ok, resolve_msg = _resolve_authorized_autofind_candidate(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
    )
    _add_step("Resolve autofind candidate", resolve_ok, resolve_msg)
    if not resolve_ok:
        return False, resolve_msg, steps

    # Configure management IP so ONT can reach ACS (must happen before TR-069 bind)
    olt = get_olt_or_none(db, olt_id)
    if olt is not None:
        mgmt_ok, mgmt_msg = _configure_management_ip_for_authorization(
            db,
            olt=olt,
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
        )
        _add_step("Configure management IP", mgmt_ok, mgmt_msg)
        # Continue even if management IP config fails - it may already be configured
        # or the profile may not specify a management VLAN

    try:
        if olt is not None:
            from app.services.network.olt_ssh_ont import bind_tr069_server_profile
            from app.services.network.olt_tr069_admin import (
                ensure_tr069_profile_for_linked_acs,
            )

            profile_ok, profile_msg, profile_id = ensure_tr069_profile_for_linked_acs(
                olt
            )
            _add_step("Verify DotMac ACS profile", profile_ok, profile_msg)
            if profile_ok and profile_id is not None:
                bind_ok, bind_msg = bind_tr069_server_profile(
                    olt, fsp, ont_id_on_olt, profile_id=profile_id
                )
            else:
                bind_ok = False
                bind_msg = profile_msg
        else:
            bind_ok, bind_msg = False, "OLT not found for ACS bind."
    except (OSError, SQLAlchemyError) as exc:
        logger.warning("ACS bind failed for ONT %s: %s", ont_unit_id, exc)
        bind_ok = False
        bind_msg = f"ACS bind failed: {exc}"
    _add_step("Bind DotMac ACS profile", bind_ok, bind_msg)
    if not bind_ok:
        return False, bind_msg, steps

    try:
        from app.services.network.ont_provision_steps import queue_wait_tr069_bootstrap

        wait_result = queue_wait_tr069_bootstrap(db, ont_unit_id)
        _add_step("Wait for ACS inform", True, wait_result.message)
        logger.info(
            "Queued TR-069 bootstrap wait after authorization: ont_id=%s serial=%s fsp=%s ont_id_on_olt=%s",
            ont_unit_id,
            serial_number,
            fsp,
            ont_id_on_olt,
        )
    except Exception as exc:
        logger.warning(
            "Failed to queue TR-069 bootstrap wait after authorization for ONT %s: %s",
            ont_unit_id,
            exc,
        )
        message = f"Failed to queue ACS wait: {exc}"
        _add_step("Wait for ACS inform", False, message)
        return False, message, steps

    return True, "Post-authorization sync completed successfully.", steps


def queue_post_authorization_follow_up(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
    initiated_by: str | None = None,
) -> tuple[bool, str, str | None]:
    """Queue post-authorization reconciliation as a tracked background operation."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations
    from app.tasks.ont_authorization import run_post_authorization_follow_up_task

    correlation_key = f"ont_post_auth_sync:{ont_unit_id}"

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.ont_authorize,
            NetworkOperationTargetType.ont,
            ont_unit_id,
            correlation_key=correlation_key,
            initiated_by=initiated_by,
            input_payload={
                "phase": "post_authorization_sync",
                "title": "Post-Authorization Sync",
                "message": "Queued after successful OLT authorization.",
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "ont_id_on_olt": ont_id_on_olt,
            },
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            existing = db.scalars(
                select(NetworkOperation.id).where(
                    NetworkOperation.correlation_key == correlation_key,
                    NetworkOperation.status.in_(
                        (
                            NetworkOperationStatus.pending,
                            NetworkOperationStatus.running,
                            NetworkOperationStatus.waiting,
                        )
                    ),
                )
            ).first()
            return (
                True,
                "Post-authorization sync is already in progress.",
                (str(existing) if existing else None),
            )
        raise

    network_operations.mark_waiting(
        db,
        str(op.id),
        "Queued after successful OLT authorization.",
    )
    db.commit()

    try:
        from app.celery_app import enqueue_celery_task

        enqueue_celery_task(
            run_post_authorization_follow_up_task,
            args=[
                str(op.id),
                ont_unit_id,
                olt_id,
                fsp,
                serial_number,
                ont_id_on_olt,
            ],
            correlation_id=correlation_key,
            source="olt_post_authorization",
        )
    except Exception as exc:
        network_operations.mark_failed(
            db,
            str(op.id),
            f"Failed to queue post-authorization sync: {exc}",
        )
        db.commit()
        logger.error(
            "Failed to queue post-authorization sync for ONT %s: %s",
            ont_unit_id,
            exc,
            exc_info=True,
        )
        return (
            False,
            "Authorization succeeded, but follow-up sync could not be queued.",
            str(op.id),
        )

    return (
        True,
        "Queued post-authorization sync and ACS bind in the background.",
        str(op.id),
    )


def queue_authorize_autofind_ont(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> tuple[bool, str, str | None]:
    """Queue the full ONT authorization workflow as a tracked operation."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations
    from app.tasks.ont_authorization import run_authorize_autofind_ont_task

    normalized_serial = str(serial_number or "").replace("-", "").strip().upper()
    mode = "force" if force_reauthorize else "normal"
    correlation_key = f"ont_authorize:{mode}:{olt_id}:{fsp}:{normalized_serial}"

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.ont_authorize,
            NetworkOperationTargetType.olt,
            olt_id,
            correlation_key=correlation_key,
            initiated_by=initiated_by,
            input_payload={
                "phase": "authorization",
                "title": "Force ONT Authorization"
                if force_reauthorize
                else "ONT Authorization",
                "message": "Queued force authorization on the OLT."
                if force_reauthorize
                else "Queued authorization on the OLT.",
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "force_reauthorize": force_reauthorize,
            },
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            existing = db.scalars(
                select(NetworkOperation.id).where(
                    NetworkOperation.correlation_key == correlation_key,
                    NetworkOperation.status.in_(
                        (
                            NetworkOperationStatus.pending,
                            NetworkOperationStatus.running,
                            NetworkOperationStatus.waiting,
                        )
                    ),
                )
            ).first()
            return (
                True,
                "Authorization is already queued or running.",
                (str(existing) if existing else None),
            )
        raise

    network_operations.mark_waiting(db, str(op.id), "Queued authorization on the OLT.")
    db.commit()

    try:
        from app.celery_app import enqueue_celery_task

        enqueue_celery_task(
            run_authorize_autofind_ont_task,
            args=[
                str(op.id),
                olt_id,
                fsp,
                serial_number,
                force_reauthorize,
            ],
            correlation_id=correlation_key,
            source="olt_authorization",
        )
    except Exception as exc:
        network_operations.mark_failed(
            db,
            str(op.id),
            f"Failed to queue ONT authorization: {exc}",
        )
        db.commit()
        from app.services.network.action_logging import log_network_action_result

        log_network_action_result(
            request=request,
            resource_type="olt",
            resource_id=olt_id,
            action="Force Authorize ONT" if force_reauthorize else "Authorize ONT",
            success=False,
            message=f"Failed to queue ONT authorization: {exc}",
            metadata={
                "fsp": fsp,
                "serial_number": serial_number,
                "force_reauthorize": force_reauthorize,
                "operation_id": str(op.id),
                "queued": False,
            },
        )
        logger.error(
            "Failed to queue ONT authorization for serial %s on OLT %s: %s",
            serial_number,
            olt_id,
            exc,
            exc_info=True,
        )
        return False, "Failed to queue ONT authorization.", str(op.id)

    return (
        True,
        (
            "Force authorization queued. Track progress in operation history."
            if force_reauthorize
            else "Authorization queued. Track progress in operation history."
        ),
        str(op.id),
    )


def get_autofind_candidate_by_serial(
    db: Session,
    olt_id: str,
    serial_number: str | None,
    *,
    fsp: str | None = None,
):
    """Return the active autofind candidate matching a serial on an OLT."""
    from app.models.ont_autofind import OltAutofindCandidate

    clean_serial = re.sub(r"[^A-Za-z0-9]", "", serial_number or "").upper()
    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    clean_fsp = (fsp or "").strip()
    return next(
        (
            candidate
            for candidate in candidates
            if re.sub(r"[^A-Za-z0-9]", "", candidate.serial_number or "").upper()
            == clean_serial
            and (not clean_fsp or (candidate.fsp or "").strip() == clean_fsp)
        ),
        None,
    )


def create_or_find_ont_for_authorized_serial(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None = None,
    olt_run_state: str | None = None,
) -> tuple[str | None, str]:
    """Create or find an OntUnit for a just-authorized ONT serial."""
    from app.models.ont_autofind import OltAutofindCandidate
    from app.services.network.ont_status import apply_resolved_status_for_model

    clean_serial = re.sub(r"[^A-Za-z0-9]", "", serial_number).upper()
    olt = get_olt_or_none(db, olt_id)
    observed_online_status = (
        OnuOnlineStatus.online
        if str(olt_run_state or "").strip().lower() == "online"
        else None
    )

    existing = db.scalars(
        select(OntUnit).where(
            func.upper(func.replace(OntUnit.serial_number, "-", "")) == clean_serial,
        )
    ).first()
    if existing:
        try:
            existing.olt_device_id = uuid.UUID(olt_id)
            existing.is_active = True
            if ont_id_on_olt is not None:
                existing.external_id = str(ont_id_on_olt)
            parts = fsp.split("/")
            if len(parts) == 3:
                existing.board = f"{parts[0]}/{parts[1]}"
                existing.port = parts[2]
            if observed_online_status is not None:
                existing.online_status = observed_online_status
                existing.offline_reason = None
                existing.last_seen_at = datetime.now(UTC)
                existing.last_sync_source = "olt_ssh_readback"
                existing.last_sync_at = datetime.now(UTC)
            if existing.tr069_acs_server_id is None:
                if olt is not None:
                    existing.tr069_acs_server_id = olt.tr069_acs_server_id
            apply_resolved_status_for_model(existing)
            db.commit()
            return str(
                existing.id
            ), f"Using existing ONT record {existing.serial_number}."
        except SQLAlchemyError as exc:
            db.rollback()
            return None, f"Failed to update existing ONT record: {exc}"

    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    matched_candidate = next(
        (
            candidate
            for candidate in candidates
            if re.sub(r"[^A-Za-z0-9]", "", candidate.serial_number or "").upper()
            == clean_serial
        ),
        None,
    )

    display_serial = serial_number.replace("-", "")
    vendor = "Huawei" if display_serial.upper().startswith(("HWTC", "HWTT")) else None

    parts = fsp.split("/")
    board = f"{parts[0]}/{parts[1]}" if len(parts) == 3 else None
    port = parts[2] if len(parts) == 3 else None

    new_ont = OntUnit(
        id=str(uuid.uuid4()),
        serial_number=display_serial,
        external_id=str(ont_id_on_olt) if ont_id_on_olt is not None else None,
        vendor=vendor,
        model=getattr(matched_candidate, "model", None),
        mac_address=getattr(matched_candidate, "mac", None),
        olt_device_id=olt_id,
        board=board,
        port=port,
        is_active=True,
        online_status=observed_online_status or OnuOnlineStatus.unknown,
        offline_reason=None,
        last_seen_at=datetime.now(UTC) if observed_online_status else None,
        last_sync_source="olt_ssh_readback" if observed_online_status else None,
        last_sync_at=datetime.now(UTC) if observed_online_status else None,
        tr069_acs_server_id=getattr(olt, "tr069_acs_server_id", None) if olt else None,
        pon_type="gpon",
        name=display_serial,
    )
    try:
        db.add(new_ont)
        apply_resolved_status_for_model(new_ont)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        return None, f"Failed to create ONT record: {exc}"

    logger.info(
        "Created OntUnit %s for authorized serial %s on %s %s",
        new_ont.id,
        serial_number,
        olt_id,
        fsp,
    )
    return str(new_ont.id), f"Created ONT record for {display_serial}."


def ensure_ont_for_authorized_serial(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None = None,
) -> str | None:
    """Backward-compatible wrapper for legacy callers."""
    ont_id, _msg = create_or_find_ont_for_authorized_serial(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        ont_id_on_olt=ont_id_on_olt,
    )
    if ont_id is None:
        return None
    ok, _assignment_msg = ensure_assignment_and_pon_port_for_authorized_ont(
        db,
        ont_unit_id=ont_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    return ont_id if ok else None


def ensure_assignment_and_pon_port_for_authorized_ont(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
) -> tuple[bool, str]:
    """Ensure the authorized ONT is linked to an active assignment and PON port."""
    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT record not found."

    try:
        result = align_ont_assignment_to_authoritative_fsp(
            db,
            ont=ont,
            olt_id=olt_id,
            fsp=fsp,
        )
        if result is None:
            return False, f"Invalid OLT F/S/P for assignment: {fsp}."

        db.commit()
        return True, f"Linked ONT to PON port {result.pon_port.name}."
    except SQLAlchemyError as exc:
        logger.error(
            "Failed to link assignment/PON port for ONT %s on OLT %s: %s",
            ont_unit_id,
            olt_id,
            exc,
        )
        db.rollback()
        return False, f"Failed to link assignment/PON port: {exc}"
