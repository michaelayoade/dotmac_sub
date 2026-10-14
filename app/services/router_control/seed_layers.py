"""PROPOSED seed layers for the PR1 report. Synthetic and not authoritative.

These use documentation address ranges and invented site names so the public
repository carries no estate values. Real values come from a private seed
passed with ``--layers``. Nothing here is stored or applied.
"""

from __future__ import annotations

from typing import Any

from app.services.composition.layers import Layer, LayerRevision

SEED_STATUS = "PROPOSED (synthetic, not authoritative)"

POLLERS = ("192.0.2.1/32", "192.0.2.2/32", "192.0.2.3/32", "192.0.2.4/32")


def _block(
    version: int = 1, mode: str | None = "enforce", **params: Any
) -> dict[str, Any]:
    block: dict[str, Any] = {"module_version": version}
    if mode is not None:
        block["mode"] = mode
    if params:
        block["params"] = params
    return block


def _doc(**modules: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "modules": {key.replace("_", "-"): v for key, v in modules.items()},
    }


def seed_layer_revisions() -> list[LayerRevision]:
    baseline = _doc(
        snmp=_block(
            enabled=True,
            trap_target="192.0.2.1",
            allowed_sources=list(POLLERS),
            write_access=False,
        ),
        ntp=_block(client_enabled=True),
        services_hardening=_block(
            disabled_services=["telnet", "ftp", "www", "api-ssl", "reverse-proxy"],
            dns_allow_remote=False,
            proxy=False,
            socks=False,
            upnp=False,
            btest={"enabled": False, "authenticate": True},
        ),
    )
    layers = [
        (Layer("global:fleet-baseline", "global", "fleet-baseline", 0), baseline),
        (
            Layer("site:site-a", "site", "site-a", 0, {"sites": ("site-a",)}),
            _doc(ntp=_block(mode=None, servers=["192.0.2.92"])),
        ),
        (
            Layer("site:site-b", "site", "site-b", 0, {"sites": ("site-b",)}),
            _doc(ntp=_block(mode=None, servers=["198.51.100.254"])),
        ),
        (
            Layer(
                "profile:access",
                "profile",
                "access",
                100,
                {"profiles": ("access",)},
                True,
                "role",
            ),
            _doc(),
        ),
        (
            Layer(
                "profile:aggregation",
                "profile",
                "aggregation",
                200,
                {"profiles": ("aggregation",)},
                True,
                "role",
            ),
            _doc(),
        ),
        (
            Layer(
                "profile:core",
                "profile",
                "core",
                300,
                {"profiles": ("core",)},
                True,
                "role",
            ),
            _doc(
                ntp=_block(mode=None, servers=["time.example.net"], server_enabled=True)
            ),
        ),
        (
            Layer(
                "profile:border",
                "profile",
                "border",
                400,
                {"profiles": ("border",)},
                True,
                "role",
            ),
            _doc(
                ntp=_block(mode=None, servers=["time.example.net"], server_enabled=True)
            ),
        ),
    ]
    return [LayerRevision(layer, 1, document) for layer, document in layers]
