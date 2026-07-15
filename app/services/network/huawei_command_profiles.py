"""Huawei OLT command grammar helpers."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.network import OLTDevice
from app.services.adapters.olt_types import olt_type_registry


@dataclass(frozen=True)
class HuaweiCommandProfile:
    name: str
    requires_slow_send: bool = True
    supports_slash_fsp_display: bool = False

    def display_ont_info(self, fsp: str, ont_id: int) -> str:
        frame, slot, port = _split_fsp(fsp, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont info {frame}/{slot} {ont_id}"
            return f"display ont info {frame}/{slot} {port} {ont_id}"
        if port is None:
            raise ValueError("Full F/S/P is required for single-ONT display syntax")
        return f"display ont info {frame} {slot} {port} {ont_id}"

    def display_ont_optical_info(self, fsp: str, ont_id: int) -> str:
        frame, slot, port = _split_fsp(fsp, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont optical-info {frame}/{slot} {ont_id}"
            return f"display ont optical-info {frame}/{slot} {port} {ont_id}"
        if port is None:
            raise ValueError("Full F/S/P is required for single-ONT optical syntax")
        return f"display ont optical-info {frame} {slot} {port} {ont_id}"

    def display_ont_info_all(self, fsp_or_board: str) -> str:
        frame, slot, port = _split_fsp(fsp_or_board, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont info {frame}/{slot} all"
            return f"display ont info {frame}/{slot} {port} all"
        if port is None:
            return f"display ont info {frame} {slot} all"
        return f"display ont info {frame} {slot} {port} all"

    def display_ont_status_inventory(self, fsp: str) -> str:
        """Return the bounded per-port status inventory command."""
        frame, slot, port = _split_fsp(fsp)
        if self.supports_slash_fsp_display:
            return f"display ont info summary {frame}/{slot}/{port}"
        return f"display ont info {frame} {slot} {port} all"


def _split_fsp(
    fsp: str,
    *,
    allow_board: bool = False,
) -> tuple[str, str, str | None]:
    parts = str(fsp or "").split("/")
    if len(parts) == 2 and allow_board:
        return parts[0], parts[1], None
    if len(parts) != 3:
        raise ValueError(f"Invalid F/S/P format: {fsp!r}")
    return parts[0], parts[1], parts[2]


def get_huawei_command_profile(olt: OLTDevice) -> HuaweiCommandProfile:
    model = str(getattr(olt, "model", None) or "").upper()
    capabilities = olt_type_registry.get_capabilities(
        model=getattr(olt, "model", None),
        firmware=(
            getattr(olt, "firmware_version", None)
            or getattr(olt, "software_version", None)
        ),
    )
    return HuaweiCommandProfile(
        name=capabilities.command_profile_name or "huawei-generic",
        requires_slow_send=capabilities.requires_slow_send,
        supports_slash_fsp_display=(
            capabilities.supports_slash_fsp_display or "MA5800" in model
        ),
    )
