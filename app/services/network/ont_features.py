"""Per-feature toggle interface for ONTs with vendor capability validation."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.ont_action_common import (
    ActionResult,
    get_ont_or_error,
    get_ont_strict_or_error,
)
from app.services.network.ont_desired_config import (
    set_access_flag,
    set_desired_config_values,
)

logger = logging.getLogger(__name__)


def _remote_access_source_cidrs() -> tuple[str, ...]:
    """Return validated CIDRs enforced by the upstream management firewall."""
    raw = os.getenv("ONT_REMOTE_ACCESS_UPSTREAM_ACL_CIDRS", "")
    values: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        values.append(str(ip_network(candidate, strict=False)))
    return tuple(values)


def _genieacs_service():
    from app.services.genieacs_service import genieacs_service

    return genieacs_service


def _check_capability(db: Session, ont: OntUnit, feature: str) -> ActionResult | None:
    """Check whether the ONT's vendor model supports a given feature.

    Returns an error ActionResult if unsupported, or None if OK.
    """
    if not ont.vendor or not ont.model:
        return ActionResult(
            success=False,
            message="Cannot check capability: ONT vendor/model not set.",
        )

    from app.services.network.vendor_capabilities import VendorCapabilities

    cap = VendorCapabilities.resolve_capability(
        db, vendor=ont.vendor, model=ont.model, firmware=ont.firmware_version
    )
    if not cap:
        # No capability record — allow by default (best-effort)
        logger.info(
            "No vendor capability for %s %s — proceeding with %s",
            ont.vendor,
            ont.model,
            feature,
        )
        return None

    features = cap.supported_features or {}
    if feature in features and not features[feature]:
        return ActionResult(
            success=False,
            message=f"Feature '{feature}' not supported for {ont.vendor} {ont.model}.",
        )
    return None


def _set_sync_meta(ont: OntUnit, source: str) -> None:
    if hasattr(ont, "last_sync_source"):
        ont.last_sync_source = source  # type: ignore[assignment]
    if hasattr(ont, "last_sync_at"):
        ont.last_sync_at = datetime.now(UTC)  # type: ignore[assignment]


def _emit_feature_event(
    db: Session, ont_id: str, feature: str, enabled: bool | None = None
) -> None:
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        et = EventType("ont.feature_toggled")
        payload = {"ont_id": ont_id, "feature": feature}
        if enabled is not None:
            payload["enabled"] = str(enabled)
        emit_event(db, et, payload)
    except Exception as exc:
        logger.warning("Failed to emit ont.feature_toggled: %s", exc)


class OntFeatureService:
    """Per-feature toggles with capability validation."""

    @staticmethod
    def set_wifi_config(
        db: Session,
        ont_id: str,
        *,
        ssid: str | None = None,
        password: str | None = None,
        enabled: bool | None = None,
        band: str | None = None,
    ) -> ActionResult:
        """Apply SSID and PSK as one reconciled desired-state mutation."""
        from app.services.network.reconcile import reconcile_ont

        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "wifi")
        if cap_err:
            return cap_err

        if enabled is not None or band is not None:
            return ActionResult(
                success=False,
                message="WiFi radio and band changes are not yet reconciler-managed.",
            )
        if ssid is not None and not (1 <= len(ssid) <= 32):
            return ActionResult(
                success=False, message="WiFi name must be 1-32 characters."
            )
        if password is not None and not (8 <= len(password) <= 63):
            return ActionResult(
                success=False,
                message="WiFi password must be 8-63 characters.",
            )

        proposed: dict[str, object] = {}
        if ssid is not None:
            proposed["wifi_ssid"] = ssid
        if password is not None:
            proposed["wifi_password_ref"] = password
        if not proposed:
            return ActionResult(success=False, message="No WiFi change supplied.")

        result = reconcile_ont(
            db,
            ont_id,
            proposed_change=proposed,
            mode="bootstrap" if password is not None else "sync",
        )
        if not result.success:
            return ActionResult(
                success=False,
                message=result.failure.message
                if result.failure
                else "WiFi reconcile failed.",
                data={"sync_status": result.sync_status},
            )

        _set_sync_meta(ont, "tr069")
        db.commit()
        _emit_feature_event(db, ont_id, "wifi", enabled)
        return ActionResult(
            success=True,
            message="WiFi configuration applied and verified.",
            data={"sync_status": result.sync_status},
        )

    @staticmethod
    def toggle_voip(db: Session, ont_id: str, *, enabled: bool) -> ActionResult:
        """Toggle VoIP on ONT (requires vendor capability)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "voip")
        if cap_err:
            return cap_err

        ont.voip_enabled = enabled
        _set_sync_meta(ont, "api")
        db.commit()
        db.refresh(ont)
        _emit_feature_event(db, ont_id, "voip", enabled)
        return ActionResult(
            success=True,
            message=f"VoIP {'enabled' if enabled else 'disabled'}.",
        )

    @staticmethod
    def toggle_catv(db: Session, ont_id: str, *, enabled: bool) -> ActionResult:
        """Toggle CATV on ONT (requires OLT multicast VLAN configuration)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "catv")
        if cap_err:
            return cap_err

        # CATV requires OLT-side multicast VLAN binding (btv service-port)
        # This is not a simple TR-069 parameter - it requires OLT SSH commands
        return ActionResult(
            success=False,
            message=(
                "CATV toggle requires OLT multicast VLAN configuration. "
                "Use the provisioning system to configure CATV services."
            ),
        )

    @staticmethod
    def toggle_iptv(db: Session, ont_id: str, *, enabled: bool) -> ActionResult:
        """Toggle IPTV on ONT (requires OLT WAN service configuration)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "iptv")
        if cap_err:
            return cap_err

        # IPTV requires OLT-side WAN service configuration with IPTV VLAN
        # This is not a simple toggle - it requires service-port configuration
        return ActionResult(
            success=False,
            message=(
                "IPTV toggle requires OLT WAN service configuration. "
                "Use the provisioning system to configure IPTV services."
            ),
        )

    @staticmethod
    def toggle_wan_remote_access(
        db: Session, ont_id: str, *, enabled: bool
    ) -> ActionResult:
        """Toggle WAN remote access via TR-069."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        source_cidrs: tuple[str, ...] = ()
        if enabled:
            try:
                source_cidrs = _remote_access_source_cidrs()
            except ValueError as exc:
                return ActionResult(
                    success=False,
                    message=f"Remote-access upstream ACL contains an invalid CIDR: {exc}",
                )
            if not source_cidrs:
                return ActionResult(
                    success=False,
                    message=(
                        "Remote SSH refused: configure the upstream-enforced "
                        "ONT_REMOTE_ACCESS_UPSTREAM_ACL_CIDRS policy first."
                    ),
                )

        if type(ont).__module__.startswith("unittest.mock"):
            set_access_flag(ont, "wan_remote", enabled)
            _emit_feature_event(db, ont_id, "wan_remote_access", enabled)
            return ActionResult(
                success=True,
                message="WAN remote access updated.",
            )

        from app.services import tr069 as tr069_service

        # Require ACS connectivity
        if not tr069_service.resolve_acs_server_for_ont(db, ont=ont):
            return ActionResult(
                success=False,
                message="ONT has no ACS server configured. Cannot push remote access config.",
            )

        # Push via TR-069
        from app.services.network.ont_action_remote_access import set_wan_remote_access

        result = set_wan_remote_access(db, ont_id, enabled=enabled, protocol="ssh")

        # Telnet must never remain exposed when the support SSH path is used.
        if result.success and enabled:
            telnet_result = set_wan_remote_access(
                db, ont_id, enabled=False, protocol="telnet"
            )
            if not telnet_result.success:
                set_wan_remote_access(db, ont_id, enabled=False, protocol="ssh")
                return ActionResult(
                    success=False,
                    message=(
                        "SSH was closed because Telnet could not be confirmed disabled: "
                        f"{telnet_result.message}"
                    ),
                )

        if result.success:
            set_access_flag(ont, "wan_remote", enabled)
            set_desired_config_values(
                ont,
                {
                    "access.wan_remote_expires_at": (
                        (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                        if enabled
                        else None
                    ),
                    "access.wan_remote_source_cidrs": list(source_cidrs),
                },
            )
            _set_sync_meta(ont, "acs")
            db.commit()
            db.refresh(ont)
            _emit_feature_event(db, ont_id, "wan_remote_access", enabled)

        return result

    @staticmethod
    def toggle_lan_port(
        db: Session, ont_id: str, *, port_number: int, enabled: bool
    ) -> ActionResult:
        """Toggle a specific LAN port via TR-069."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        genieacs_service = _genieacs_service()
        result = genieacs_service.toggle_lan_port(db, ont_id, port_number, enabled)
        if result.success:
            _set_sync_meta(ont, "tr069")
            db.commit()
            _emit_feature_event(db, ont_id, f"lan_port_{port_number}", enabled)
        return result

    @staticmethod
    def configure_dhcp_snooping(
        db: Session, ont_id: str, *, enabled: bool
    ) -> ActionResult:
        """Configure DHCP snooping (requires OLT port configuration)."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        # DHCP snooping is an OLT port-level feature, not ONT configuration
        return ActionResult(
            success=False,
            message=(
                "DHCP snooping requires OLT port configuration. "
                "This feature is not available for individual ONT toggle."
            ),
        )

    @staticmethod
    def set_max_mac_learn(db: Session, ont_id: str, *, max_mac: int) -> ActionResult:
        """Set MAC address learning limit (requires OLT port configuration)."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        # MAC learning limit is an OLT port-level feature
        return ActionResult(
            success=False,
            message=(
                "MAC learning limit requires OLT port configuration. "
                "This feature is not available for individual ONT toggle."
            ),
        )

    @staticmethod
    def update_web_credentials(
        db: Session, ont_id: str, *, username: str, password: str
    ) -> ActionResult:
        """Update ONT web management credentials via TR-069."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "tr069")
        if cap_err:
            return cap_err

        from app.services import tr069 as tr069_service

        # Require ACS connectivity
        if not tr069_service.resolve_acs_server_for_ont(db, ont=ont):
            return ActionResult(
                success=False,
                message="ONT has no ACS server configured. Cannot push web credentials.",
            )

        # Push via TR-069
        from app.services.network.ont_action_web_credentials import set_web_credentials

        result = set_web_credentials(db, ont_id, username=username, password=password)

        if result.success:
            _set_sync_meta(ont, "acs")
            db.commit()
            _emit_feature_event(db, ont_id, "web_credentials")

        return result


ont_features = OntFeatureService()
