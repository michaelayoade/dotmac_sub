"""Background ONT authorization follow-up tasks."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


def _post_authorization_follow_up_key(
    operation_id: str,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
    *args: object,
    **kw: object,
) -> str:
    return f"{operation_id}:{ont_unit_id}:{fsp}:{ont_id_on_olt}"


@celery_app.task(name="app.tasks.ont_authorization.authorize_ont_from_olt_api")
def authorize_ont_from_olt_api(
    operation_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
) -> dict[str, object]:
    from app.services.network.ont_authorization import (
        authorize_autofind_ont_and_provision_network_audited,
    )
    from app.services.network_operations import network_operations

    db = db_session_adapter.create_session()
    try:
        network_operations.mark_running(db, operation_id)
        result = authorize_autofind_ont_and_provision_network_audited(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            request=None,
        )
        payload = {
            "status": result.status,
            "ont_unit_id": result.ont_unit_id,
            "ont_id_on_olt": result.ont_id_on_olt,
            "completed_authorization": result.completed_authorization,
            "follow_up_operation_id": result.follow_up_operation_id,
            "pending_rediscovery": result.pending_rediscovery,
            "rediscovery_task_id": result.rediscovery_task_id,
            "steps": [
                {
                    "step": step.step,
                    "success": step.success,
                    "message": step.message,
                    "duration_ms": step.duration_ms,
                }
                for step in result.steps
            ],
        }
        if result.success and result.status != "warning":
            network_operations.mark_succeeded(db, operation_id, output_payload=payload)
        elif result.success and result.status == "warning":
            network_operations.mark_warning(
                db,
                operation_id,
                result.message,
                output_payload=payload,
            )
        else:
            network_operations.mark_failed(db, operation_id, result.message)
        db.commit()
        return {
            "success": result.success,
            "message": result.message,
            "data": payload,
        }
    except Exception as exc:
        db.rollback()
        try:
            network_operations.mark_failed(db, operation_id, str(exc))
            db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_authorization.ensure_tr069_acs_connectivity")
@idempotent_task(
    key_func=lambda ont_unit_id, *args, **kw: (
        f"tr069_connect:{ont_unit_id}:attempt:{kw.get('bootstrap_attempt', 1)}"
    )
)
def ensure_tr069_acs_connectivity(
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    ont_id_on_olt: int,
    *,
    bootstrap_attempt: int = 1,
    max_bootstrap_attempts: int = 3,
    retry_countdown_seconds: int = 60,
    olt_config_already_applied: bool = False,
) -> dict[str, Any]:
    """Configure management IP, bind TR-069 profile, and wait for ACS after authorization.

    This runs in the background after OLT authorization completes. It:
    1. Configures management IP on the OLT (IPHOST) if allocated
    2. Resolves the effective TR-069 profile ID from config pack / desired config
    3. Binds the TR-069 profile on the OLT (so ONT knows ACS URL)
    4. Waits for the ONT to send INFORM to GenieACS (up to 120s)

    This is non-blocking to authorization - if it fails, the ONT is still
    authorized but may need manual TR-069 binding later.
    """
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.olt_batched_mgmt import BatchedMgmtSpec
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_provision_steps import wait_tr069_bootstrap

    db = db_session_adapter.create_session()
    steps: list[dict[str, Any]] = []
    retry_olt_config_already_applied = olt_config_already_applied

    def _int_or_none(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _queue_bootstrap_retry(reason: str) -> None:
        if bootstrap_attempt >= max_bootstrap_attempts:
            logger.warning(
                "TR-069 ACS connectivity: Exhausted bootstrap attempts for ONT %s after %d attempt(s): %s",
                ont_unit_id,
                bootstrap_attempt,
                reason,
            )
            return
        from app.services.queue_adapter import enqueue_task

        next_attempt = bootstrap_attempt + 1
        result = enqueue_task(
            "app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
            args=[ont_unit_id, olt_id, fsp, ont_id_on_olt],
            kwargs={
                "bootstrap_attempt": next_attempt,
                "max_bootstrap_attempts": max_bootstrap_attempts,
                "retry_countdown_seconds": retry_countdown_seconds,
                "olt_config_already_applied": retry_olt_config_already_applied,
            },
            queue="acs",
            countdown=retry_countdown_seconds,
            correlation_id=f"tr069_acs_retry:{ont_unit_id}:attempt:{next_attempt}",
            source="ensure_tr069_acs_connectivity",
        )
        if result.queued:
            steps.append({
                "name": "Queue ACS bootstrap retry",
                "success": True,
                "message": (
                    f"Queued attempt {next_attempt}/{max_bootstrap_attempts} "
                    f"in {retry_countdown_seconds}s"
                ),
                "task_id": result.task_id,
            })
            logger.info(
                "TR-069 ACS connectivity: Queued retry attempt %d/%d for ONT %s",
                next_attempt,
                max_bootstrap_attempts,
                ont_unit_id,
            )
            return
        steps.append({
            "name": "Queue ACS bootstrap retry",
            "success": False,
            "message": result.error or "Failed to queue retry",
        })
        logger.warning(
            "TR-069 ACS connectivity: Failed to queue retry for ONT %s: %s",
            ont_unit_id,
            result.error,
        )

    try:
        ont = db.get(OntUnit, ont_unit_id)
        if not ont:
            logger.warning(
                "TR-069 ACS connectivity: ONT %s not found", ont_unit_id
            )
            return {"success": False, "message": "ONT not found", "steps": steps}

        olt = db.get(OLTDevice, olt_id)
        if not olt:
            logger.warning(
                "TR-069 ACS connectivity: OLT %s not found for ONT %s",
                olt_id,
                ont_unit_id,
            )
            return {"success": False, "message": "OLT not found", "steps": steps}

        # Resolve effective config for management IP and TR-069
        effective_config = resolve_effective_ont_config(db, ont)
        effective_values = effective_config.get("values", {})
        config_pack = effective_config.get("config_pack")

        mgmt_ip = effective_values.get("mgmt_ip_address")
        mgmt_vlan_tag = _int_or_none(effective_values.get("mgmt_vlan"))
        if mgmt_vlan_tag is None:
            mgmt_vlan = getattr(olt, "management_vlan", None)
            mgmt_vlan_tag = _int_or_none(getattr(mgmt_vlan, "tag", None))
        mgmt_gem_index = _int_or_none(getattr(config_pack, "mgmt_gem_index", None)) or 2
        mgmt_subnet = effective_values.get("mgmt_subnet") or "255.255.255.0"
        mgmt_gateway = effective_values.get("mgmt_gateway")
        tr069_profile_id = _int_or_none(effective_values.get("tr069_olt_profile_id"))
        acs_server_id = effective_values.get("tr069_acs_server_id")
        internet_config_ip_index = _int_or_none(
            effective_values.get("internet_config_ip_index")
        )
        wan_config_profile_id = _int_or_none(
            effective_values.get("wan_config_profile_id")
        )

        if olt_config_already_applied:
            steps.append({
                "name": "Run batched OLT management setup",
                "success": True,
                "message": "OLT management/TR-069 config already applied during authorization",
                "skipped": True,
            })
        elif not mgmt_ip:
            steps.append({
                "name": "Configure management IP",
                "success": True,
                "message": "No management IP allocated, skipping IPHOST config",
                "skipped": True,
            })
        elif mgmt_vlan_tag is None:
            steps.append({
                "name": "Configure management IP",
                "success": True,
                "message": "No management VLAN configured, skipping IPHOST config",
                "skipped": True,
            })

        spec = BatchedMgmtSpec(
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
            mgmt_vlan_tag=mgmt_vlan_tag if mgmt_ip and mgmt_vlan_tag else None,
            mgmt_gem_index=mgmt_gem_index,
            ip_mode="static" if mgmt_ip else "dhcp",
            ip_address=str(mgmt_ip) if mgmt_ip else None,
            subnet_mask=str(mgmt_subnet) if mgmt_ip else None,
            gateway=str(mgmt_gateway) if mgmt_ip and mgmt_gateway else None,
            internet_config_ip_index=internet_config_ip_index
            if mgmt_ip and mgmt_vlan_tag is not None
            else None,
            wan_config_profile_id=wan_config_profile_id
            if mgmt_ip and mgmt_vlan_tag is not None
            else None,
            tr069_profile_id=tr069_profile_id,
        )

        if acs_server_id and not tr069_profile_id:
            message = (
                "ACS is configured for this ONT, but no OLT TR-069 profile ID "
                "was resolved. The ONT cannot be bound to ACS until the OLT "
                "config pack or desired config provides tr069_olt_profile_id."
            )
            steps.append({
                "name": "Resolve TR-069 OLT profile",
                "success": False,
                "message": message,
            })
            db.commit()
            raise RuntimeError(message)

        if not olt_config_already_applied and not any(
            (
                spec.has_service_port,
                spec.has_iphost,
                spec.has_internet_config,
                spec.has_wan_config,
                spec.has_tr069,
            )
        ):
            logger.info(
                "TR-069 ACS connectivity: No OLT-side management config for ONT %s, skipping",
                ont.serial_number,
            )
            steps.append({
                "name": "Run batched OLT management setup",
                "success": True,
                "message": "No management IP or TR-069 profile configured",
                "skipped": True,
            })
            return {
                "success": True,
                "message": "No OLT-side management config configured",
                "steps": steps,
                "skipped": True,
            }

        if not olt_config_already_applied:
            logger.info(
                "TR-069 ACS connectivity: Running batched OLT management setup for ONT %s on OLT %s",
                ont.serial_number,
                olt.name,
            )
            adapter = get_protocol_adapter(olt)
            batch_result = adapter.configure_management_batch(spec)
            steps.append({
                "name": "Run batched OLT management setup",
                "success": batch_result.success,
                "message": batch_result.message,
                "data": batch_result.data,
                "mgmt_ip": mgmt_ip,
                "mgmt_vlan": mgmt_vlan_tag,
                "tr069_profile_id": tr069_profile_id,
            })
            if not batch_result.success:
                logger.warning(
                    "TR-069 ACS connectivity: Batched OLT management setup failed for ONT %s: %s",
                    ont.serial_number,
                    batch_result.message,
                )
                message = f"Batched OLT management setup failed: {batch_result.message}"
                _queue_bootstrap_retry(message)
                db.commit()
                raise RuntimeError(message)
            retry_olt_config_already_applied = True

        if not tr069_profile_id:
            steps.append({
                "name": "Wait for ACS inform",
                "success": True,
                "message": "No TR-069 profile configured, skipping ACS bootstrap",
                "skipped": True,
            })
            db.commit()
            return {
                "success": True,
                "message": "Batched OLT management setup completed; no TR-069 profile configured",
                "steps": steps,
                "skipped": True,
            }

        # Wait for ONT to inform ACS
        logger.info(
            "TR-069 ACS connectivity: Waiting for ONT %s to inform ACS",
            ont.serial_number,
        )
        try:
            from app.models.tr069 import Tr069CpeDevice
            from app.services.network.ont_action_network import send_connection_request

            existing_tr069 = db.scalars(
                select(Tr069CpeDevice)
                .where(Tr069CpeDevice.ont_unit_id == ont.id)
                .where(Tr069CpeDevice.is_active.is_(True))
                .where(Tr069CpeDevice.connection_request_url.isnot(None))
                .order_by(Tr069CpeDevice.last_inform_at.desc().nullslast())
                .limit(1)
            ).first()
            if existing_tr069:
                trigger_result = send_connection_request(db, ont_unit_id)
                steps.append({
                    "name": "Trigger ACS inform",
                    "success": trigger_result.success,
                    "message": trigger_result.message,
                })
                if trigger_result.success:
                    logger.info(
                        "TR-069 ACS connectivity: Triggered ACS inform for ONT %s before waiting",
                        ont.serial_number,
                    )
                else:
                    logger.info(
                        "TR-069 ACS connectivity: Could not trigger inform for ONT %s before waiting: %s",
                        ont.serial_number,
                        trigger_result.message,
                    )
            else:
                steps.append({
                    "name": "Trigger ACS inform",
                    "success": True,
                    "message": "No existing connection-request URL yet; waiting for first inform.",
                    "skipped": True,
                })
            bootstrap_result = wait_tr069_bootstrap(db, ont_unit_id)
            steps.append({
                "name": "Wait for ACS inform",
                "success": bootstrap_result.success,
                "message": bootstrap_result.message,
                "duration_ms": bootstrap_result.duration_ms,
            })
            if bootstrap_result.success:
                logger.info(
                    "TR-069 ACS connectivity: ONT %s successfully registered with ACS",
                    ont.serial_number,
                )
                from app.config import settings
                from app.models.tr069 import Tr069AcsServer
                from app.services.credential_crypto import decrypt_credential
                from app.services.network.ont_action_network import (
                    set_connection_request_credentials,
                )

                linked_tr069 = db.scalars(
                    select(Tr069CpeDevice)
                    .where(Tr069CpeDevice.ont_unit_id == ont.id)
                    .where(Tr069CpeDevice.is_active.is_(True))
                    .order_by(Tr069CpeDevice.last_inform_at.desc().nullslast())
                    .limit(1)
                ).first()
                acs_server_id = (
                    getattr(linked_tr069, "acs_server_id", None)
                    or getattr(ont, "tr069_acs_server_id", None)
                )
                acs_server = db.get(Tr069AcsServer, acs_server_id) if acs_server_id else None
                cr_username = (
                    str(getattr(acs_server, "connection_request_username", "") or "").strip()
                    if acs_server
                    else ""
                )
                cr_password = (
                    decrypt_credential(getattr(acs_server, "connection_request_password", None))
                    if acs_server
                    else None
                )
                inform_interval = (
                    getattr(acs_server, "periodic_inform_interval", None)
                    if acs_server
                    else None
                ) or settings.tr069_periodic_inform_interval
                if not cr_username or not cr_password:
                    credential_message = (
                        "ACS connection-request credentials are not configured; "
                        "cannot enforce periodic inform interval."
                    )
                    steps.append({
                        "name": "Apply ACS inform settings",
                        "success": False,
                        "message": credential_message,
                    })
                    db.commit()
                    raise RuntimeError(credential_message)

                credentials_result = set_connection_request_credentials(
                    db,
                    ont_unit_id,
                    cr_username,
                    cr_password,
                    periodic_inform_interval=int(inform_interval),
                )
                steps.append({
                    "name": "Apply ACS inform settings",
                    "success": credentials_result.success,
                    "message": credentials_result.message,
                    "periodic_inform_interval": int(inform_interval),
                })
                if not credentials_result.success:
                    _queue_bootstrap_retry(credentials_result.message)
                    db.commit()
                    raise RuntimeError(credentials_result.message)
            else:
                logger.warning(
                    "TR-069 ACS connectivity: ONT %s did not inform ACS: %s",
                    ont.serial_number,
                    bootstrap_result.message,
                )
                _queue_bootstrap_retry(bootstrap_result.message)
                db.commit()
                raise RuntimeError(
                    f"ONT did not inform ACS after bootstrap attempt "
                    f"{bootstrap_attempt}/{max_bootstrap_attempts}: "
                    f"{bootstrap_result.message}"
                )
        except Exception as exc:
            logger.warning(
                "TR-069 ACS connectivity: Error waiting for ACS inform for ONT %s: %s",
                ont.serial_number,
                exc,
            )
            steps.append({
                "name": "Wait for ACS inform",
                "success": False,
                "message": str(exc),
            })
            if not isinstance(exc, RuntimeError):
                _queue_bootstrap_retry(str(exc))
                db.commit()
            raise

        db.commit()
        return {
            "success": bootstrap_result.success,
            "message": bootstrap_result.message,
            "steps": steps,
        }
    except Exception as exc:
        db.rollback()
        logger.exception(
            "TR-069 ACS connectivity failed for ONT %s: %s", ont_unit_id, exc
        )
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_authorization.run_post_authorization_follow_up")
@idempotent_task(key_func=_post_authorization_follow_up_key)
def run_post_authorization_follow_up_task(
    operation_id: str,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> dict[str, object]:
    """Run non-critical reconciliation after foreground OLT authorization succeeds."""
    from app.services.network.ont_authorization import (
        run_post_authorization_follow_up,
    )
    from app.services.network_operations import network_operations

    db = db_session_adapter.create_session()
    try:
        network_operations.mark_running(db, operation_id)
        db.commit()

        try:
            success, message, steps = run_post_authorization_follow_up(
                db,
                ont_unit_id=ont_unit_id,
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
                ont_id_on_olt=ont_id_on_olt,
            )
            payload = {"message": message, "steps": steps}
            if success:
                network_operations.mark_succeeded(
                    db,
                    operation_id,
                    output_payload=payload,
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    message,
                    output_payload=payload,
                )
            db.commit()
            return {"success": success, "message": message, "steps": steps}
        except Exception as exc:
            logger.error(
                "Post-authorization follow-up failed for ONT %s: %s",
                ont_unit_id,
                exc,
                exc_info=True,
            )
            network_operations.mark_failed(db, operation_id, str(exc))
            db.commit()
            raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
