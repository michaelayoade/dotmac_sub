"""Seed vendor model capabilities with TR-069 parameter map overrides.

Called at startup from ``settings_seed.py``.  Only inserts rows when the
``vendor_model_capabilities`` table is empty, so it is safe to call
repeatedly (idempotent).

Vendor-specific ``Tr069ParameterMap`` entries are only created where the
device's actual path **deviates** from the standard TR-181/TR-098 path.
Standard paths do not need DB rows — the ``Tr069PathResolver`` handles
them via its built-in dicts.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import Tr069ParameterMap, VendorModelCapability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed definitions
# ---------------------------------------------------------------------------

_VENDOR_SEEDS: list[dict[str, Any]] = [
    # ── Huawei HG8145V5 (TR-181 / Device) ──────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8145V5",
        "tr069_root": "Device",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 2,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": True,
        "supported_features": {"wifi": True, "voip": False, "catv": False},
        "notes": "Common GPON ONT. TR-181 data model.",
        "parameter_overrides": [
            # Huawei uses KeyPassphrase, not PreSharedKey, for the WiFi password
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "WiFi.AccessPoint.{i}.Security.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase path",
            },
        ],
    },
    # ── Huawei HG8245H (TR-098 / InternetGatewayDevice) ────────────────
    {
        "vendor": "Huawei",
        "model": "HG8245H",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 2,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "Older GPON ONT with VoIP. TR-098 data model.",
        "parameter_overrides": [
            {
                "canonical_name": "system.mac_address",
                "tr069_path": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MACAddress",
                "writable": False,
                "value_type": "string",
                "notes": "Huawei exposes MAC on WAN PPP connection",
            },
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── ZTE F660 (TR-098 / InternetGatewayDevice) ──────────────────────
    {
        "vendor": "ZTE",
        "model": "F660",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 4,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": True,
        "supports_ipv6": True,
        "supported_features": {"wifi": True, "voip": True, "catv": True},
        "notes": "ZTE GPON ONT with multiple WAN services. TR-098 data model.",
        "parameter_overrides": [],
    },
    # ── Nokia G-010G-A (TR-181 / Device) ────────────────────────────────
    {
        "vendor": "Nokia",
        "model": "G-010G-A",
        "tr069_root": "Device",
        "max_wan_services": 1,
        "max_lan_ports": 1,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": True,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "Nokia SFP ONT (no WiFi, single LAN). TR-181 data model.",
        "parameter_overrides": [],
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_vendor_capabilities(db: Session) -> int:
    """Populate vendor model capabilities if the table is empty.

    Returns:
        Number of capability rows inserted.
    """
    count = db.scalar(
        select(func.count()).select_from(VendorModelCapability)
    )
    if count and count > 0:
        logger.debug(
            "Vendor capabilities table already has %d rows, skipping seed.", count,
        )
        return 0

    inserted = 0
    for seed in _VENDOR_SEEDS:
        seed = dict(seed)  # shallow copy to avoid mutating module-level data
        overrides = seed.pop("parameter_overrides", [])
        cap = VendorModelCapability(**seed)
        db.add(cap)
        db.flush()  # get cap.id for FK

        for override in overrides:
            param_map = Tr069ParameterMap(
                capability_id=cap.id,
                **override,
            )
            db.add(param_map)

        inserted += 1
        logger.info(
            "Seeded vendor capability: %s %s (root: %s, %d overrides)",
            cap.vendor,
            cap.model,
            cap.tr069_root,
            len(overrides),
        )

    db.commit()
    logger.info("Seeded %d vendor model capabilities.", inserted)
    return inserted
