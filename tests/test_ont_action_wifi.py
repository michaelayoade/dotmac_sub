"""Compatibility WiFi actions delegate to reconciled desired state."""

from __future__ import annotations

from unittest.mock import patch

from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_action_wifi import (
    set_wifi_config,
    set_wifi_password,
    set_wifi_ssid,
    toggle_lan_port,
)


def _success(*args, **kwargs) -> ActionResult:
    return ActionResult(success=True, message="ok")


def test_set_wifi_ssid_delegates_to_feature_reconciler() -> None:
    db = object()
    with patch(
        "app.services.network.ont_features.OntFeatureService.set_wifi_config",
        side_effect=_success,
    ) as reconcile:
        result = set_wifi_ssid(db, "ont-1", "DOTMAC")  # type: ignore[arg-type]

    assert result.success is True
    reconcile.assert_called_once_with(db, "ont-1", ssid="DOTMAC")


def test_set_wifi_password_delegates_to_feature_reconciler() -> None:
    db = object()
    with patch(
        "app.services.network.ont_features.OntFeatureService.set_wifi_config",
        side_effect=_success,
    ) as reconcile:
        result = set_wifi_password(db, "ont-1", "Secret123")  # type: ignore[arg-type]

    assert result.success is True
    reconcile.assert_called_once_with(db, "ont-1", password="Secret123")


def test_set_wifi_config_delegates_every_field_in_one_call() -> None:
    db = object()
    with patch(
        "app.services.network.ont_features.OntFeatureService.set_wifi_config",
        side_effect=_success,
    ) as reconcile:
        result = set_wifi_config(
            db,  # type: ignore[arg-type]
            "ont-1",
            enabled=False,
            ssid="DOTMAC",
            password="Secret123",
            channel=6,
            security_mode="WPA2-Personal",
        )

    assert result.success is True
    reconcile.assert_called_once_with(
        db,
        "ont-1",
        enabled=False,
        ssid="DOTMAC",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
    )


def test_toggle_lan_port_remains_on_network_compatibility_writer() -> None:
    db = object()
    with patch(
        "app.services.network.ont_action_network.toggle_lan_port",
        side_effect=_success,
    ) as writer:
        result = toggle_lan_port(db, "ont-1", 2, False)  # type: ignore[arg-type]

    assert result.success is True
    writer.assert_called_once_with(db, "ont-1", 2, False)
