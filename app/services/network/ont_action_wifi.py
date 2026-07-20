"""Compatibility facade for WiFi actions owned by the ONT reconciler."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.network.ont_action_common import ActionResult


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    from app.services.network.ont_features import OntFeatureService

    return OntFeatureService.set_wifi_config(db, ont_id, ssid=ssid)


def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
    from app.services.network.ont_features import OntFeatureService

    return OntFeatureService.set_wifi_config(db, ont_id, password=password)


def set_wifi_config(
    db: Session,
    ont_id: str,
    *,
    enabled: bool | None = None,
    ssid: str | None = None,
    password: str | None = None,
    channel: int | None = None,
    security_mode: str | None = None,
) -> ActionResult:
    from app.services.network.ont_features import OntFeatureService

    return OntFeatureService.set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=ssid,
        password=password,
        channel=channel,
        security_mode=security_mode,
    )


def toggle_lan_port(
    db: Session,
    ont_id: str,
    port: int,
    enabled: bool,
) -> ActionResult:
    from app.services.network.ont_action_network import toggle_lan_port as _toggle

    return _toggle(db, ont_id, port, enabled)


__all__ = (
    "set_wifi_config",
    "set_wifi_password",
    "set_wifi_ssid",
    "toggle_lan_port",
)
