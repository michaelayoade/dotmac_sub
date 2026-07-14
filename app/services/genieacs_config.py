"""Authoritative managed GenieACS configuration entries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

GENIEACS_CPE_AUTH_EXPRESSION: Final = (
    'EXT("auth", "authenticateCpe", username, password, '
    "DeviceID.ID, DeviceID.SerialNumber)"
)
GENIEACS_CONNECTION_REQUEST_AUTH_EXPRESSION: Final = (
    'AUTH(EXT("auth", "connectionRequestUsername", DeviceID.SerialNumber), '
    'EXT("auth", "connectionRequestPassword", DeviceID.SerialNumber))'
)

GENIEACS_CONFIG_ENTRIES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cwmp.auth": GENIEACS_CPE_AUTH_EXPRESSION,
        "cwmp.connectionRequestAuth": (GENIEACS_CONNECTION_REQUEST_AUTH_EXPRESSION),
    }
)
