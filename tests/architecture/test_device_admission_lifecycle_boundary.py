"""Device admission is a single owned transition, and it stays that way.

``network.monitoring_inventory`` owns the ``NetworkDevice`` admission
lifecycle. Every deactivation must go through ``set_network_device_active`` so
the derived reachability cache is decayed with it; a caller that writes the
flag directly reintroduces the frozen-``up`` defect (a device that leaves the
poll sweep keeps asserting reachability forever, which vetoes outage detection
for every customer behind it).

These are static checks: they fail when a new caller reaches around the owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

OWNER_MODULE = "app/services/network_monitoring.py"
TRANSITION = "set_network_device_active"


def _source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _mentions_network_device(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == "NetworkDevice"
        for child in ast.walk(node)
    )


def _network_device_bindings(tree: ast.AST) -> set[str]:
    """Names in this module that provably hold a ``NetworkDevice``.

    Three provable origins — assigned from an expression naming the model
    (``db.get(NetworkDevice, ...)``, ``select(NetworkDevice)``, ...), annotated
    as one, or a parameter annotated as one. Other models that happen to expose
    ``is_active`` never bind these names, so the scan below cannot confuse a
    ``NasDevice``/``OntUnit``/``GeoLocation`` write for a monitoring-device one.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _mentions_network_device(node.value):
            bound.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and _mentions_network_device(node.annotation)
            and isinstance(node.target, ast.Name)
        ):
            bound.add(node.target.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in [*node.args.args, *node.args.kwonlyargs]:
                if arg.annotation is not None and _mentions_network_device(
                    arg.annotation
                ):
                    bound.add(arg.arg)
    return bound


def test_only_the_owner_module_writes_network_device_is_active() -> None:
    """No service outside the owner reassigns a NetworkDevice's admission flag.

    Constructor keyword arguments are fine — creation is not a transition. It
    is reassigning an existing row that must route through the owner, because
    only the owner also decays the derived reachability cache.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if relative == OWNER_MODULE or "NetworkDevice" not in source:
            continue
        tree = ast.parse(source)
        bound = _network_device_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_active"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in bound
                ):
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], (
        "these callers write NetworkDevice.is_active directly instead of "
        f"requesting {TRANSITION}: {offenders}"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "app/services/router_management/inventory.py",
        "app/services/monitoring_metrics.py",
        "app/services/web_network_monitoring.py",
        "app/services/web_network_core_devices_forms.py",
        "app/services/network/olt_web_forms.py",
    ],
)
def test_known_admission_callers_request_the_owner_transition(relative: str) -> None:
    assert TRANSITION in _source(relative)


def test_router_field_sync_does_not_copy_admission_as_a_field() -> None:
    """The cross-domain write this slice removed.

    ``device.is_active = router.is_active`` sat in a field-copy helper: a
    router_management writer performing a monitoring-inventory lifecycle
    transition, skipping the decay. Router inventory remains an authoritative
    INPUT to that admission, but it requests the transition.
    """
    source = _source("app/services/monitoring_metrics.py")
    assert "device.is_active = router.is_active" not in source
    assert TRANSITION in source


def test_the_transition_decays_the_derived_reachability_cache() -> None:
    source = _source(OWNER_MODULE)
    assert "device.live_status = _INACTIVE_LIVE_STATUS" in source


def test_device_derivation_does_not_filter_out_inactive_devices() -> None:
    """Filtering here made the projection reconciler DELETE the device."""
    source = _source("app/services/web_network_core_devices_inventory.py")
    assert "for device in monitoring_devices if device.is_active" not in source
    assert "lifecycle_state" in source


def test_projection_carries_the_inactive_never_working_release_gate() -> None:
    model = _source("app/models/network_monitoring.py")
    assert "ck_device_projection_inactive_never_working" in model
    assert "lifecycle_state = 'active' OR operational_status = 'not_working'" in model

    reconciler = _source("app/services/device_projection_reconcile.py")
    assert "_gated_status" in reconciler


def test_customer_facing_readers_apply_the_freshness_gate() -> None:
    """A dead warmer must not be able to serve a confident stale verdict."""
    classifier = _source("app/services/topology/health_classifier.py")
    assert "trusted_live_status" in classifier
    # The raw column must not be read straight into the mgmt-plane decision.
    assert '_mgmt_state(getattr(node, "live_status", None))' not in classifier

    for relative in (
        "app/services/topology/connection_status.py",
        "app/services/topology/last_mile.py",
    ):
        assert "warm_stale" in _source(relative), relative


def test_retired_zabbix_provenance_module_is_gone() -> None:
    """The dead drift filter keyed on a source string from a deleted importer."""
    assert not (PROJECT_ROOT / "app/services/topology/sources.py").exists()
    gaps = _source("app/services/topology/gaps.py")
    assert "zabbix_reconcile" not in gaps
    assert "RECONCILED_SOURCE" not in gaps
    assert "NetworkDevice.source ==" not in gaps


def test_ownership_is_recorded_in_the_map_and_registry() -> None:
    registry = _source("app/services/sot_relationships.py")
    assert "monitoring device admission lifecycle transitions" in registry
    assert TRANSITION in registry

    relationship_map = _source("docs/SOT_RELATIONSHIP_MAP.md")
    assert TRANSITION in relationship_map
    assert "admission" in relationship_map
