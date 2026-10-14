"""ntp@1: /system ntp client (+ optional server enablement). Read-only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

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
    FieldSpec,
    Kind,
    Strategy,
    canon_host,
)
from app.services.composition.plugin import Invalid, ModuleBase, SurfaceClaim

from ._support import ExportDocument, is_true, set_props


class NtpParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_enabled: bool
    servers: list[str]
    server_enabled: bool | None = None


FIELD_SPECS: dict[str, FieldSpec] = {
    "/client_enabled": FieldSpec(Kind.scalar, default=REQUIRED),
    "/servers": FieldSpec(
        Kind.list, Strategy.replace, canon="host", discoverable=True, default=REQUIRED
    ),
    "/server_enabled": FieldSpec(Kind.scalar, default=OPTIONAL_UNMANAGED),
}


def _canon_host(value: str) -> str:
    try:
        return canon_host(value)
    except ValueError:
        return value.strip().lower()


@dataclass(frozen=True)
class NtpObserved:
    client_enabled: bool
    servers: tuple[str, ...]
    server_enabled: bool


class NtpModule(ModuleBase):
    key = "ntp"
    version = 1
    params_schema = NtpParams
    field_specs = FIELD_SPECS
    surfaces = (
        SurfaceClaim("/system ntp client"),
        SurfaceClaim("/system ntp client servers"),
        SurfaceClaim("/system ntp server"),
    )
    health_checks = ("ntp-client-off", "ntp-unsynced", "clock-skew")

    def invariants(
        self, params: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> tuple[Invalid, ...]:
        """The router must never be its own time source (T20)."""
        own = {_canon_host(str(item)) for item in facts.get("loopbacks", ())}
        return tuple(
            Invalid(
                "self_reference",
                "/servers",
                "the server list contains this router's own loopback",
            )
            for server in params.get("servers", [])
            if server in own
        )[:1]

    def extract_observed(self, observation: Any) -> NtpObserved | Unknown:
        if not isinstance(observation, ExportDocument):
            return Unknown(NotEvaluated.unparseable, "no parsed export")
        client = set_props(observation, "/system ntp client")
        servers: list[str] = []
        for host in (client.get("servers") or "").split(","):
            if host.strip():
                servers.append(_canon_host(host))
        for item in observation.section("/system ntp client servers"):
            address = item.get("address")
            if item.verb == "add" and address:
                servers.append(_canon_host(address))
        deduped = tuple(dict.fromkeys(servers))
        server = set_props(observation, "/system ntp server")
        return NtpObserved(
            client_enabled=is_true(client.get("enabled")),
            servers=deduped,
            server_enabled=is_true(server.get("enabled")),
        )

    def compare(
        self, desired: NtpParams, observed: NtpObserved, facts: Mapping[str, Any]
    ) -> tuple[DriftItem, ...]:
        key = self.key
        items: list[DriftItem] = []
        if desired.client_enabled == observed.client_enabled:
            items.append(
                DriftItem(
                    key,
                    "/client_enabled",
                    DriftStatus.ok,
                    desired.client_enabled,
                    observed.client_enabled,
                )
            )
        else:
            items.append(
                DriftItem(
                    key,
                    "/client_enabled",
                    DriftStatus.disabled
                    if desired.client_enabled
                    else DriftStatus.drifted,
                    desired.client_enabled,
                    observed.client_enabled,
                    severity="medium",
                )
            )
        wanted = list(desired.servers)
        absent = [s for s in wanted if s not in observed.servers]
        extra = [s for s in observed.servers if s not in wanted]
        for server in absent:
            items.append(
                DriftItem(
                    key,
                    "/servers",
                    DriftStatus.missing,
                    server,
                    None,
                    element=server,
                    severity="medium",
                )
            )
        for server in extra:
            items.append(
                DriftItem(
                    key,
                    "/servers",
                    DriftStatus.unexpected,
                    None,
                    server,
                    element=server,
                    severity="low",
                )
            )
        if not absent and not extra:
            items.append(
                DriftItem(
                    key, "/servers", DriftStatus.ok, wanted, list(observed.servers)
                )
            )
        if (
            desired.server_enabled is not None
            and desired.server_enabled != observed.server_enabled
        ):
            items.append(
                DriftItem(
                    key,
                    "/server_enabled",
                    DriftStatus.drifted,
                    desired.server_enabled,
                    observed.server_enabled,
                )
            )
        return tuple(items)

    def propose(
        self, desired: NtpParams, observed: NtpObserved
    ) -> tuple[Proposal, ...]:
        """Observed extra servers. NTP sources are neutral, so never a widening."""
        return tuple(
            Proposal(
                self.key,
                "/servers",
                "add",
                server,
                False,
                {"source": "ntp client servers"},
            )
            for server in observed.servers
            if server not in desired.servers
        )


MODULE = NtpModule()
