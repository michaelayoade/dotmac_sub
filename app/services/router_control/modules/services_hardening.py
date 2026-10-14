"""services-hardening@1: unused services and exposed helpers. Read-only.

Absent keys in a compact RouterOS 7 export mean the RouterOS default; the
defaults below are [I] (inferred) until a real export per version proves them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.services.composition.drift import (
    DriftItem,
    DriftStatus,
    NotEvaluated,
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

from ._support import ExportDocument, is_true, parse_version, set_props

# Services the module may require disabled. Others (ssh, winbox, api, www-ssl)
# belong to the mgmt-access module (later PR).
KNOWN_SERVICES = ("telnet", "ftp", "www", "api-ssl", "reverse-proxy")
# [I] services that RouterOS ships disabled; an absent `set` line keeps that.
DEFAULT_DISABLED = frozenset({"www-ssl"})
# `reverse-proxy` exists from this RouterOS version (matches the posture check).
SERVICE_MIN_VERSION: Mapping[str, str] = {"reverse-proxy": "7.22"}


class BtestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    authenticate: bool


class HardeningParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled_services: list[str]
    dns_allow_remote: bool = False
    proxy: bool = False
    socks: bool = False
    upnp: bool = False
    btest: BtestParams | None = None
    romon: bool | None = None
    mac_winbox: bool | None = None


def _secure_false(**extra: Any) -> FieldSpec:
    return FieldSpec(Kind.scalar, access=AccessKind.secure_bool, secure=False, **extra)


FIELD_SPECS: dict[str, FieldSpec] = {
    "/disabled_services": FieldSpec(
        Kind.list,
        Strategy.union,
        access=AccessKind.deny_list,
        tie=Tie.restrictive,
        canon="lower",
        default=REQUIRED,
    ),
    "/dns_allow_remote": _secure_false(default=False),
    "/proxy": _secure_false(default=False),
    "/socks": _secure_false(default=False),
    "/upnp": _secure_false(default=False),
    "/btest": FieldSpec(Kind.object, default=OPTIONAL_UNMANAGED),
    "/btest/enabled": _secure_false(default=REQUIRED),
    "/btest/authenticate": FieldSpec(
        Kind.scalar, access=AccessKind.secure_bool, secure=True, default=REQUIRED
    ),
    "/romon": _secure_false(default=OPTIONAL_UNMANAGED),
    "/mac_winbox": _secure_false(default=OPTIONAL_UNMANAGED),
}


@dataclass(frozen=True)
class HardeningObserved:
    service_disabled: Mapping[str, bool]
    dns_allow_remote: bool
    proxy: bool
    socks: bool
    upnp: bool
    btest_enabled: bool
    btest_authenticate: bool
    romon: bool
    mac_winbox: bool
    version: tuple[int, ...] | None
    reverse_proxy_disabled_by_section: bool


class ServicesHardeningModule(ModuleBase):
    key = "services-hardening"
    version = 1
    params_schema = HardeningParams
    field_specs = FIELD_SPECS
    surfaces = (
        SurfaceClaim("/ip service", "telnet|ftp|www|api-ssl|reverse-proxy"),
        SurfaceClaim("/ip dns"),
        SurfaceClaim("/ip proxy"),
        SurfaceClaim("/ip socks"),
        SurfaceClaim("/ip upnp"),
        SurfaceClaim("/tool bandwidth-server"),
        SurfaceClaim("/tool romon"),
        SurfaceClaim("/tool mac-server mac-winbox"),
    )
    health_checks = ("reverse-proxy-enabled",)

    def extract_observed(self, observation: Any) -> HardeningObserved | Unknown:
        if not isinstance(observation, ExportDocument):
            return Unknown(NotEvaluated.unparseable, "no parsed export")
        doc = observation
        disabled: dict[str, bool] = {}
        for item in doc.section("/ip service"):
            if item.verb != "set":
                continue
            names = set(item.positional) | (
                {item.selector["name"]} if "name" in item.selector else set()
            )
            for name in names:
                disabled[name] = is_true(item.get("disabled"))
        for name in DEFAULT_DISABLED:
            disabled.setdefault(name, True)
        reverse_section = any(
            item.verb == "set" and item.get("enabled") in {"no", "false"}
            for item in doc.section("/ip reverse-proxy")
        )
        bandwidth = set_props(doc, "/tool bandwidth-server")
        mac = set_props(doc, "/tool mac-server mac-winbox")
        return HardeningObserved(
            service_disabled=disabled,
            dns_allow_remote=is_true(
                set_props(doc, "/ip dns").get("allow-remote-requests")
            ),
            proxy=is_true(set_props(doc, "/ip proxy").get("enabled")),
            socks=is_true(set_props(doc, "/ip socks").get("enabled")),
            upnp=is_true(set_props(doc, "/ip upnp").get("enabled")),
            # [I] RouterOS ships the bandwidth server enabled with authentication.
            btest_enabled=bandwidth.get("enabled", "yes") in {"yes", "true"},
            btest_authenticate=bandwidth.get("authenticate", "yes") in {"yes", "true"},
            romon=is_true(set_props(doc, "/tool romon").get("enabled")),
            # [I] allowed-interface-list defaults to "all"; "none" turns MAC-Winbox off.
            mac_winbox=mac.get("allowed-interface-list", "all") != "none",
            version=doc.version_tuple,
            reverse_proxy_disabled_by_section=reverse_section,
        )

    def compare(
        self,
        desired: HardeningParams,
        observed: HardeningObserved,
        facts: Mapping[str, Any],
    ) -> tuple[DriftItem, ...]:
        key = self.key
        items: list[DriftItem] = []
        for name in desired.disabled_services:
            minimum = parse_version(SERVICE_MIN_VERSION.get(name))
            if minimum is not None and (
                observed.version is None or observed.version < minimum
            ):
                items.append(
                    DriftItem(
                        key,
                        "/disabled_services",
                        DriftStatus.not_applicable,
                        name,
                        None,
                        element=name,
                        note=f"{name} needs RouterOS {SERVICE_MIN_VERSION[name]} or newer",
                    )
                )
                continue
            is_disabled = observed.service_disabled.get(name, False) or (
                name == "reverse-proxy" and observed.reverse_proxy_disabled_by_section
            )
            items.append(
                DriftItem(
                    key,
                    "/disabled_services",
                    DriftStatus.ok if is_disabled else DriftStatus.drifted,
                    name,
                    "disabled" if is_disabled else "enabled",
                    element=name,
                    severity="medium",
                )
            )
        scalars: list[tuple[str, bool | None, bool]] = [
            ("/dns_allow_remote", desired.dns_allow_remote, observed.dns_allow_remote),
            ("/proxy", desired.proxy, observed.proxy),
            ("/socks", desired.socks, observed.socks),
            ("/upnp", desired.upnp, observed.upnp),
            ("/romon", desired.romon, observed.romon),
            ("/mac_winbox", desired.mac_winbox, observed.mac_winbox),
        ]
        if desired.btest is not None:
            scalars.append(
                ("/btest/enabled", desired.btest.enabled, observed.btest_enabled)
            )
            scalars.append(
                (
                    "/btest/authenticate",
                    desired.btest.authenticate,
                    observed.btest_authenticate,
                )
            )
        for path, want, have in scalars:
            if want is None:
                continue
            items.append(
                DriftItem(
                    key,
                    path,
                    DriftStatus.ok if want == have else DriftStatus.drifted,
                    want,
                    have,
                    severity="medium",
                )
            )
        return tuple(items)


MODULE = ServicesHardeningModule()
