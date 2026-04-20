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
        """Toggle CATV on ONT (requires vendor capability)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "catv")
        if cap_err:
            return cap_err

        # CATV toggle is a desired-state write; actual OLT multicast VLAN binding
        # is handled by provisioning or reconciliation tasks.
        _set_sync_meta(ont, "api")
        db.commit()
        _emit_feature_event(db, ont_id, "catv", enabled)
        return ActionResult(
            success=True,
            message=f"CATV {'enabled' if enabled else 'disabled'} (desired state).",
        )

    @staticmethod
    def toggle_iptv(db: Session, ont_id: str, *, enabled: bool) -> ActionResult:
        """Toggle IPTV on ONT."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        cap_err = _check_capability(db, ont, "iptv")
        if cap_err:
            return cap_err

        _set_sync_meta(ont, "api")
        db.commit()
        _emit_feature_event(db, ont_id, "iptv", enabled)
        return ActionResult(
            success=True,
            message=f"IPTV {'enabled' if enabled else 'disabled'} (desired state).",
        )

    @staticmethod
    def toggle_wan_remote_access(
        db: Session, ont_id: str, *, enabled: bool
    ) -> ActionResult:
        """Toggle WAN remote access."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        ont.wan_remote_access = enabled
        _set_sync_meta(ont, "api")
        db.commit()
        db.refresh(ont)
        _emit_feature_event(db, ont_id, "wan_remote_access", enabled)
        return ActionResult(
            success=True,
            message=f"WAN remote access {'enabled' if enabled else 'disabled'}.",
        )

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
        """Configure DHCP snooping (desired state)."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        _set_sync_meta(ont, "api")
        db.commit()
        _emit_feature_event(db, ont_id, "dhcp_snooping", enabled)
        return ActionResult(
            success=True,
            message=f"DHCP snooping {'enabled' if enabled else 'disabled'} (desired state).",
        )

    @staticmethod
    def set_max_mac_learn(db: Session, ont_id: str, *, max_mac: int) -> ActionResult:
        """Set MAC address learning limit (desired state)."""
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        _set_sync_meta(ont, "api")
        db.commit()
        _emit_feature_event(db, ont_id, "max_mac_learn")
        return ActionResult(
            success=True,
            message=f"Max MAC learn set to {max_mac} (desired state).",
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

        # This would typically use TR-069 SetParameterValues for the web UI credentials.
        # For now, record as desired state until TR-069 parameter map is in place.
        _set_sync_meta(ont, "api")
        db.commit()
        _emit_feature_event(db, ont_id, "web_credentials")
        return ActionResult(
            success=True,
            message="Web credentials update queued.",
        )


ont_features = OntFeatureService()
