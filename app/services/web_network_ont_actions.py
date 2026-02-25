"""Service helpers for remote ONT action web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.network.ont_actions import ActionResult, OntActions

logger = logging.getLogger(__name__)


def execute_reboot(db: Session, ont_id: str) -> ActionResult:
    """Execute reboot action and return result."""
    return OntActions.reboot(db, ont_id)


def execute_refresh(db: Session, ont_id: str) -> ActionResult:
    """Execute status refresh and return result."""
    return OntActions.refresh_status(db, ont_id)


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def execute_factory_reset(db: Session, ont_id: str) -> ActionResult:
    """Execute factory reset and return result."""
    return OntActions.factory_reset(db, ont_id)


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    """Set WiFi SSID and return result."""
    return OntActions.set_wifi_ssid(db, ont_id, ssid)


def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
    """Set WiFi password and return result."""
    return OntActions.set_wifi_password(db, ont_id, password)


def toggle_lan_port(
    db: Session, ont_id: str, port: int, enabled: bool
) -> ActionResult:
    """Toggle a LAN port and return result."""
    return OntActions.toggle_lan_port(db, ont_id, port, enabled)
