"""§1.12 worked examples restricted to the three PR1 modules (documentation ranges)."""

from __future__ import annotations

from datetime import timedelta

from app.services.composition.drift import DriftStatus, ModuleStatus
from app.services.composition.exceptions import ExceptionKind, ExceptionSpec
from app.services.composition.widening import mode_widening, params_widening
from app.services.router_control.plugin import ROUTER_PLUGIN
from app.services.router_control.seed_layers import seed_layer_revisions
from tests.services.router_control.support import (
    LOOPBACK,
    TODAY,
    binding,
    compose_for,
    doc_of,
    export,
    extra_layer,
    fixture_text,
    report_for,
)


def replace_layer(name, new):
    return [r for r in seed_layer_revisions() if r.layer.name != name] + [new]


def row(rep, module, path):
    return next(r for r in rep.effective[module]["values"] if r["path"] == path)


def test_access_eoip_router_matches_the_design():
    rep = report_for(
        binding("access-eoip", profiles=("access", "eoip")), fixture_text("Gudu")
    )
    assert [(l["tier"], l["name"]) for l in rep.layers] == [
        ("global", "fleet-baseline"),
        ("site", "site-a"),
        ("profile", "access"),
    ]  # `eoip` has no layer in the PR1 seed
    assert rep.evaluations["ntp"].status is ModuleStatus.conform
    assert rep.evaluations["snmp"].status is ModuleStatus.conform
    hardening = rep.evaluations["services-hardening"]
    by_element = {(i.path, i.element): i.status for i in hardening.items}
    assert (
        by_element[("/disabled_services", "reverse-proxy")]
        is DriftStatus.not_applicable
    )
    assert by_element[("/btest/enabled", None)] is DriftStatus.drifted
    assert row(rep, "ntp", "/servers")["origin"]["layer"] == "site-a"
    assert row(rep, "snmp", "/src_address")["origin"]["op"] == "derived"
    assert (
        row(rep, "snmp", "/allowed_sources/192.0.2.1%2F32")["origin"]["layer"]
        == "fleet-baseline"
    )


def test_btest_exception_x1_resolves_the_drift_with_provenance():
    exc = ExceptionSpec(
        "X-1",
        "services-hardening",
        ExceptionKind.override,
        "field engineers run bandwidth tests to this router",
        TODAY,
        TODAY + timedelta(days=30),
        subject_key="id-access-eoip",
        path="/btest",
        patch={"enabled": True, "authenticate": True},
        approved=True,
    )
    rep = report_for(
        binding("access-eoip", router_id="id-access-eoip"),
        fixture_text("Gudu"),
        exceptions=[exc],
    )
    assert rep.evaluations["services-hardening"].status is ModuleStatus.conform
    prov = row(rep, "services-hardening", "/btest/enabled")["origin"]
    assert (prov["tier"], prov["detail"]["until"]) == (
        "exception",
        (TODAY + timedelta(days=30)).isoformat(),
    )
    assert prov["shadowed"][0]["value"] is False


def test_border_profile_outranks_the_site_ntp_ladder_and_avoids_self_reference():
    b = binding(
        "border-router",
        site="site-b",
        profiles=("border",),
        loopback_v4="198.51.100.254",
    )
    layers = seed_layer_revisions()
    eff = compose_for(b, layers)
    assert eff.modules["ntp"].ok
    prov = eff.modules["ntp"].provenance["/servers"]
    assert prov.origin.layer_name == "border"
    assert [(s.origin.layer_name, s.value) for s in prov.shadowed] == [
        ("site-b", ["198.51.100.254"])
    ]
    # without the border profile the site ladder points the router at itself
    plain = compose_for(
        binding(
            "border-router",
            site="site-b",
            profiles=("access",),
            loopback_v4="198.51.100.254",
        ),
        layers,
    )
    assert [(r.code, r.path) for r in plain.modules["ntp"].reasons] == [
        ("self_reference", "/servers")
    ]


def test_border_router_fixture_drift_matches_the_design_highlights():
    layers = replace_layer(
        "border",
        extra_layer(
            "profile",
            "border",
            {"ntp": {"module_version": 1, "params": {"servers": ["time.google.com"]}}},
            weight=400,
            kind="role",
        ),
    )
    b = binding(
        "border-router",
        site="site-b",
        profiles=("border",),
        loopback_v4="198.51.100.254",
    )
    rep = report_for(b, fixture_text("Lagos_Medallion"), revisions=layers)
    ntp_items = [
        (i.status.value, i.element)
        for i in rep.evaluations["ntp"].items
        if i.status is not DriftStatus.ok
    ]
    assert ntp_items == [("unexpected", "pool.ntp.org")]
    assert [(p.op, p.value, p.widening) for p in rep.evaluations["ntp"].proposals] == [
        ("add", "pool.ntp.org", False)
    ]
    snmp_items = {(i.path, i.status) for i in rep.evaluations["snmp"].items}
    assert ("/src_address", DriftStatus.drifted) in snmp_items
    assert any(
        path == "/btest/enabled"
        for path, _ in {
            (i.path, i.status) for i in rep.evaluations["services-hardening"].items
        }
    )


def test_higher_weight_profile_overrides_and_widening_is_computed_for_a_router_edit():
    layers = replace_layer(
        "access",
        extra_layer(
            "profile",
            "access",
            {"ntp": {"module_version": 1, "params": {"servers": ["192.0.2.60"]}}},
            weight=100,
            kind="role",
        ),
    )
    b = binding("aggregation-router", profiles=("access", "aggregation"))
    eff = compose_for(b, layers)
    assert eff.modules["ntp"].params["servers"] == [
        "192.0.2.60"
    ]  # profile outranks site
    # a router-layer edit that removes `www` from the disabled services is a widening
    before = compose_for(b, layers).modules["services-hardening"].params
    edit = extra_layer(
        "router",
        "id-aggregation-router",
        {
            "services-hardening": {
                "module_version": 1,
                "params": {"disabled_services": {"$remove": ["www"]}},
            }
        },
    )
    after = compose_for(b, layers + [edit]).modules["services-hardening"].params
    specs = ROUTER_PLUGIN.specs("services-hardening")
    assert [w.path for w in params_widening(specs, before, after)] == [
        "/disabled_services"
    ]
    assert [w.path for w in params_widening(specs, after, before)] == []
    assert mode_widening("enforce", "observe")


def test_effective_hash_changes_with_the_effective_state_only():
    b = binding()
    base = compose_for(b)
    same = compose_for(b, list(reversed(seed_layer_revisions())))
    assert base.router_hash == same.router_hash
    changed = compose_for(
        b,
        seed_layer_revisions()
        + [
            extra_layer(
                "router",
                "id-r-a",
                {
                    "snmp": {
                        "module_version": 1,
                        "params": {"trap_target": "192.0.2.77"},
                    }
                },
            ),
        ],
    )
    assert changed.router_hash != base.router_hash
    assert changed.modules["ntp"].effective_hash == base.modules["ntp"].effective_hash


def test_export_helper_round_trips_for_every_default_loopback():
    assert doc_of(export()).parsed
    assert LOOPBACK in export()
