"""Versioned walled-garden RouterOS module rendered from canonical settings.

Owner: ``access.walled_garden_router_module`` (read-only policy).

With ``radius.group_routing_enabled=false`` a captive subscriber receives a
normal session plus ``Mikrotik-Address-List := <suspended_address_list>``.
Enforcement then lives on the router firewall. This owner renders that
firewall contract as one deterministic, versioned RouterOS command set in
which every element carries a ``dotmac-wg:v1:<element>`` comment tag, so it can
be found, verified, re-rendered and removed without touching anything else.

It renders and self-verifies only. It never connects to a router, never emits
removal of the hand-made legacy quarantine (that is returned as an explicit,
typed worklist for a separately approved operator step), and never touches
static ``suspended`` address-list entries.

Allowed resources are NAMED, individually enabled entries from
``radius.walled_garden_allowed_resources``. The portal entry is derived from
``captive_portal_url``/``captive_portal_ip`` and always present. Each entry's
address-list rows are tagged ``dotmac-wg:v1:allow:<key>:<host>``, so enabling
or disabling one entry changes only that entry's elements.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.schemas.walled_garden import (
    WALLED_GARDEN_PORTAL_ENTRY_KEY,
    WalledGardenAllowedResource,
    WalledGardenAllowedResources,
    WalledGardenResourceKind,
    normalize_walled_garden_hostname,
)
from app.services import settings_spec
from app.services.radius_address_lists import suspended_address_list
from app.services.router_management.connection import check_dangerous_commands
from app.services.router_management.write_adapter import (
    RouterWriteAdapterError,
    parse_routeros_rest_commands,
)

logger = logging.getLogger(__name__)

MODULE_NAME = "dotmac-wg"
MODULE_VERSION = "v1"
TAG_ROOT = f"{MODULE_NAME}:"
TAG_PREFIX = f"{MODULE_NAME}:{MODULE_VERSION}:"
ALLOW_LIST = "dotmac-wg-allow"
MODULE_CHAIN = "dotmac-wg"
LEGACY_QUARANTINE_JUMP_COMMENT = "dotmac suspended quarantine"

DNS_DST_LIMIT = "20,40,src-address/1m"
REJECT_LIMIT = "20,40:packet"

_ADDRESS_LIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_BARE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/,!-]+$")


class WalledGardenModuleErrorCode(StrEnum):
    PORTAL_URL_INVALID = "portal_url_invalid"
    PORTAL_IP_INVALID = "portal_ip_invalid"
    SUSPENDED_ADDRESS_LIST_INVALID = "suspended_address_list_invalid"
    ALLOWED_RESOURCES_INVALID = "allowed_resources_invalid"
    UNSAFE_RENDER = "unsafe_render"


class WalledGardenModuleError(Exception):
    """Transport-neutral refusal to render the walled-garden module."""

    def __init__(self, code: WalledGardenModuleErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WalledGardenRouterResource(StrEnum):
    address_list = "address_list"
    filter = "filter"
    nat = "nat"

    @property
    def rest_path(self) -> str:
        return _REST_PATHS[self]

    @property
    def cli_path(self) -> str:
        return _CLI_PATHS[self]


_REST_PATHS: dict[WalledGardenRouterResource, str] = {
    WalledGardenRouterResource.address_list: "/ip/firewall/address-list",
    WalledGardenRouterResource.filter: "/ip/firewall/filter",
    WalledGardenRouterResource.nat: "/ip/firewall/nat",
}
_CLI_PATHS: dict[WalledGardenRouterResource, str] = {
    WalledGardenRouterResource.address_list: "/ip firewall address-list",
    WalledGardenRouterResource.filter: "/ip firewall filter",
    WalledGardenRouterResource.nat: "/ip firewall nat",
}


@dataclass(frozen=True, slots=True)
class WalledGardenPlacement:
    """Where an element must sit relative to existing rules.

    ``before_comment`` names the legacy anchor the element must precede when it
    exists; otherwise the element goes to the top of ``chain``. REST ``add``
    cannot express this without a device-local ``.id``, so the directive is
    carried as data and the readiness verifier checks the observed order.
    """

    chain: str
    before_comment: str | None


@dataclass(frozen=True, slots=True)
class WalledGardenAllowEntry:
    """One rendered allowed resource (derived portal or configured entry)."""

    key: str
    label: str
    kind: WalledGardenResourceKind
    hosts: tuple[str, ...]
    enabled: bool
    derived: bool

    @property
    def tag_prefix(self) -> str:
        return f"{TAG_PREFIX}allow:{self.key}:"


@dataclass(frozen=True, slots=True)
class WalledGardenElement:
    """One module-owned RouterOS row, identified by its comment tag."""

    resource: WalledGardenRouterResource
    name: str
    fields: tuple[tuple[str, str], ...]
    #: Fields RouterOS may re-normalise on export; verified by presence only.
    presence_only_fields: frozenset[str] = frozenset()
    entry_key: str | None = None
    placement: WalledGardenPlacement | None = None

    @property
    def tag(self) -> str:
        return f"{TAG_PREFIX}{self.name}"

    def field_value(self, key: str) -> str | None:
        return next((value for name, value in self.fields if name == key), None)

    def rest_payload(self) -> dict[str, str]:
        return {**dict(self.fields), "comment": self.tag}

    def rest_command(self) -> str:
        payload = json.dumps(self.rest_payload(), separators=(",", ":"))
        return f"{self.resource.rest_path}/add {payload}"

    def cli_add(self, *, extra: str = "") -> str:
        words = " ".join(f"{key}={_cli_value(value)}" for key, value in self.fields)
        suffix = f" {extra}" if extra else ""
        return (
            f"{self.resource.cli_path} add {words} "
            f"comment={_cli_quote(self.tag)}{suffix}"
        )


class LegacyQuarantineRole(StrEnum):
    forward_jump = "forward_jump"
    chain_rule = "chain_rule"
    splynx_blocked_portal = "splynx_blocked_portal"


@dataclass(frozen=True, slots=True)
class LegacyQuarantineElement:
    """A hand-made pre-module quarantine rule, matched by its exact comment."""

    resource: WalledGardenRouterResource
    chain: str
    comment: str
    role: LegacyQuarantineRole


LEGACY_QUARANTINE_ELEMENTS: tuple[LegacyQuarantineElement, ...] = (
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "forward",
        LEGACY_QUARANTINE_JUMP_COMMENT,
        LegacyQuarantineRole.forward_jump,
    ),
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "dotmac-suspended",
        "dotmac suspended allow limited DNS",
        LegacyQuarantineRole.chain_rule,
    ),
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "dotmac-suspended",
        "dotmac suspended portal",
        LegacyQuarantineRole.chain_rule,
    ),
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "dotmac-suspended",
        "dotmac suspended reject limited",
        LegacyQuarantineRole.chain_rule,
    ),
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "dotmac-suspended",
        "dotmac suspended drop",
        LegacyQuarantineRole.chain_rule,
    ),
    LegacyQuarantineElement(
        WalledGardenRouterResource.filter,
        "splynx-blocked",
        "dotmac portal allow (suspended)",
        LegacyQuarantineRole.splynx_blocked_portal,
    ),
)
LEGACY_QUARANTINE_COMMENTS: frozenset[str] = frozenset(
    item.comment for item in LEGACY_QUARANTINE_ELEMENTS
)


@dataclass(frozen=True, slots=True)
class WalledGardenModuleConfig:
    """Validated settings inputs for one render."""

    portal_url: str
    portal_host: str
    portal_ip: ipaddress.IPv4Address
    suspended_address_list: str
    resources: tuple[WalledGardenAllowedResource, ...] = ()


@dataclass(frozen=True, slots=True)
class WalledGardenRouterModule:
    """The rendered, self-verified v1 module."""

    version: str
    config: WalledGardenModuleConfig
    entries: tuple[WalledGardenAllowEntry, ...]
    elements: tuple[WalledGardenElement, ...]
    legacy_elements_to_retire: tuple[LegacyQuarantineElement, ...] = field(
        default=LEGACY_QUARANTINE_ELEMENTS
    )

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(element.tag for element in self.elements)

    def entry(self, key: str) -> WalledGardenAllowEntry | None:
        return next((item for item in self.entries if item.key == key), None)

    def elements_for_entry(self, key: str) -> tuple[WalledGardenElement, ...]:
        return tuple(item for item in self.elements if item.entry_key == key)

    def rest_commands(self) -> tuple[str, ...]:
        """REST plan form (``add`` only) accepted by the write adapter parser."""

        return tuple(element.rest_command() for element in self.elements)

    def routeros_script(self) -> str:
        """Idempotent operator script: remove own tags, then re-add in order."""

        return _render_script(self)


def _cli_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def _cli_value(value: str) -> str:
    return value if _BARE_VALUE_RE.fullmatch(value) else _cli_quote(value)


def _portal_host(portal_url: str) -> str:
    parsed = urlparse(portal_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.PORTAL_URL_INVALID,
            "captive_portal_url must be an https URL with a hostname",
        )
    try:
        return normalize_walled_garden_hostname(parsed.hostname)
    except ValueError as exc:
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.PORTAL_URL_INVALID, str(exc)
        ) from exc


def _portal_ip(value: str) -> ipaddress.IPv4Address:
    text = value.strip()
    try:
        interface = ipaddress.ip_interface(text)
    except ValueError as exc:
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.PORTAL_IP_INVALID,
            "captive_portal_ip must be a single IPv4 address",
        ) from exc
    if (
        not isinstance(interface, ipaddress.IPv4Interface)
        or interface.network.prefixlen != 32
    ):
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.PORTAL_IP_INVALID,
            "captive_portal_ip must be a single IPv4 address (dst-nat target)",
        )
    return interface.ip


def build_module_config(
    *,
    portal_url: str,
    portal_ip: str,
    suspended_address_list: str,
    allowed_resources: object,
) -> WalledGardenModuleConfig:
    """Validate raw setting values into a render config; fails closed."""

    list_name = suspended_address_list.strip()
    if not _ADDRESS_LIST_NAME_RE.fullmatch(list_name) or list_name == ALLOW_LIST:
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.SUSPENDED_ADDRESS_LIST_INVALID,
            "suspended_address_list must be a plain RouterOS list name",
        )
    if isinstance(allowed_resources, WalledGardenAllowedResources):
        resources = allowed_resources
    else:
        try:
            resources = WalledGardenAllowedResources.from_setting_value(
                allowed_resources
            )
        except (ValueError, TypeError) as exc:
            raise WalledGardenModuleError(
                WalledGardenModuleErrorCode.ALLOWED_RESOURCES_INVALID,
                f"walled_garden_allowed_resources is invalid: {exc}",
            ) from exc
    return WalledGardenModuleConfig(
        portal_url=portal_url.strip(),
        portal_host=_portal_host(portal_url),
        portal_ip=_portal_ip(portal_ip),
        suspended_address_list=list_name,
        resources=resources.entries,
    )


def load_module_config(db: Session) -> WalledGardenModuleConfig:
    """Read the canonical settings through ``control.settings_spec``."""

    return build_module_config(
        portal_url=str(
            settings_spec.resolve_value(db, SettingDomain.radius, "captive_portal_url")
            or ""
        ),
        portal_ip=str(
            settings_spec.resolve_value(db, SettingDomain.radius, "captive_portal_ip")
            or ""
        ),
        suspended_address_list=suspended_address_list(db),
        allowed_resources=settings_spec.resolve_value(
            db, SettingDomain.radius, "walled_garden_allowed_resources"
        ),
    )


def _entries(config: WalledGardenModuleConfig) -> tuple[WalledGardenAllowEntry, ...]:
    portal = WalledGardenAllowEntry(
        key=WALLED_GARDEN_PORTAL_ENTRY_KEY,
        label="Captive portal",
        kind=WalledGardenResourceKind.portal,
        hosts=(str(config.portal_ip), config.portal_host),
        enabled=True,
        derived=True,
    )
    configured = tuple(
        WalledGardenAllowEntry(
            key=item.key,
            label=item.label,
            kind=item.kind,
            hosts=item.hosts,
            enabled=item.enabled,
            derived=False,
        )
        for item in config.resources
    )
    return (portal, *configured)


def _elements(
    config: WalledGardenModuleConfig,
    entries: Sequence[WalledGardenAllowEntry],
) -> tuple[WalledGardenElement, ...]:
    addr = WalledGardenRouterResource.address_list
    flt = WalledGardenRouterResource.filter
    nat = WalledGardenRouterResource.nat
    suspended = config.suspended_address_list
    portal_ip = str(config.portal_ip)

    allow = tuple(
        WalledGardenElement(
            resource=addr,
            name=f"allow:{entry.key}:{host}",
            fields=(("list", ALLOW_LIST), ("address", host)),
            entry_key=entry.key,
        )
        for entry in entries
        if entry.enabled
        for host in entry.hosts
    )
    chain = (
        WalledGardenElement(
            resource=flt,
            name="chain:dns-udp",
            fields=(
                ("chain", MODULE_CHAIN),
                ("action", "accept"),
                ("protocol", "udp"),
                ("dst-port", "53"),
                ("dst-limit", DNS_DST_LIMIT),
            ),
            presence_only_fields=frozenset({"dst-limit"}),
        ),
        WalledGardenElement(
            resource=flt,
            name="chain:dns-tcp",
            fields=(
                ("chain", MODULE_CHAIN),
                ("action", "accept"),
                ("protocol", "tcp"),
                ("dst-port", "53"),
                ("dst-limit", DNS_DST_LIMIT),
            ),
            presence_only_fields=frozenset({"dst-limit"}),
        ),
        WalledGardenElement(
            resource=flt,
            name="chain:allow-web",
            fields=(
                ("chain", MODULE_CHAIN),
                ("action", "accept"),
                ("protocol", "tcp"),
                ("dst-port", "80,443"),
                ("dst-address-list", ALLOW_LIST),
            ),
        ),
        WalledGardenElement(
            resource=flt,
            name="chain:reject",
            fields=(
                ("chain", MODULE_CHAIN),
                ("action", "reject"),
                ("reject-with", "icmp-admin-prohibited"),
                ("limit", REJECT_LIMIT),
            ),
            presence_only_fields=frozenset({"limit"}),
        ),
        WalledGardenElement(
            resource=flt,
            name="chain:drop",
            fields=(("chain", MODULE_CHAIN), ("action", "drop")),
        ),
    )
    redirect = WalledGardenElement(
        resource=nat,
        name="nat:http-redirect",
        fields=(
            ("chain", "dstnat"),
            ("action", "dst-nat"),
            ("protocol", "tcp"),
            ("dst-port", "80"),
            ("src-address-list", suspended),
            ("dst-address-list", f"!{ALLOW_LIST}"),
            ("to-addresses", portal_ip),
            ("to-ports", "80"),
        ),
        placement=WalledGardenPlacement(chain="dstnat", before_comment=None),
    )
    # No dst-address-list exclusion on the jump: while the legacy quarantine
    # still follows this rule, an excluded allow-list destination would fall
    # through to the legacy chain (whose allow list is empty) and be dropped.
    # The module chain accepts allowed web traffic itself, which is final.
    jump = WalledGardenElement(
        resource=flt,
        name="forward:jump",
        fields=(
            ("chain", "forward"),
            ("action", "jump"),
            ("jump-target", MODULE_CHAIN),
            ("src-address-list", suspended),
        ),
        placement=WalledGardenPlacement(
            chain="forward", before_comment=LEGACY_QUARANTINE_JUMP_COMMENT
        ),
    )
    return (*allow, *chain, redirect, jump)


def _placed_add(element: WalledGardenElement, variable: str) -> list[str]:
    placement = element.placement
    assert placement is not None  # nosec B101 - internal invariant
    path = element.resource.cli_path
    lines = ["{"]
    if placement.before_comment is not None:
        lines.append(
            f"  :local {variable} [{path} find where chain={placement.chain} "
            f"comment={_cli_quote(placement.before_comment)}]"
        )
        lines.append(
            f"  :if ([:len ${variable}] = 0) do={{ :set {variable} "
            f"[{path} find where chain={placement.chain}] }}"
        )
    else:
        lines.append(f"  :local {variable} [{path} find where chain={placement.chain}]")
    lines.append(
        f"  :if ([:len ${variable}] > 0) do={{ "
        f"{element.cli_add(extra=f'place-before=[:pick ${variable} 0]')} "
        f"}} else={{ {element.cli_add()} }}"
    )
    lines.append("}")
    return lines


def _render_script(module: WalledGardenRouterModule) -> str:
    owned = f'[find where comment~"^{TAG_PREFIX}"]'
    lines = [
        f"# {MODULE_NAME} walled-garden router module {module.version}",
        "# Rendered by dotmac_sub from settings. Review before applying.",
        f"# Removes and re-adds ONLY rows tagged {TAG_PREFIX}*; legacy",
        "# quarantine rules and static address-list entries are not touched.",
        f"/ip firewall address-list remove {owned}",
        f"/ip firewall filter remove {owned}",
        f"/ip firewall nat remove {owned}",
    ]
    for element in module.elements:
        if element.placement is None:
            lines.append(element.cli_add())
        elif element.resource is WalledGardenRouterResource.nat:
            lines.extend(_placed_add(element, "wgNatAnchor"))
        else:
            lines.extend(_placed_add(element, "wgFwdAnchor"))
    return "\n".join(lines) + "\n"


_SCRIPT_FORBIDDEN = (
    "/system",
    "/user",
    "/ip service",
    "/file",
    "/interface",
    "/routing",
    "/ip route",
    "reset",
    "/import",
    "/tool",
)
_SCRIPT_ALLOWED_PATHS = tuple(_CLI_PATHS.values())


def script_safety_errors(script: str) -> tuple[str, ...]:
    """Return every reason the operator script leaves the module's surface."""

    errors: list[str] = []
    lowered = script.lower()
    for word in _SCRIPT_FORBIDDEN:
        if word in lowered:
            errors.append(f"forbidden token {word!r}")
    for line in script.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text in {"{", "}"}:
            continue
        if " remove " in f" {text} ":
            if not text.endswith(f'remove [find where comment~"^{TAG_PREFIX}"]'):
                errors.append(f"untagged remove: {text}")
            continue
        paths = re.findall(r"/ip firewall [a-z-]+", text)
        if not paths or any(path not in _SCRIPT_ALLOWED_PATHS for path in paths):
            errors.append(f"line outside module surface: {text}")
        if " add " in text and f'comment="{TAG_PREFIX}' not in text:
            errors.append(f"untagged add: {text}")
    return tuple(errors)


