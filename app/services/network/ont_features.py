"""Per-feature toggle interface for ONTs with vendor capability validation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.ont_action_common import (
    ActionResult,
    get_ont_or_error,
    get_ont_strict_or_error,
)

logger = logging.getLogger(__name__)


def _acs_config_writer():
    from app.services.acs_client import create_acs_config_writer

    return create_acs_config_writer()


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
        """Set WiFi configuration via TR-069."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "wifi")
        if cap_err:
            return cap_err

        if ssid is not None:
            result = _acs_config_writer().set_wifi_ssid(db, ont_id, ssid)
            if not result.success:
                return result

        if password is not None:
            result = _acs_config_writer().set_wifi_password(db, ont_id, password)
            if not result.success:
                return result

        _set_sync_meta(ont, "tr069")
        db.commit()
        _emit_feature_event(db, ont_id, "wifi", enabled)
        return ActionResult(success=True, message="WiFi configuration updated.")

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

        # Require ACS connectivity
        if not getattr(ont, "tr069_acs_server_id", None):
            return ActionResult(
                success=False,
                message="ONT has no ACS server configured. Cannot push remote access config.",
            )

        # Push via TR-069
        from app.services.network.ont_action_remote_access import (
            set_wan_remote_access_best_effort,
        )

        result = set_wan_remote_access_best_effort(
            db, ont_id, enabled=enabled, protocol="ssh"
        )

        if result.success:
            ont.wan_remote_access = enabled
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

        result = _acs_config_writer().toggle_lan_port(db, ont_id, port_number, enabled)
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

        # Require ACS connectivity
        if not getattr(ont, "tr069_acs_server_id", None):
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
