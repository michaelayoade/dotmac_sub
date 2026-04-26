"""Background ONT authorization follow-up tasks."""

from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_authorization.ensure_tr069_acs_connectivity")
@idempotent_task(key_func=lambda ont_unit_id, *args, **kw: f"tr069_connect:{ont_unit_id}")
def ensure_tr069_acs_connectivity(
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    ont_id_on_olt: int,
) -> dict[str, Any]:
    """Configure management IP, bind TR-069 profile, and wait for ACS after authorization.

    This runs in the background after OLT authorization completes. It:
    1. Configures management IP on the OLT (IPHOST) if allocated
    2. Resolves the effective TR-069 profile ID from config pack / desired config
    3. Binds the TR-069 profile on the OLT (so ONT knows ACS URL)
    4. Waits for the ONT to send INFORM to GenieACS (up to 120s)
    5. Normalizes WAN structure via TR-069 (deletes non-management WAN instances)

    This is non-blocking to authorization - if it fails, the ONT is still
    authorized but may need manual TR-069 binding later.
    """
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_ssh_ont.iphost import configure_ont_iphost
    from app.services.network.ont_provision_steps import wait_tr069_bootstrap

    db = db_session_adapter.create_session()
    steps: list[dict[str, Any]] = []

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

        # Step 1: Configure management IP on OLT (IPHOST) if allocated
        mgmt_ip = effective_values.get("mgmt_ip_address") or ont.mgmt_ip_address
        mgmt_vlan = effective_values.get("mgmt_vlan") or getattr(olt, "management_vlan", None)
        mgmt_vlan_tag = getattr(mgmt_vlan, "tag", None) if mgmt_vlan else None

        if mgmt_ip and mgmt_vlan_tag:
            logger.info(
                "Configuring management IP %s on VLAN %s for ONT %s",
                mgmt_ip,
                mgmt_vlan_tag,
                ont.serial_number,
            )
            try:
                # Get subnet and gateway from pool or defaults
                mgmt_subnet = effective_values.get("mgmt_subnet") or "255.255.255.0"
                mgmt_gateway = effective_values.get("mgmt_gateway")

                iphost_ok, iphost_msg = configure_ont_iphost(
                    olt,
                    fsp,
                    ont_id_on_olt,
                    vlan_id=int(mgmt_vlan_tag),
                    ip_mode="static",
                    ip_address=mgmt_ip,
                    subnet=mgmt_subnet,
                    gateway=mgmt_gateway,
                )
                steps.append({
                    "name": "Configure management IP",
                    "success": iphost_ok,
                    "message": iphost_msg,
                    "ip_address": mgmt_ip,
                    "vlan": mgmt_vlan_tag,
                })
                if not iphost_ok:
                    logger.warning(
                        "Failed to configure management IP for ONT %s: %s",
                        ont.serial_number,
                        iphost_msg,
                    )
                    # Non-fatal, continue with TR-069 binding
            except Exception as exc:
                logger.warning(
                    "Error configuring management IP for ONT %s: %s",
                    ont.serial_number,
                    exc,
                )
                steps.append({
                    "name": "Configure management IP",
                    "success": False,
                    "message": str(exc),
                })
                # Non-fatal, continue with TR-069 binding
        else:
            if not mgmt_ip:
                steps.append({
                    "name": "Configure management IP",
                    "success": True,
                    "message": "No management IP allocated, skipping IPHOST config",
                    "skipped": True,
                })
            elif not mgmt_vlan_tag:
                steps.append({
                    "name": "Configure management IP",
                    "success": True,
                    "message": "No management VLAN configured, skipping IPHOST config",
                    "skipped": True,
                })

        # Step 2: Resolve effective TR-069 profile ID (from config pack or desired_config)
        tr069_profile_id = effective_values.get("tr069_olt_profile_id")

        if not tr069_profile_id:
            logger.info(
                "TR-069 ACS connectivity: No TR-069 profile configured for ONT %s, skipping",
                ont.serial_number,
            )
            steps.append({
                "name": "Resolve TR-069 profile",
                "success": True,
                "message": "No TR-069 profile configured, skipping ACS bootstrap",
                "skipped": True,
            })
            return {
                "success": True,
                "message": "No TR-069 profile configured",
                "steps": steps,
                "skipped": True,
            }

        # Bind TR-069 profile on OLT
        logger.info(
            "TR-069 ACS connectivity: Binding profile %s for ONT %s on OLT %s",
            tr069_profile_id,
            ont.serial_number,
            olt.name,
        )
        try:
            adapter = get_protocol_adapter(olt)
            bind_result = adapter.bind_tr069_profile(
                fsp, ont_id_on_olt, profile_id=int(tr069_profile_id)
            )
            steps.append({
                "name": "Bind TR-069 profile",
                "success": bind_result.success,
                "message": bind_result.message,
                "profile_id": tr069_profile_id,
            })
            if not bind_result.success:
                logger.warning(
                    "TR-069 ACS connectivity: Failed to bind profile for ONT %s: %s",
                    ont.serial_number,
                    bind_result.message,
                )
                return {
                    "success": False,
                    "message": f"Failed to bind TR-069 profile: {bind_result.message}",
                    "steps": steps,
                }
        except Exception as exc:
            logger.warning(
                "TR-069 ACS connectivity: Error binding profile for ONT %s: %s",
                ont.serial_number,
                exc,
            )
            steps.append({
                "name": "Bind TR-069 profile",
                "success": False,
                "message": str(exc),
            })
            return {
                "success": False,
                "message": f"Error binding TR-069 profile: {exc}",
                "steps": steps,
            }

        # Wait for ONT to inform ACS
        logger.info(
            "TR-069 ACS connectivity: Waiting for ONT %s to inform ACS",
            ont.serial_number,
        )
        try:
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

                # Step 4: Normalize WAN structure after ACS bootstrap
                # This ensures consistent WCD layout for TR-069 configuration
                try:
                    from app.services.network.ont_action_wan import (
                        normalize_wan_structure,
                    )

                    logger.info(
                        "TR-069 ACS connectivity: Normalizing WAN structure for ONT %s",
                        ont.serial_number,
                    )
                    normalize_result = normalize_wan_structure(
                        db, ont_unit_id, preserve_mgmt=True
                    )
                    steps.append({
                        "name": "Normalize WAN structure",
                        "success": normalize_result.success,
                        "message": normalize_result.message,
                        "data": normalize_result.data,
                    })
                    if normalize_result.success:
                        logger.info(
                            "TR-069 ACS connectivity: WAN normalization complete for ONT %s",
                            ont.serial_number,
                        )
                    else:
                        logger.warning(
                            "TR-069 ACS connectivity: WAN normalization failed for ONT %s: %s",
                            ont.serial_number,
                            normalize_result.message,
                        )
                        # Non-fatal - ONT is still usable, just may have non-standard WAN layout
                except Exception as exc:
                    logger.warning(
                        "TR-069 ACS connectivity: Error normalizing WAN for ONT %s: %s",
                        ont.serial_number,
                        exc,
                    )
                    steps.append({
                        "name": "Normalize WAN structure",
                        "success": False,
                        "message": str(exc),
                    })
                    # Non-fatal - continue with success since ACS bootstrap worked
            else:
                logger.warning(
                    "TR-069 ACS connectivity: ONT %s did not inform ACS: %s",
                    ont.serial_number,
                    bootstrap_result.message,
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
            return {
                "success": False,
                "message": f"Error waiting for ACS inform: {exc}",
                "steps": steps,
            }

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
        return {
            "success": False,
            "message": str(exc),
            "steps": steps,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_authorization.run_post_authorization_follow_up")
@idempotent_task(key_func=lambda operation_id, ont_unit_id, **kw: f"{ont_unit_id}")
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