def rest_safety_errors(commands: Sequence[str]) -> tuple[str, ...]:
    """Validate the REST form against the dangerous list and plan parser."""

    errors: list[str] = []
    try:
        check_dangerous_commands(list(commands))
    except ValueError as exc:
        errors.append(str(exc))
    try:
        plans = parse_routeros_rest_commands(list(commands))
    except RouterWriteAdapterError as exc:
        return (*errors, str(exc))
    allowed_paths = set(_REST_PATHS.values())
    for plan in plans:
        if plan.action != "add":
            errors.append(f"non-add action {plan.action!r}")
        if plan.resource_path not in allowed_paths:
            errors.append(f"resource outside module surface {plan.resource_path!r}")
        comment = plan.payload.get("comment")
        if not isinstance(comment, str) or not comment.startswith(TAG_PREFIX):
            errors.append(f"untagged REST command {plan.path}")
    return tuple(errors)


def render_walled_garden_module(
    config: WalledGardenModuleConfig,
) -> WalledGardenRouterModule:
    """Render the deterministic v1 module and refuse anything unsafe."""

    entries = _entries(config)
    elements = _elements(config, entries)
    module = WalledGardenRouterModule(
        version=MODULE_VERSION,
        config=config,
        entries=entries,
        elements=elements,
    )
    tags = module.tags
    errors: list[str] = []
    if len(set(tags)) != len(tags):
        errors.append("duplicate element tags")
    errors.extend(rest_safety_errors(module.rest_commands()))
    errors.extend(script_safety_errors(module.routeros_script()))
    if errors:
        raise WalledGardenModuleError(
            WalledGardenModuleErrorCode.UNSAFE_RENDER, "; ".join(errors)
        )
    return module


