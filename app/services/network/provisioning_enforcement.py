"""Provisioning enforcement — detect and fix gaps in the ONT provisioning chain.

Identifies ONTs where the database state (PPPoE credentials, ACS binding)
doesn't match the actual device state (WAN IP, TR-069 registration), then
re-runs the specific failed provisioning steps to close the gap.

Designed to run both on-demand (operator clicks a button) and periodically
(Celery beat task every 30 minutes).

Credential lookup for PPPoE password resolution is injected via
:class:`~app.services.network._credentials.PppoeCredentialProvider` so
this module never imports from the subscription/catalog domain. Callers
that have an ``AccessCredential``-backed store should wire up
``app.services.network_credential_bridge.AccessCredentialAdapter``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.network import OLTDevice, OntUnit
from app.models.tr069 import Tr069CpeDevice
from app.services.genieacs_service import genieacs_service
from app.services.network._credentials import PppoeCredentialProvider
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.imported_service_ports import (
    ImportedServicePortStateMissing,
    list_imported_service_ports,
    require_imported_service_port_state,
)
from app.services.network.iphost_priority import resolve_management_iphost_priority
from app.services.network.provisioning_settings import get_stale_runtime_hours
from app.services.zabbix_ont_status import get_ont_snapshots_from_zabbix

logger = logging.getLogger(__name__)


def _effective_field(db: Session, ont: OntUnit, key: str) -> object | None:
    resolved = resolve_effective_ont_config(db, ont)
    values = resolved.get("values", {}) if isinstance(resolved, dict) else {}
    return values.get(key)


@dataclass
class VlanDriftEntry:
    """Describes a VLAN mismatch for a single ONT."""

    ont_id: str
    serial_number: str
    fsp: str
    ont_slot_id: int | None
    drift_type: str  # "missing", "mismatch", or "orphaned"
    expected_vlan: int | None
    observed_vlans: list[int] = field(default_factory=list)
    message: str = ""


class ProvisioningEnforcement:
    """Detect and fix provisioning gaps across the ONT fleet."""

    @staticmethod
    def _list_candidate_onts(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> list[OntUnit]:
        stmt = (
            select(OntUnit)
            .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
            .options(
                joinedload(OntUnit.olt_device),
                joinedload(OntUnit.user_vlan),
            )
            .where(OntUnit.is_active.is_(True))
        )
        if olt_id:
            stmt = stmt.where(OntUnit.olt_device_id == olt_id)
        return list(db.scalars(stmt).unique().all())

    @staticmethod
    def detect_gaps(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, list[str]]:
        """Return ONT IDs grouped by gap category.

        Categories:
        - ``no_acs_binding``: PPPoE set but ONT not bound to ACS (OLT has ACS)
        - ``no_acs_on_olt``: ONT's OLT has no ACS server configured at all
        - ``pppoe_not_pushed``: Online, ACS-bound, PPPoE set but no WAN IP
        - ``stale_wan_ip``: Offline with WAN IP older than STALE_RUNTIME_HOURS

        Args:
            db: Database session.
            olt_id: Optional filter to a single OLT.
        """
        gaps: dict[str, list[str]] = {
            "no_acs_binding": [],
            "no_acs_on_olt": [],
            "pppoe_not_pushed": [],
            "wifi_pending_sync": [],
            "mgmt_pending_push": [],
            "stale_wan_ip": [],
        }

        stale_hours = get_stale_runtime_hours(db)
        stale_cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)

        candidates = ProvisioningEnforcement._list_candidate_onts(db, olt_id=olt_id)
        zabbix_snapshots = get_ont_snapshots_from_zabbix(db, candidates)

        for ont in candidates:
            zabbix_online = bool(
                (snapshot := zabbix_snapshots.get(str(ont.id))) and snapshot.online
            )
            resolved = resolve_effective_ont_config(db, ont)
            values = resolved.get("values", {}) if isinstance(resolved, dict) else {}
            effective_pppoe_username = values.get("pppoe_username")
            effective_wifi_ssid = values.get("wifi_ssid")
            effective_mgmt_ip = values.get("mgmt_ip_address")
            effective_acs_server_id = values.get("tr069_acs_server_id")
            has_observed_tr069_link = False
            if effective_acs_server_id is not None:
                has_observed_tr069_link = (
                    db.query(Tr069CpeDevice.id)
                    .filter(Tr069CpeDevice.ont_unit_id == ont.id)
                    .filter(Tr069CpeDevice.is_active.is_(True))
                    .filter(Tr069CpeDevice.acs_server_id == effective_acs_server_id)
                    .filter(Tr069CpeDevice.genieacs_device_id.isnot(None))
                    .first()
                    is not None
                )

            if (
                effective_pppoe_username not in (None, "")
                and effective_acs_server_id is not None
                and not has_observed_tr069_link
            ):
                gaps["no_acs_binding"].append(str(ont.id))

            if (
                effective_pppoe_username not in (None, "")
                and effective_acs_server_id is None
            ):
                gaps["no_acs_on_olt"].append(str(ont.id))

            if (
                effective_pppoe_username not in (None, "")
                and effective_acs_server_id is not None
                and getattr(ont, "observed_wan_ip", None) is None
                and zabbix_online
            ):
                gaps["pppoe_not_pushed"].append(str(ont.id))

            if (
                effective_wifi_ssid not in (None, "")
                and effective_acs_server_id is not None
                and zabbix_online
            ):
                gaps["wifi_pending_sync"].append(str(ont.id))

            if (
                effective_mgmt_ip not in (None, "")
                and getattr(ont, "board", None) is not None
                and getattr(ont, "port", None) is not None
                and getattr(ont, "external_id", None) is not None
            ):
                gaps["mgmt_pending_push"].append(str(ont.id))

            runtime_updated = getattr(ont, "observed_runtime_updated_at", None)
            if (
                getattr(ont, "observed_wan_ip", None) is not None
                and not zabbix_online
                and runtime_updated is not None
                and runtime_updated < stale_cutoff
            ):
                gaps["stale_wan_ip"].append(str(ont.id))

        return gaps

    @staticmethod
    def detect_gap_counts(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, int]:
        """Return gap counts derived from the same effective-config logic."""
        gaps = ProvisioningEnforcement.detect_gaps(db, olt_id=olt_id)
        return {key: len(value) for key, value in gaps.items()}

    @staticmethod
    def enforce_acs_binding(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Align linked TR-069 rows to the resolved ACS for specified ONTs.

        ONTs inherit ACS from their OLT/default unless they have an explicit
        override, so this no longer writes inherited ACS IDs onto OntUnit.

        Note:
            This method uses flush() to stage changes. Caller is responsible
            for committing the transaction.
        """
        updated = 0
        skipped = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont or not ont.olt_device_id:
                skipped += 1
                continue
            from app.services import tr069 as tr069_service

            acs_server_id = tr069_service.resolve_acs_server_for_ont(db, ont=ont)
            if not acs_server_id:
                skipped += 1
                continue
            changed = tr069_service.sync_ont_acs_server(
                db, ont, uuid.UUID(str(acs_server_id))
            )
            if changed:
                updated += changed
            else:
                skipped += 1

        if updated:
            db.flush()
            logger.info("ACS enforcement: bound %d ONTs, skipped %d", updated, skipped)
        return {"updated": updated, "skipped": skipped}

    @staticmethod
    def enforce_connection_request(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Send connection requests to force TR-069 bootstrap."""
        sent = 0
        failed = 0
        acs = genieacs_service
        for ont_id in ont_ids:
            try:
                result = acs.send_connection_request(db, ont_id)
                if result.success:
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.warning(
                    "Connection request failed for ONT %s: %s",
                    ont_id,
                    exc,
                )
                failed += 1
        logger.info(
            "Connection request enforcement: sent %d, failed %d",
            sent,
            failed,
        )
        return {"sent": sent, "failed": failed}

    @staticmethod
    def enforce_wifi_push(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Push WiFi configuration to ONTs via TR-069.

        Reads effective desired_config values and pushes them to the device
        using GenieACS. This is idempotent - pushing the
        same config multiple times has no adverse effects.
        """
        from app.services.credential_crypto import decrypt_credential

        acs = genieacs_service
        pushed = 0
        failed = 0
        skipped = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont:
                skipped += 1
                continue
            wifi_ssid = _effective_field(db, ont, "wifi_ssid")
            if not wifi_ssid:
                skipped += 1
                continue

            # Get password from effective config.
            password: str | None = None
            wifi_password_override = _effective_field(db, ont, "wifi.password")
            if wifi_password_override:
                try:
                    password = decrypt_credential(str(wifi_password_override))
                except ValueError:
                    logger.warning(
                        "Cannot decrypt WiFi password for ONT %s, pushing SSID only",
                        ont.serial_number,
                    )

            wifi_enabled = _effective_field(db, ont, "wifi_enabled")
            wifi_channel = _effective_field(db, ont, "wifi_channel")
            wifi_security_mode = _effective_field(db, ont, "wifi_security_mode")

            try:
                result = acs.set_wifi_config(
                    db,
                    ont_id,
                    enabled=True if wifi_enabled is None else bool(wifi_enabled),
                    ssid=str(wifi_ssid),
                    password=password,
                    channel=int(wifi_channel)  # type: ignore[call-overload]
                    if wifi_channel not in (None, "")
                    else None,
                    security_mode=(
                        str(wifi_security_mode)
                        if wifi_security_mode not in (None, "")
                        else None
                    ),
                )
                if result.success:
                    pushed += 1
                    logger.info(
                        "WiFi config pushed to ONT %s (SSID: %s)",
                        ont.serial_number,
                        wifi_ssid,
                    )
                else:
                    logger.warning(
                        "WiFi push failed for ONT %s: %s",
                        ont.serial_number,
                        result.message,
                    )
                    failed += 1
            except Exception as exc:
                logger.warning(
                    "WiFi push error for ONT %s: %s",
                    ont.serial_number,
                    exc,
                )
                failed += 1

        logger.info(
            "WiFi enforcement: pushed %d, failed %d, skipped %d",
            pushed,
            failed,
            skipped,
        )
        return {"pushed": pushed, "failed": failed, "skipped": skipped}

    @staticmethod
    def enforce_management_config(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Push management service-port and IPHOST config to OLTs via SSH.

        Reads mgmt_ip_address, mgmt_vlan from OntUnit and pushes to the OLT.
        This is idempotent - existing service-ports are detected and skipped.
        Batches by OLT for connection efficiency.
        """
        import time

        from app.services.network.olt_dependency_preflight import (
            validate_olt_profile_dependencies,
        )
        from app.services.network.olt_protocol_adapters import get_protocol_adapter
        from app.services.network.serial_utils import parse_ont_id_on_olt

        pushed = 0
        failed = 0
        skipped = 0

        # Group ONTs by OLT for batching
        onts_by_olt: dict[str, list[OntUnit]] = {}
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont:
                skipped += 1
                continue
            mgmt_ip_address = _effective_field(db, ont, "mgmt_ip_address")
            if not mgmt_ip_address:
                skipped += 1
                continue
            if not ont.board or not ont.port or not ont.external_id:
                skipped += 1
                continue
            if not ont.olt_device_id:
                skipped += 1
                continue

            olt_key = str(ont.olt_device_id)
            if olt_key not in onts_by_olt:
                onts_by_olt[olt_key] = []
            onts_by_olt[olt_key].append(ont)

        # Process each OLT batch
        for olt_id, onts in onts_by_olt.items():
            olt = db.get(OLTDevice, olt_id)
            if not olt:
                skipped += len(onts)
                continue

            logger.info(
                "Management enforcement: processing %d ONTs on %s",
                len(onts),
                olt.name,
            )
            dependency_result = validate_olt_profile_dependencies(
                db,
                olt_id=str(olt.id),
                operation="management IPHOST enforcement",
            )
            if not dependency_result.success:
                logger.warning(
                    "Management enforcement skipped OLT %s: %s",
                    olt.name,
                    dependency_result.message,
                )
                failed += len(onts)
                continue

            for ont in onts:
                fsp = f"{ont.board}/{ont.port}"
                ont_id_on_olt = parse_ont_id_on_olt(ont.external_id)

                if ont_id_on_olt is None:
                    skipped += 1
                    continue

                # Resolve management network from effective/imported state.
                effective_mgmt_vlan = _effective_field(db, ont, "mgmt_vlan")
                effective_mgmt_subnet = _effective_field(db, ont, "mgmt_subnet")
                effective_mgmt_gateway = _effective_field(db, ont, "mgmt_gateway")
                if effective_mgmt_vlan in (None, ""):
                    logger.warning(
                        "Management enforcement skipped ONT %s: missing effective management VLAN",
                        ont.serial_number,
                    )
                    skipped += 1
                    continue
                if effective_mgmt_subnet in (None, "") or effective_mgmt_gateway in (
                    None,
                    "",
                ):
                    logger.warning(
                        "Management enforcement skipped ONT %s: missing effective management subnet/gateway",
                        ont.serial_number,
                    )
                    skipped += 1
                    continue

                mgmt_vlan_tag = int(str(effective_mgmt_vlan))
                ip_addr = str(mgmt_ip_address)
                subnet_mask = str(effective_mgmt_subnet)
                gateway = str(effective_mgmt_gateway)
                mgmt_priority = resolve_management_iphost_priority(
                    db,
                    olt_id=olt.id,
                    fsp=fsp,
                    ont_id_on_olt=ont_id_on_olt,
                    mgmt_vlan_tag=mgmt_vlan_tag,
                    mgmt_gem_index=_effective_field(db, ont, "mgmt_gem_index"),
                    line_profile_id=_effective_field(
                        db, ont, "authorization_line_profile_id"
                    ),
                )
                if mgmt_priority is None:
                    logger.warning(
                        "Management enforcement skipped ONT %s: could not resolve IPHOST priority",
                        ont.serial_number,
                    )
                    skipped += 1
                    continue

                try:
                    # Configure IPHOST (management IP on ONT)
                    # Service-port creation skipped - management uses existing
                    # service-ports created during internet provisioning
                    adapter = get_protocol_adapter(olt)
                    iphost_result = adapter.configure_iphost(
                        fsp,
                        ont_id_on_olt,
                        vlan=mgmt_vlan_tag,
                        mode="static",
                        priority=mgmt_priority,
                        ip_address=ip_addr,
                        subnet_mask=subnet_mask,
                        gateway=gateway,
                    )
                    iphost_ok = iphost_result.success
                    iphost_msg = iphost_result.message

                    if iphost_ok:
                        pushed += 1
                    else:
                        logger.warning(
                            "Management IPHOST failed for ONT %s: %s",
                            ont.serial_number,
                            iphost_msg[:60],
                        )
                        failed += 1

                    # Delay between ONTs to prevent OLT connection overload
                    time.sleep(1.0)

                except Exception as exc:
                    logger.warning(
                        "Management push error for ONT %s: %s",
                        ont.serial_number,
                        exc,
                    )
                    failed += 1

        logger.info(
            "Management enforcement: pushed %d, failed %d, skipped %d",
            pushed,
            failed,
            skipped,
        )
        return {"pushed": pushed, "failed": failed, "skipped": skipped}

    @staticmethod
    def clear_stale_runtime(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Clear stale cached runtime observations on offline ONTs.

        Note:
            This method uses flush() to stage changes. Caller is responsible
            for committing the transaction.
        """
        cleared = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont:
                continue
            ont.observed_wan_ip = None
            ont.observed_pppoe_status = None
            ont.observed_lan_mode = None
            ont.observed_wifi_clients = None
            ont.observed_lan_hosts = None
            ont.observed_runtime_updated_at = None
            ont.tr069_last_snapshot = {}
            ont.tr069_last_snapshot_at = None
            cleared += 1

        if cleared:
            db.flush()
            logger.info("Cleared stale runtime data on %d ONTs", cleared)
        return {"cleared": cleared}

    @staticmethod
    def detect_vlan_drift(
        db: Session,
        olt_id: str,
    ) -> list[VlanDriftEntry]:
        """Detect service-port VLAN mismatches for ONTs on a specific OLT.

        This method reads imported OLT service-port state from the database and
        compares it against expected VLANs from the database.

        Note:
            This should be run after OLT state import has populated
            ``olt_service_ports`` for the target OLT.

        Drift types detected:
        - ``missing``: ONT has expected VLAN in DB but no service-port on OLT
        - ``mismatch``: ONT has service-port but VLAN differs from expected
        - ``orphaned``: ONT has service-port but no VLAN is expected in DB

        Args:
            db: Database session.
            olt_id: The OLT to scan for drift.

        Returns:
            List of VlanDriftEntry describing each detected mismatch.
        """
        from app.services.network.olt_inventory import get_olt_or_none
        from app.services.network.serial_utils import parse_ont_id_on_olt

        olt = get_olt_or_none(db, olt_id)
        if not olt:
            logger.warning("Cannot detect VLAN drift: OLT %s not found", olt_id)
            return []

        # Load ONTs with their VLAN relationships
        # Filter to ONTs that have both board/port (for FSP) and external_id (for OLT ONT-ID)
        stmt = (
            select(OntUnit)
            .where(
                OntUnit.olt_device_id == olt_id,
                OntUnit.is_active.is_(True),
                OntUnit.board.isnot(None),
                OntUnit.port.isnot(None),
                OntUnit.external_id.isnot(None),
            )
            .options(joinedload(OntUnit.user_vlan))
        )
        onts = db.scalars(stmt).unique().all()
        if not onts:
            return []

        # Group ONTs by FSP for efficient OLT queries
        # FSP is constructed from board/port (e.g., "0/2/1")
        onts_by_fsp: dict[str, list[tuple[OntUnit, int]]] = {}
        for ont in onts:
            board = ont.board or ""
            port = ont.port or ""
            if not board or not port:
                continue
            fsp = f"{board}/{port}"
            olt_ont_id = parse_ont_id_on_olt(ont.external_id)
            if olt_ont_id is None:
                continue
            onts_by_fsp.setdefault(fsp, []).append((ont, olt_ont_id))

        drift_entries: list[VlanDriftEntry] = []

        for fsp, fsp_onts in onts_by_fsp.items():
            try:
                require_imported_service_port_state(db, olt_id=olt.id)
                service_ports = list_imported_service_ports(
                    db,
                    olt_id=olt.id,
                    fsp=fsp,
                )
            except ImportedServicePortStateMissing as exc:
                logger.warning(
                    "Cannot inspect imported service-ports for OLT %s FSP %s: %s",
                    olt.name,
                    fsp,
                    exc,
                )
                continue

            # Build lookup: olt_ont_id -> list of observed VLANs
            observed_vlans_by_ont: dict[int, list[int]] = {}
            for sp in service_ports:
                if sp.ont_id is not None:
                    observed_vlans_by_ont.setdefault(sp.ont_id, []).append(sp.vlan_id)

            for ont, olt_ont_id in fsp_onts:
                # Determine expected VLAN from effective config or user_vlan
                expected_vlan: int | None = None
                effective_wan_vlan = _effective_field(db, ont, "wan_vlan")
                if effective_wan_vlan not in (None, ""):
                    expected_vlan = int(str(effective_wan_vlan))
                elif ont.user_vlan and ont.user_vlan.tag:
                    expected_vlan = ont.user_vlan.tag

                observed = observed_vlans_by_ont.get(olt_ont_id, [])

                if expected_vlan is None and not observed:
                    # No VLAN expected and none observed — not drift
                    continue

                if expected_vlan is None and observed:
                    # Service-ports exist but no VLAN expected in DB
                    drift_entries.append(
                        VlanDriftEntry(
                            ont_id=str(ont.id),
                            serial_number=ont.serial_number or "",
                            fsp=fsp,
                            ont_slot_id=olt_ont_id,
                            drift_type="orphaned",
                            expected_vlan=None,
                            observed_vlans=observed,
                            message=f"Service-port(s) with VLAN(s) {observed} exist but no VLAN is configured in DB",
                        )
                    )
                    continue

                if expected_vlan is not None and not observed:
                    # VLAN expected but no service-port on OLT
                    drift_entries.append(
                        VlanDriftEntry(
                            ont_id=str(ont.id),
                            serial_number=ont.serial_number or "",
                            fsp=fsp,
                            ont_slot_id=olt_ont_id,
                            drift_type="missing",
                            expected_vlan=expected_vlan,
                            observed_vlans=[],
                            message=f"Expected VLAN {expected_vlan} but no service-port found on OLT",
                        )
                    )
                    continue

                if expected_vlan is not None and expected_vlan not in observed:
                    # Service-ports exist but expected VLAN not among them
                    drift_entries.append(
                        VlanDriftEntry(
                            ont_id=str(ont.id),
                            serial_number=ont.serial_number or "",
                            fsp=fsp,
                            ont_slot_id=olt_ont_id,
                            drift_type="mismatch",
                            expected_vlan=expected_vlan,
                            observed_vlans=observed,
                            message=f"Expected VLAN {expected_vlan} but found {observed}",
                        )
                    )

        logger.info(
            "VLAN drift detection on OLT %s: %d issues found",
            olt.name,
            len(drift_entries),
        )
        return drift_entries

    @staticmethod
    def run_full_enforcement(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, Any]:
        """Detect provisioning gaps without mutating live device state."""
        gaps = ProvisioningEnforcement.detect_gaps(db, olt_id=olt_id)

        stats: dict[str, Any] = {
            "gaps_detected": {k: len(v) for k, v in gaps.items()},
            "remediation_performed": False,
        }

        logger.info("Full enforcement complete: %s", stats)
        return stats


def _resolve_access_credential_password(
    db: Session,
    credentials: PppoeCredentialProvider,
    ont: OntUnit,
    *,
    username: str | None = None,
) -> str:
    """Resolve the PPPoE password via the injected credential provider.

    Looks up the active credential by the ONT's ``pppoe_username`` and
    decrypts its stored ``secret_hash``. Returns an empty string if no
    active credential is found or the secret cannot be decrypted.
    """
    effective_pppoe_username = _effective_field(db, ont, "pppoe_username")
    lookup_username = str(username or effective_pppoe_username or "").strip()
    if not lookup_username:
        return ""

    try:
        cred = credentials.get_by_username(lookup_username)
    except Exception as exc:  # noqa: BLE001 - provider errors must not abort the run
        logger.warning(
            "Credential provider lookup failed for ONT %s (username %s): %s",
            ont.serial_number,
            lookup_username,
            exc,
            exc_info=True,
        )
        return ""

    if cred is None or not cred.secret_hash:
        return ""

    try:
        from app.services.credential_crypto import decrypt_credential

        return decrypt_credential(cred.secret_hash) or ""
    except ValueError as exc:
        logger.warning(
            "Could not decrypt access credential for ONT %s (username %s): %s",
            ont.serial_number,
            lookup_username,
            exc,
            exc_info=True,
        )
        return ""


def _default_credential_provider(db: Session) -> PppoeCredentialProvider | None:
    """Return the full-app AccessCredential adapter when it is available."""
    try:
        from app.services.network_credential_bridge import AccessCredentialAdapter
    except ImportError:  # pragma: no cover - standalone deployments
        return None
    return AccessCredentialAdapter(db)
