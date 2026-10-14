"""A synthetic plug-in that exercises every engine feature (no router concepts)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.services.composition.compose import compose
from app.services.composition.derive import DerivedRule, Ref
from app.services.composition.drift import DriftItem, Proposal
from app.services.composition.exceptions import ExceptionKind, ExceptionSpec
from app.services.composition.fields import (
    OPTIONAL_UNMANAGED,
    REQUIRED,
    AccessKind,
    FieldSpec,
    Kind,
    Strategy,
    Tie,
)
from app.services.composition.layers import Layer, LayerRevision, Subject, TierSpec
from app.services.composition.plugin import (
    DomainPlugin,
    Invalid,
    ModuleBase,
    SurfaceClaim,
)

TODAY = date(2026, 10, 14)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Btest(_Strict):
    enabled: bool
    authenticate: bool | None = None


class Peer(_Strict):
    remote: str
    asn: int | None = None
    hold: int | None = None
    afi: str | None = None
    enabled: bool = True


class MParams(_Strict):
    x: int | None = None
    flag: bool | None = None
    note: str | None = None
    level: str | None = None
    servers: list[str] = []
    sources: list[str] = []
    denied: list[str] = []
    routes: list[str] = []
    peers: list[Peer] = []
    btest: Btest | None = None


M_SPECS: dict[str, FieldSpec] = {
    "/x": FieldSpec(Kind.scalar, default=OPTIONAL_UNMANAGED),
    "/flag": FieldSpec(
        Kind.scalar,
        access=AccessKind.secure_bool,
        secure=False,
        default=OPTIONAL_UNMANAGED,
    ),
    "/note": FieldSpec(Kind.scalar, nullable=True, default=OPTIONAL_UNMANAGED),
    "/level": FieldSpec(
        Kind.scalar,
        access=AccessKind.ordered,
        ranking=("high", "mid", "low"),
        tie=Tie.restrictive,
        default=OPTIONAL_UNMANAGED,
    ),
    "/servers": FieldSpec(
        Kind.list, Strategy.replace, canon="host", default=OPTIONAL_UNMANAGED
    ),
    "/sources": FieldSpec(
        Kind.list,
        Strategy.union,
        access=AccessKind.allow_list,
        tie=Tie.restrictive,
        canon="cidr",
        default=OPTIONAL_UNMANAGED,
    ),
    "/denied": FieldSpec(
        Kind.list,
        Strategy.union,
        access=AccessKind.deny_list,
        tie=Tie.restrictive,
        canon="lower",
        default=OPTIONAL_UNMANAGED,
    ),
    "/routes": FieldSpec(
        Kind.list, Strategy.union, canon="text", default=OPTIONAL_UNMANAGED
    ),
    "/peers": FieldSpec(
        Kind.list,
        Strategy.keyed,
        key="remote",
        order="remote",
        default=OPTIONAL_UNMANAGED,
    ),
    "/peers/*/remote": FieldSpec(Kind.scalar, canon="ip"),
    "/peers/*/asn": FieldSpec(Kind.scalar, nullable=True, default=OPTIONAL_UNMANAGED),
    "/peers/*/hold": FieldSpec(Kind.scalar, nullable=True, default=OPTIONAL_UNMANAGED),
    "/peers/*/afi": FieldSpec(Kind.scalar, nullable=True, default=OPTIONAL_UNMANAGED),
    "/peers/*/enabled": FieldSpec(Kind.scalar, default=OPTIONAL_UNMANAGED),
    "/btest": FieldSpec(Kind.object, default=OPTIONAL_UNMANAGED),
    "/btest/enabled": FieldSpec(
        Kind.scalar, access=AccessKind.secure_bool, secure=False, default=False
    ),
    "/btest/authenticate": FieldSpec(
        Kind.scalar,
        access=AccessKind.secure_bool,
        secure=True,
        default=OPTIONAL_UNMANAGED,
    ),
}


class DemoModule(ModuleBase):
    version = 1
    mode_ordered = False

    def __init__(
        self, key: str, schema: type[BaseModel], specs: Mapping[str, FieldSpec]
    ):
        self.key = key
        self.params_schema = schema
        self.field_specs = specs
        self.surfaces = (SurfaceClaim(f"/{key}"),)

    def extract_observed(self, observation: Any) -> Any:
        return observation

    def compare(
        self, desired: Any, observed: Any, facts: Mapping[str, Any]
    ) -> tuple[DriftItem, ...]:
        return ()

    def propose(self, desired: Any, observed: Any) -> tuple[Proposal, ...]:
        return ()


class ReqParams(_Strict):
    btest: Btest
    name: str


REQ_SPECS: dict[str, FieldSpec] = {
    "/btest": FieldSpec(Kind.object, default=OPTIONAL_UNMANAGED),
    "/btest/enabled": FieldSpec(Kind.scalar, default=False),
    "/btest/authenticate": FieldSpec(Kind.scalar, default=REQUIRED),
    "/name": FieldSpec(Kind.scalar, default=REQUIRED),
}


class NtpParams(_Strict):
    servers: list[str]


class NtpModule(DemoModule):
    def invariants(
        self, params: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> tuple[Invalid, ...]:
        own = set(facts.get("loopbacks", ()))
        bad = [s for s in params.get("servers", []) if s in own]
        return tuple(
            Invalid("self_reference", "/servers", "own loopback") for _ in bad[:1]
        )


NTP_SPECS = {
    "/servers": FieldSpec(Kind.list, Strategy.replace, canon="host", default=REQUIRED),
}


class BgpParams(_Strict):
    peers: list[Peer] = []


BGP_SPECS = {k: v for k, v in M_SPECS.items() if k.startswith("/peers")}


class PeerClaimModule(DemoModule):
    def claims(self, params: Mapping[str, Any]) -> frozenset[str]:
        return frozenset(f"bgp:{p['remote']}" for p in params.get("peers", []))


class FwParams(_Strict):
    ipp_bgp: list[str] = []


def _ipp_bgp(reads: Mapping[str, Any]) -> Any:
    peers = reads.get("bgp:/peers") or []
    return [p["remote"] for p in peers if p.get("enabled", True)]


FW_SPECS = {
    "/ipp_bgp": FieldSpec(
        Kind.list, Strategy.replace, derived=True, default=OPTIONAL_UNMANAGED
    ),
}


class FwModule(DemoModule):
    derives = (
        DerivedRule("ipp-bgp", "fw", "/ipp_bgp", (Ref("bgp", "/peers"),), _ipp_bgp),
    )


TIERS = (
    TierSpec("global", 0),
    TierSpec("site", 1, allowed_dims=("sites",), require_any=("sites",)),
    TierSpec(
        "profile",
        2,
        weight_band=(100, 900),
        allowed_dims=("profiles",),
        require_any=("profiles",),
        kind_bands={"role": (100, 400), "feature": (500, 900)},
    ),
    TierSpec(
        "group",
        3,
        allowed_dims=("tags", "profiles", "hardware_classes", "sites"),
        require_any=("tags", "profiles", "hardware_classes"),
    ),
    TierSpec(
        "router",
        4,
        weight_band=(0, 0),
        allowed_dims=("router_ids",),
        require_any=("router_ids",),
    ),
)


def make_plugin() -> DomainPlugin:
    return DomainPlugin(
        "demo",
        TIERS,
        {
            "m": DemoModule("m", MParams, M_SPECS),
            "req": DemoModule("req", ReqParams, REQ_SPECS),
            "ntp": NtpModule("ntp", NtpParams, NTP_SPECS),
            "bgp": PeerClaimModule("bgp", BgpParams, BGP_SPECS),
            "rr": PeerClaimModule("rr", BgpParams, BGP_SPECS),
            "fw": FwModule("fw", FwParams, FW_SPECS),
        },
    )


PLUGIN = make_plugin()


def subject(
    *,
    sites: tuple[str, ...] = ("s",),
    profiles: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    facts: Mapping[str, Any] | None = None,
) -> Subject:
    return Subject(
        "r1",
        {
            "sites": frozenset(sites),
            "profiles": frozenset(profiles),
            "tags": frozenset(tags),
            "router_ids": frozenset({"r1"}),
            "hardware_classes": frozenset(),
        },
        facts or {},
    )


def block(
    module: str, *, mode: str | None = "enforce", version: int = 1, **params: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"module_version": version}
    if mode is not None:
        body["mode"] = mode
    if params:
        body["params"] = params
    return {"schema": 1, "modules": {module: body}}


def doc(*blocks: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    for item in blocks:
        modules.update(item["modules"])
    return {"schema": 1, "modules": modules}


def rev(
    tier: str,
    name: str,
    document: dict[str, Any],
    *,
    weight: int = 0,
    criteria: dict[str, tuple[str, ...]] | None = None,
    kind: str | None = None,
    revision: int = 1,
    layer_id: str | None = None,
) -> LayerRevision:
    default_criteria: dict[str, tuple[str, ...]] = {
        "global": {},
        "site": {"sites": ("s",)},
        "profile": {"profiles": (name,)},
        "group": {"tags": ("canary",)},
        "router": {"router_ids": ("r1",)},
    }[tier]  # type: ignore[assignment]
    if tier == "profile" and kind is None:
        kind = "role" if weight < 500 else "feature"
    return LayerRevision(
        Layer(
            layer_id or f"{tier}:{name}",
            tier,
            name,
            weight,
            criteria if criteria is not None else default_criteria,
            True,
            kind,
        ),
        revision,
        document,
    )


def run(
    revisions: list[LayerRevision],
    *,
    subj: Subject | None = None,
    exceptions: list[ExceptionSpec] | None = None,
    today: date = TODAY,
):
    return compose(
        PLUGIN,
        subj or subject(profiles=("access", "aggregation"), tags=("canary",)),
        revisions,
        exceptions or [],
        today,
    )


def override(
    exc_id: str,
    module: str,
    path: str,
    patch: dict[str, Any],
    *,
    until: date,
    created: date = date(2026, 10, 14),
    approved: bool = False,
    reason: str = "field engineers need this for testing",
) -> ExceptionSpec:
    return ExceptionSpec(
        id=exc_id,
        module_key=module,
        kind=ExceptionKind.override,
        reason=reason,
        created_on=created,
        until=until,
        subject_key="r1",
        path=path,
        patch=patch,
        approved=approved,
    )


def make_ordered_mode_plugin() -> DomainPlugin:
    """A plug-in whose module `m` has an ordered mode (enforce > observe > off)."""
    module = DemoModule("m", MParams, M_SPECS)
    module.mode_ordered = True
    return DomainPlugin("demo-ordered", TIERS, {"m": module})