def render_walled_garden_module_from_settings(db: Session) -> WalledGardenRouterModule:
    """Render the module from the current canonical settings."""

    module = render_walled_garden_module(load_module_config(db))
    logger.info(
        "walled_garden_module_rendered",
        extra={
            "event": "walled_garden_module_rendered",
            "module_version": module.version,
            "element_count": len(module.elements),
            "enabled_entry_count": sum(1 for item in module.entries if item.enabled),
        },
    )
    return module


__all__ = [
    "ALLOW_LIST",
    "LEGACY_QUARANTINE_COMMENTS",
    "LEGACY_QUARANTINE_ELEMENTS",
    "LEGACY_QUARANTINE_JUMP_COMMENT",
    "MODULE_CHAIN",
    "MODULE_VERSION",
    "TAG_PREFIX",
    "TAG_ROOT",
    "LegacyQuarantineElement",
    "LegacyQuarantineRole",
    "WalledGardenAllowEntry",
    "WalledGardenElement",
    "WalledGardenModuleConfig",
    "WalledGardenModuleError",
    "WalledGardenModuleErrorCode",
    "WalledGardenPlacement",
    "WalledGardenRouterModule",
    "WalledGardenRouterResource",
    "build_module_config",
    "load_module_config",
    "render_walled_garden_module",
    "render_walled_garden_module_from_settings",
    "rest_safety_errors",
    "script_safety_errors",
]
