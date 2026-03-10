"""Integration connector registry and discovery helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectorRegistryEntry:
    key: str
    name: str
    version: str
    connector_type: str
    description: str
    module_name: str
    file_size_bytes: int


_STATIC_CATALOG: dict[str, dict[str, str]] = {
    "quickbooks": {
        "name": "QuickBooks Online",
        "version": "1.0.0",
        "connector_type": "accounting",
        "description": "Bidirectional invoice/payment/customer sync.",
    },
    "xero": {
        "name": "Xero",
        "version": "1.0.0",
        "connector_type": "accounting",
        "description": "Accounting synchronization for invoices and payments.",
    },
    "sage": {
        "name": "Sage",
        "version": "1.0.0",
        "connector_type": "accounting",
        "description": "Accounting connector for African market deployments.",
    },
    "whatsapp": {
        "name": "WhatsApp",
        "version": "1.0.0",
        "connector_type": "messaging",
        "description": "Template and notification messaging connector.",
    },
    "paystack": {
        "name": "Paystack",
        "version": "1.0.0",
        "connector_type": "payment",
        "description": "Online payment gateway integration.",
    },
    "flutterwave": {
        "name": "Flutterwave",
        "version": "1.0.0",
        "connector_type": "payment",
        "description": "Online payment gateway integration.",
    },
    "3cx": {
        "name": "3CX",
        "version": "1.0.0",
        "connector_type": "voice",
        "description": "Embedded PBX integration frame.",
    },
    "freepbx": {
        "name": "FreePBX",
        "version": "1.0.0",
        "connector_type": "voice",
        "description": "Embedded PBX integration frame.",
    },
}


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def discover_connectors() -> list[ConnectorRegistryEntry]:
    """Auto-discover connector modules from the connectors directory."""
    connectors_dir = Path(__file__).resolve().parent / "connectors"
    entries: dict[str, ConnectorRegistryEntry] = {}

    for module_path in connectors_dir.glob("*.py"):
        if module_path.name == "__init__.py":
            continue
        key = module_path.stem.lower()
        stat = module_path.stat()
        catalog = _STATIC_CATALOG.get(key, {})
        entries[key] = ConnectorRegistryEntry(
            key=key,
            name=catalog.get("name", _humanize(key)),
            version=catalog.get("version", "1.0.0"),
            connector_type=catalog.get("connector_type", "custom"),
            description=catalog.get("description", f"{_humanize(key)} connector"),
            module_name=f"app.services.integrations.connectors.{module_path.stem}",
            file_size_bytes=int(stat.st_size),
        )

    for key, catalog in _STATIC_CATALOG.items():
        if key in entries:
            continue
        entries[key] = ConnectorRegistryEntry(
            key=key,
            name=catalog.get("name", _humanize(key)),
            version=catalog.get("version", "1.0.0"),
            connector_type=catalog.get("connector_type", "custom"),
            description=catalog.get("description", f"{_humanize(key)} connector"),
            module_name=f"catalog:{key}",
            file_size_bytes=0,
        )

    return sorted(entries.values(), key=lambda item: item.name.lower())
