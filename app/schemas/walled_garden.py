"""Typed value contract for the walled-garden allowed-resource setting.

``radius.walled_garden_allowed_resources`` is a JSON object
``{"entries": [...]}``. Each entry is one NAMED resource (a payment provider,
support site, ...) that suspended subscribers may reach while captive. Entries
are individually enabled or disabled; the router module renders address-list
elements only for enabled entries, each tagged with the entry key.

The captive portal itself is NOT an entry here: it is derived from
``radius.captive_portal_url``/``captive_portal_ip`` and is always present, so
the reserved key ``portal`` and kind ``portal`` are refused in this setting.
"""

from __future__ import annotations

import ipaddress
import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WALLED_GARDEN_PORTAL_ENTRY_KEY = "portal"
WALLED_GARDEN_MAX_HOSTS_PER_ENTRY = 32
WALLED_GARDEN_MAX_ENTRIES = 64

_ENTRY_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class WalledGardenResourceKind(StrEnum):
    portal = "portal"
    payment = "payment"
    dns = "dns"
    support = "support"
    other = "other"


def normalize_walled_garden_hostname(value: object) -> str:
    """Return a lower-case RFC 1123 FQDN or raise ``ValueError``.

    IP literals, wildcards, single-label names, ports, schemes, and paths are
    refused: RouterOS 7 resolves FQDN address-list entries itself, and an
    operator-entered IP would bypass the named-resource audit trail.
    """

    if not isinstance(value, str):
        raise ValueError("hostname must be a string")
    host = value.strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise ValueError(f"invalid hostname {value!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"IP literals are not hostnames: {value!r}")
    labels = host.split(".")
    if len(labels) < 2:
        raise ValueError(f"hostname must be fully qualified: {value!r}")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError(f"invalid hostname {value!r}")
    if labels[-1].isdigit():
        raise ValueError(f"invalid top-level domain in {value!r}")
    return host


class WalledGardenAllowedResource(BaseModel):
    """One named, individually toggleable allowed resource."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str = Field(min_length=1, max_length=80)
    kind: WalledGardenResourceKind
    hosts: tuple[str, ...] = Field(min_length=1)
    enabled: bool = False

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        key = value.strip()
        if not _ENTRY_KEY_RE.fullmatch(key):
            raise ValueError(
                "entry key must be 1-40 lower-case letters, digits, or inner '-'"
            )
        if key == WALLED_GARDEN_PORTAL_ENTRY_KEY:
            raise ValueError(
                "'portal' is reserved: the portal entry is derived from "
                "captive_portal_url and cannot be configured here"
            )
        return key

    @field_validator("label")
    @classmethod
    def _valid_label(cls, value: str) -> str:
        label = value.strip()
        if not label or any(ch in label for ch in '"\\\r\n'):
            raise ValueError("label must be non-blank and contain no quotes")
        return label

    @field_validator("kind")
    @classmethod
    def _not_portal(cls, value: WalledGardenResourceKind) -> WalledGardenResourceKind:
        if value is WalledGardenResourceKind.portal:
            raise ValueError("kind 'portal' is reserved for the derived portal entry")
        return value

    @field_validator("hosts", mode="before")
    @classmethod
    def _valid_hosts(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("hosts must be a list of hostnames")
        hosts = tuple(normalize_walled_garden_hostname(item) for item in value)
        if len(set(hosts)) != len(hosts):
            raise ValueError("hosts must be unique within an entry")
        if len(hosts) > WALLED_GARDEN_MAX_HOSTS_PER_ENTRY:
            raise ValueError(
                f"an entry may list at most {WALLED_GARDEN_MAX_HOSTS_PER_ENTRY} hosts"
            )
        return hosts


class WalledGardenAllowedResources(BaseModel):
    """The whole ``radius.walled_garden_allowed_resources`` value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[WalledGardenAllowedResource, ...] = ()

    @model_validator(mode="after")
    def _unique_keys(self) -> WalledGardenAllowedResources:
        keys = [entry.key for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("entry keys must be unique")
        if len(keys) > WALLED_GARDEN_MAX_ENTRIES:
            raise ValueError(f"at most {WALLED_GARDEN_MAX_ENTRIES} entries")
        return self

    @classmethod
    def from_setting_value(cls, value: object) -> WalledGardenAllowedResources:
        """Parse a stored setting value (object or JSON text); raises on error."""

        if value is None:
            return cls()
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return cls()
            value = json.loads(text)
        return cls.model_validate(value)


PAYSTACK_PRESET = WalledGardenAllowedResource(
    key="paystack",
    label="Paystack",
    kind=WalledGardenResourceKind.payment,
    hosts=(
        "checkout.paystack.com",
        "api.paystack.co",
        "js.paystack.co",
        "standard.paystack.co",
    ),
    enabled=False,
)

#: Seeded default. Presets ship DISABLED; an operator enables them explicitly.
DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES: dict[str, object] = {
    "entries": [PAYSTACK_PRESET.model_dump(mode="json")],
}


def walled_garden_allowed_resources_error(value: object) -> str | None:
    """Return a safe validation message, or ``None`` when the value is valid."""

    try:
        WalledGardenAllowedResources.from_setting_value(value)
    except (ValueError, TypeError) as exc:
        return f"Invalid walled-garden allowed resources: {exc}"
    return None


__all__ = [
    "DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES",
    "PAYSTACK_PRESET",
    "WALLED_GARDEN_PORTAL_ENTRY_KEY",
    "WalledGardenAllowedResource",
    "WalledGardenAllowedResources",
    "WalledGardenResourceKind",
    "normalize_walled_garden_hostname",
    "walled_garden_allowed_resources_error",
]
