"""snmp@1: /snmp and /snmp community (read-only; community names are never read)."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.services.composition.derive import NOT_DERIVED, DerivedRule, Ref
from app.services.composition.drift import (
    DriftItem,
    DriftStatus,
    NotEvaluated,
    Proposal,
    Unknown,
)
from app.services.composition.fields import (
    OPTIONAL_UNMANAGED,
    REQUIRED,
    AccessKind,
    FieldSpec,
    Kind,
    Strategy,
    Tie,
)
from app.services.composition.plugin import ModuleBase, SurfaceClaim
from app.services.composition.widening import widening

from ._support import ExportDocument, covered_by, is_true, set_props

# [I] RouterOS default for a key the compact /export omits.
DEFAULT_SRC_ADDRESS = "0.0.0.0"  # noqa: S104  # nosec B104 - a RouterOS default value, not a bind


class SnmpParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    trap_target: str | None = None
    allowed_sources: list[str]
    write_access: bool = False
    src_address: str | None = None

    @field_validator("allowed_sources")
    @classmethod
    def _cidrs(cls, value: list[str]) -> list[str]:
        for item in value:
            if ipaddress.ip_network(item, strict=False).prefixlen == 0:
                raise ValueError("a catch-all source is not allowed")
        return value


FIELD_SPECS: dict[str, FieldSpec] = {
    "/enabled": FieldSpec(Kind.scalar, default=REQUIRED),
    "/trap_target": FieldSpec(
        Kind.scalar, nullable=True, canon="host", default=OPTIONAL_UNMANAGED
    ),
    "/allowed_sources": FieldSpec(
        Kind.list,
        Strategy.union,
        access=AccessKind.allow_list,
        tie=Tie.restrictive,
        canon="cidr",
        discoverable=True,
        default=REQUIRED,
    ),
    "/write_access": FieldSpec(
        Kind.scalar, access=AccessKind.secure_bool, secure=False, default=False
    ),
    "/src_address": FieldSpec(
        Kind.scalar, nullable=True, canon="ip", derived=True, default=OPTIONAL_UNMANAGED
    ),
}


def _derive_src(reads: Mapping[str, Any]) -> Any:
    loopback = reads.get("fact:loopback_v4")
    return loopback if loopback else NOT_DERIVED


@dataclass(frozen=True)
class SnmpObserved:
    enabled: bool
    trap_target: str | None
    src_address: str
    addresses: tuple[str, ...]  # every address of every enabled community
    writable: bool
    communities: int  # a count only; names are never read


class SnmpModule(ModuleBase):
    key = "snmp"
    version = 1
    params_schema = SnmpParams
    field_specs = FIELD_SPECS
    surfaces = (SurfaceClaim("/snmp"), SurfaceClaim("/snmp community"))
    health_checks = ("snmp-exposed",)
    derives = (
        DerivedRule(
            "snmp-src-from-loopback",
            "snmp",
            "/src_address",
            (Ref(None, "loopback_v4"),),
            _derive_src,
        ),
    )

    def extract_observed(self, observation: Any) -> SnmpObserved | Unknown:
        if not isinstance(observation, ExportDocument):
            return Unknown(NotEvaluated.unparseable, "no parsed export")
        main = set_props(observation, "/snmp")
        addresses: list[str] = []
        writable = False
        count = 0
        default_modified = False
        entries = []
        # Mirrors router_health.posture_checks._check_snmp so the two agree.
        for entry in observation.section("/snmp community"):
            if entry.verb == "set" and entry.selector.get("default") == "yes":
                default_modified = True
                entries.append(entry)
            elif entry.verb == "add":
                entries.append(entry)
        for entry in entries:
            if is_true(entry.get("disabled")):
                continue
            count += 1
            listed = list(entry.value_list("addresses")) or ["::/0"]
            addresses.extend(item.strip() for item in listed)
            if is_true(entry.get("write-access")):
                writable = True
        if not default_modified:
            count += 1  # the implicit default community answers ::/0
            addresses.append("::/0")
        return SnmpObserved(
            enabled=is_true(main.get("enabled")),
            trap_target=main.get("trap-target") or None,
            src_address=main.get("src-address", DEFAULT_SRC_ADDRESS),
            addresses=tuple(addresses),
            writable=writable,
            communities=count,
        )

    def compare(
        self, desired: SnmpParams, observed: SnmpObserved, facts: Mapping[str, Any]
    ) -> tuple[DriftItem, ...]:
        key = self.key
        items: list[DriftItem] = []
        if desired.enabled == observed.enabled:
            items.append(
                DriftItem(
                    key, "/enabled", DriftStatus.ok, desired.enabled, observed.enabled
                )
            )
        else:
            items.append(
                DriftItem(
                    key,
                    "/enabled",
                    DriftStatus.disabled if desired.enabled else DriftStatus.drifted,
                    desired.enabled,
                    observed.enabled,
                    severity="medium",
                )
            )
        if not observed.enabled:
            return tuple(items)
        allowed = [
            ipaddress.ip_network(item, strict=False) for item in desired.allowed_sources
        ]
        exposure = sorted({a for a in observed.addresses if not covered_by(a, allowed)})
        for address in exposure:
            items.append(
                DriftItem(
                    key,
                    "/allowed_sources",
                    DriftStatus.unexpected,
                    None,
                    address,
                    element=address,
                    severity="high",
                    note="SNMP answerable from outside the allowed sources",
                )
            )
        seen: set[str] = set()
        for address in observed.addresses:
            try:
                seen.add(str(ipaddress.ip_network(address, strict=False)))
            except ValueError:
                continue
        missing = [source for source in desired.allowed_sources if source not in seen]
        for source in missing:
            items.append(
                DriftItem(
                    key,
                    "/allowed_sources",
                    DriftStatus.missing,
                    source,
                    None,
                    element=source,
                    severity="medium",
                    note="allowed source is on no community",
                )
            )
        if not exposure and not missing:
            items.append(
                DriftItem(
                    key,
                    "/allowed_sources",
                    DriftStatus.ok,
                    desired.allowed_sources,
                    None,
                )
            )
        if desired.write_access != observed.writable:
            items.append(
                DriftItem(
                    key,
                    "/write_access",
                    DriftStatus.drifted,
                    desired.write_access,
                    observed.writable,
                    severity="critical" if observed.writable else "low",
                )
            )
        else:
            items.append(
                DriftItem(
                    key,
                    "/write_access",
                    DriftStatus.ok,
                    desired.write_access,
                    observed.writable,
                )
            )
        if (
            desired.trap_target is not None
            and desired.trap_target != observed.trap_target
        ):
            items.append(
                DriftItem(
                    key,
                    "/trap_target",
                    DriftStatus.drifted,
                    desired.trap_target,
                    observed.trap_target,
                )
            )
        if (
            desired.src_address is not None
            and desired.src_address != observed.src_address
        ):
            items.append(
                DriftItem(
                    key,
                    "/src_address",
                    DriftStatus.drifted,
                    desired.src_address,
                    observed.src_address,
                )
            )
        return tuple(items)

    def propose(
        self, desired: SnmpParams, observed: SnmpObserved
    ) -> tuple[Proposal, ...]:
        """Observed sources the desired list lacks; catch-all ranges are never proposed."""
        if not observed.enabled:
            return ()
        spec = FIELD_SPECS["/allowed_sources"]
        allowed = [
            ipaddress.ip_network(item, strict=False) for item in desired.allowed_sources
        ]
        out: list[Proposal] = []
        for address in sorted(set(observed.addresses)):
            try:
                network = ipaddress.ip_network(address, strict=False)
            except ValueError:
                continue
            if network.prefixlen == 0 or covered_by(address, allowed):
                continue
            canonical = str(network)
            out.append(
                Proposal(
                    self.key,
                    "/allowed_sources",
                    "add",
                    canonical,
                    widening(
                        spec,
                        desired.allowed_sources,
                        [*desired.allowed_sources, canonical],
                    ),
                    {"source": "snmp community addresses"},
                )
            )
        return tuple(out)


MODULE = SnmpModule()
