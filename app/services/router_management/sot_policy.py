"""Typed, owned desired state for MikroTik RouterOS resources."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RouterSotPolicyError(ValueError):
    """The requested desired state is outside the managed RouterOS surface."""


class RouterManagedResource(StrEnum):
    firewall_address_list = "firewall_address_list"
    firewall_filter = "firewall_filter"
    firewall_nat = "firewall_nat"
    simple_queue = "simple_queue"
    ipv4_address = "ipv4_address"
    ipv6_address = "ipv6_address"
    ipv4_route = "ipv4_route"
    ipv6_route = "ipv6_route"
    bgp_connection = "bgp_connection"
    ospf_instance = "ospf_instance"
    ospf_area = "ospf_area"
    ospf_interface_template = "ospf_interface_template"
    routing_filter_rule = "routing_filter_rule"


class RouterDesiredState(StrEnum):
    present = "present"
    absent = "absent"


@dataclass(frozen=True)
class RouterResourcePolicy:
    path: str
    allowed_fields: frozenset[str]
    required_fields: frozenset[str]


@dataclass(frozen=True)
class RouterSotIntent:
    resource: RouterManagedResource
    key: str
    state: RouterDesiredState
    values: dict[str, str | int | bool]

    @property
    def ownership_marker(self) -> str:
        return f"dotmac-sot:{self.key}"

    @property
    def policy(self) -> RouterResourcePolicy:
        return ROUTER_RESOURCE_POLICIES[self.resource]

    def desired_payload(self) -> dict[str, str | int | bool]:
        return {**self.values, "comment": self.ownership_marker}

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.value,
            "key": self.key,
            "state": self.state.value,
            "values": dict(self.values),
        }

    def preview(self) -> dict[str, Any]:
        return {
            **self.to_dict(),
            "resource_path": self.policy.path,
            "ownership_marker": self.ownership_marker,
            "verifiable": True,
        }


def _fields(*names: str) -> frozenset[str]:
    return frozenset(names)


ROUTER_RESOURCE_POLICIES: dict[RouterManagedResource, RouterResourcePolicy] = {
    RouterManagedResource.firewall_address_list: RouterResourcePolicy(
        path="/ip/firewall/address-list",
        allowed_fields=_fields("list", "address", "timeout", "disabled"),
        required_fields=_fields("list", "address"),
    ),
    RouterManagedResource.firewall_filter: RouterResourcePolicy(
        path="/ip/firewall/filter",
        allowed_fields=_fields(
            "chain",
            "action",
            "src-address",
            "dst-address",
            "src-address-list",
            "dst-address-list",
            "protocol",
            "src-port",
            "dst-port",
            "in-interface",
            "out-interface",
            "in-interface-list",
            "out-interface-list",
            "connection-state",
            "connection-nat-state",
            "jump-target",
            "log",
            "log-prefix",
            "disabled",
        ),
        required_fields=_fields("chain", "action"),
    ),
    RouterManagedResource.firewall_nat: RouterResourcePolicy(
        path="/ip/firewall/nat",
        allowed_fields=_fields(
            "chain",
            "action",
            "src-address",
            "dst-address",
            "src-address-list",
            "dst-address-list",
            "protocol",
            "src-port",
            "dst-port",
            "in-interface",
            "out-interface",
            "in-interface-list",
            "out-interface-list",
            "to-addresses",
            "to-ports",
            "ipsec-policy",
            "disabled",
        ),
        required_fields=_fields("chain", "action"),
    ),
    RouterManagedResource.simple_queue: RouterResourcePolicy(
        path="/queue/simple",
        allowed_fields=_fields(
            "name",
            "target",
            "max-limit",
            "limit-at",
            "burst-limit",
            "burst-threshold",
            "burst-time",
            "priority",
            "queue",
            "parent",
            "disabled",
        ),
        required_fields=_fields("name", "target"),
    ),
    RouterManagedResource.ipv4_address: RouterResourcePolicy(
        path="/ip/address",
        allowed_fields=_fields("address", "interface", "network", "disabled"),
        required_fields=_fields("address", "interface"),
    ),
    RouterManagedResource.ipv6_address: RouterResourcePolicy(
        path="/ipv6/address",
        allowed_fields=_fields(
            "address", "interface", "advertise", "eui-64", "no-dad", "disabled"
        ),
        required_fields=_fields("address", "interface"),
    ),
    RouterManagedResource.ipv4_route: RouterResourcePolicy(
        path="/ip/route",
        allowed_fields=_fields(
            "dst-address",
            "gateway",
            "distance",
            "routing-table",
            "scope",
            "target-scope",
            "check-gateway",
            "blackhole",
            "unreachable",
            "prohibit",
            "disabled",
        ),
        required_fields=_fields("dst-address"),
    ),
    RouterManagedResource.ipv6_route: RouterResourcePolicy(
        path="/ipv6/route",
        allowed_fields=_fields(
            "dst-address",
            "gateway",
            "distance",
            "routing-table",
            "scope",
            "target-scope",
            "check-gateway",
            "blackhole",
            "unreachable",
            "prohibit",
            "disabled",
        ),
        required_fields=_fields("dst-address"),
    ),
    RouterManagedResource.bgp_connection: RouterResourcePolicy(
        path="/routing/bgp/connection",
        allowed_fields=_fields(
            "name",
            "remote.address",
            "remote.as",
            "remote.port",
            "local.address",
            "local.as",
            "local.role",
            "routing-table",
            "templates",
            "input.filter",
            "output.filter-chain",
            "disabled",
        ),
        required_fields=_fields("name", "remote.address", "remote.as", "local.role"),
    ),
    RouterManagedResource.ospf_instance: RouterResourcePolicy(
        path="/routing/ospf/instance",
        allowed_fields=_fields(
            "name", "version", "router-id", "routing-table", "vrf", "disabled"
        ),
        required_fields=_fields("name", "version", "router-id"),
    ),
    RouterManagedResource.ospf_area: RouterResourcePolicy(
        path="/routing/ospf/area",
        allowed_fields=_fields(
            "name", "instance", "area-id", "type", "default-cost", "disabled"
        ),
        required_fields=_fields("name", "instance", "area-id"),
    ),
    RouterManagedResource.ospf_interface_template: RouterResourcePolicy(
        path="/routing/ospf/interface-template",
        allowed_fields=_fields(
            "interfaces",
            "networks",
            "area",
            "type",
            "cost",
            "priority",
            "hello-interval",
            "dead-interval",
            "retransmit-interval",
            "transmit-delay",
            "passive",
            "disabled",
        ),
        required_fields=_fields("area"),
    ),
    RouterManagedResource.routing_filter_rule: RouterResourcePolicy(
        path="/routing/filter/rule",
        allowed_fields=_fields("chain", "rule", "disabled"),
        required_fields=_fields("chain", "rule"),
    ),
}


_SOT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")
_SECRET_MARKERS = ("password", "secret", "private-key", "token", "auth-key")


def _parse_scalar_values(value: object) -> dict[str, str | int | bool]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RouterSotPolicyError("RouterOS desired values must be an object")
    result: dict[str, str | int | bool] = {}
    for raw_key, item in value.items():
        key = str(raw_key).strip()
        if not key:
            raise RouterSotPolicyError("RouterOS desired field names cannot be blank")
        if isinstance(item, bool | int | str):
            result[key] = item
            continue
        raise RouterSotPolicyError(
            f"RouterOS desired field '{key}' must be a string, integer, or boolean"
        )
    return result


def parse_routeros_sot_intent(value: object) -> RouterSotIntent:
    if not isinstance(value, Mapping):
        raise RouterSotPolicyError("RouterOS desired state entries must be objects")
    try:
        resource = RouterManagedResource(str(value.get("resource") or ""))
    except ValueError as exc:
        supported = ", ".join(item.value for item in RouterManagedResource)
        raise RouterSotPolicyError(
            f"Unsupported managed RouterOS resource; supported resources: {supported}"
        ) from exc
    try:
        state = RouterDesiredState(str(value.get("state") or "present"))
    except ValueError as exc:
        raise RouterSotPolicyError(
            "RouterOS state must be 'present' or 'absent'"
        ) from exc

    key = str(value.get("key") or "").strip()
    if not _SOT_KEY_RE.fullmatch(key):
        raise RouterSotPolicyError(
            "RouterOS ownership key must be 1-127 letters, numbers, '.', '_', ':', or '-'"
        )

    values = _parse_scalar_values(value.get("values"))
    policy = ROUTER_RESOURCE_POLICIES[resource]
    secret_fields = sorted(
        field
        for field in values
        if any(marker in field.lower() for marker in _SECRET_MARKERS)
    )
    if secret_fields:
        raise RouterSotPolicyError(
            "Secret-bearing RouterOS fields are not accepted by configuration SOT: "
            + ", ".join(secret_fields)
        )
    unknown = sorted(set(values) - policy.allowed_fields)
    if unknown:
        raise RouterSotPolicyError(
            f"Fields are not managed for {resource.value}: {', '.join(unknown)}"
        )
    if state is RouterDesiredState.absent and values:
        raise RouterSotPolicyError(
            "Absent RouterOS state cannot include desired values"
        )
    if state is RouterDesiredState.present:
        missing = sorted(policy.required_fields - set(values))
        if missing:
            raise RouterSotPolicyError(
                f"Required fields for {resource.value}: {', '.join(missing)}"
            )

    return RouterSotIntent(resource=resource, key=key, state=state, values=values)


def parse_routeros_sot_intents(values: list[object]) -> list[RouterSotIntent]:
    if not values:
        raise RouterSotPolicyError("RouterOS desired state cannot be empty")
    intents = [parse_routeros_sot_intent(value) for value in values]
    identities = [(intent.resource, intent.key) for intent in intents]
    if len(identities) != len(set(identities)):
        raise RouterSotPolicyError(
            "RouterOS desired state contains duplicate resource ownership keys"
        )
    return intents


def managed_resource_options() -> list[dict[str, object]]:
    return [
        {
            "value": resource.value,
            "label": resource.value.replace("_", " ").title(),
            "required_fields": sorted(policy.required_fields),
            "allowed_fields": sorted(policy.allowed_fields),
        }
        for resource, policy in ROUTER_RESOURCE_POLICIES.items()
    ]
