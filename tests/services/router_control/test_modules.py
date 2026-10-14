"""snmp, ntp and services-hardening: observed-state extraction and drift."""

from __future__ import annotations

import json

import pytest

from app.services.composition.drift import DriftStatus, ModuleStatus, NotEvaluated
from app.services.router_control.modules import ntp, services_hardening, snmp
from app.services.router_control.plugin import ROUTER_PLUGIN
from app.services.router_control.seed_layers import seed_layer_revisions
from app.services.router_health.contracts import CheckId
from app.services.router_health.posture_checks import evaluate_posture
from tests.services.router_control.support import (
    LOOPBACK,
    POLLER_SNMP,
    SERVICES_OK,
    binding,
    compose_for,
    doc_of,
    export,
    extra_layer,
    fixture_text,
    report_for,
)
from tests.services.router_health.support import default_policy, effective

SENTINEL = "SENTINEL-COMMUNITY-NAME"


def statuses(report, key):
    return [(i.status.value, i.path, i.element) for i in report.evaluations[key].items]


def drift_items(report, key):
    return [
        i
        for i in report.evaluations[key].items
        if i.status not in {DriftStatus.ok, DriftStatus.not_applicable}
    ]


# ------------------------------------------------------------------------ snmp


def test_snmp_conforms_after_lockdown():
    rep = report_for(binding(), export())
    assert rep.evaluations["snmp"].status is ModuleStatus.conform


def test_snmp_derived_source_follows_the_loopback_fact():
    rep = report_for(binding(loopback_v4="198.51.100.98"), export())
    items = drift_items(rep, "snmp")
    assert [(i.path, i.desired, i.observed) for i in items] == [
        ("/src_address", "198.51.100.98", LOOPBACK)
    ]
    eff = rep.effective["snmp"]["values"]
    row = next(r for r in eff if r["path"] == "/src_address")
    assert row["origin"]["op"] == "derived"
    assert row["origin"]["detail"]["inputs"][0]["ref"] == "fact:loopback_v4"


def test_snmp_without_loopback_leaves_source_unmanaged():
    rep = report_for(
        binding(loopback_v4=None), export(snmp=POLLER_SNMP.replace(LOOPBACK, "0.0.0.0"))
    )
    assert rep.evaluations["snmp"].status is ModuleStatus.conform
    assert all(r["path"] != "/src_address" for r in rep.effective["snmp"]["values"])


def test_snmp_open_default_community_is_unexpected_exposure_and_never_proposed():
    text = export(
        snmp="/snmp\nset enabled=yes trap-target=192.0.2.1 src-address=198.51.100.97\n"
    )
    rep = report_for(binding(), text)
    items = drift_items(rep, "snmp")
    assert ("unexpected", "/allowed_sources", "::/0") in [
        (i.status.value, i.path, i.element) for i in items
    ]
    assert rep.evaluations["snmp"].proposals == ()  # a catch-all is never proposed


def test_snmp_extra_source_is_proposed_as_a_widening():
    text = export(
        snmp=POLLER_SNMP.replace("192.0.2.4/32", "192.0.2.4/32,203.0.113.7/32")
    )
    rep = report_for(binding(), text)
    proposals = rep.evaluations["snmp"].proposals
    assert [(p.op, p.value, p.widening) for p in proposals] == [
        ("add", "203.0.113.7/32", True)
    ]
    assert rep.evaluations["snmp"].status is ModuleStatus.drift


def test_snmp_writable_community_is_critical_drift():
    text = export(
        snmp=POLLER_SNMP
        + "/snmp community\nadd name=x addresses=192.0.2.1/32 write-access=yes\n"
    )
    items = drift_items(report_for(binding(), text), "snmp")
    assert any(i.path == "/write_access" and i.severity == "critical" for i in items)


def test_snmp_disabled_when_wanted_is_reported_without_source_noise():
    text = export(snmp="/snmp\nset enabled=no\n")
    items = drift_items(report_for(binding(), text), "snmp")
    assert [(i.status.value, i.path) for i in items] == [("disabled", "/enabled")]


def test_snmp_never_reads_or_prints_community_names():
    text = export(
        snmp=POLLER_SNMP
        + f"/snmp community\nadd name={SENTINEL} addresses=192.0.2.1/32\n"
    )
    from app.services.router_control.report import (
        build_fleet_report,
        render_text,
        to_json,
    )
    from app.services.router_control.seed_layers import seed_layer_revisions
    from tests.services.router_control.support import NOW, TODAY, snapshot

    bnd = binding()
    fleet = build_fleet_report(
        [bnd],
        {bnd.router_id: snapshot(text)},
        seed_layer_revisions(),
        [],
        today=TODAY,
        now=NOW,
    )
    assert SENTINEL not in render_text(fleet)
    assert SENTINEL not in json.dumps(to_json(fleet), default=str)
    observed = snmp.MODULE.extract_observed(doc_of(text))
    assert SENTINEL not in repr(observed)


# ------------------------------------------------------------------------- ntp


def test_ntp_conform_and_missing_server():
    assert (
        report_for(binding(), export()).evaluations["ntp"].status
        is ModuleStatus.conform
    )
    text = export(
        ntp="/system ntp client\nset enabled=yes\n/system ntp client servers\nadd address=203.0.113.9\n"
    )
    rep = report_for(binding(), text)
    assert {(s, e) for s, _p, e in statuses(rep, "ntp") if s != "ok"} == {
        ("missing", "192.0.2.92"),
        ("unexpected", "203.0.113.9"),
    }
    proposal = rep.evaluations["ntp"].proposals[0]
    assert (proposal.op, proposal.value, proposal.widening) == (
        "add",
        "203.0.113.9",
        False,
    )


def test_ntp_client_disabled():
    text = export(
        ntp="/system ntp client\nset enabled=no\n/system ntp client servers\nadd address=192.0.2.92\n"
    )
    assert ("disabled", "/client_enabled", None) in statuses(
        report_for(binding(), text), "ntp"
    )


def test_ntp_legacy_servers_property_is_read():
    doc = doc_of(
        export(
            ntp="/system ntp client\nset enabled=yes servers=Time.Example.NET,192.0.2.5\n"
        )
    )
    observed = ntp.MODULE.extract_observed(doc)
    assert observed.servers == ("time.example.net", "192.0.2.5")


def test_ntp_agrees_with_the_posture_check():
    cases = [
        (
            "/system ntp client\nset enabled=yes\n/system ntp client servers\nadd address=192.0.2.9\n",
            False,
        ),
        (
            "/system ntp client\nset enabled=no\n/system ntp client servers\nadd address=192.0.2.9\n",
            True,
        ),
        ("/system ntp client\nset enabled=yes\n", True),
    ]
    for ntp_text, finding_expected in cases:
        text = export(ntp=ntp_text)
        doc = doc_of(text)
        result = evaluate_posture(
            doc,
            effective(default_policy()),
            router_label="r",
            suspended_list="suspended",
        )
        finding = any(f.check is CheckId.ntp_client_off for f in result.findings)
        assert finding is finding_expected
        # the module has no servers to compare when the client has none configured
        observed = ntp.MODULE.extract_observed(doc)
        broken = (not observed.client_enabled) or not observed.servers
        assert broken is finding_expected


# ------------------------------------------------------------ services-hardening


def test_services_hardening_conform():
    rep = report_for(binding(), export())
    assert rep.evaluations["services-hardening"].status is ModuleStatus.conform


def test_reverse_proxy_is_not_applicable_below_7_22_and_drift_above():
    old = report_for(
        binding(),
        export(
            "7.16.2",
            services=SERVICES_OK.replace("set reverse-proxy disabled=yes\n", ""),
        ),
    )
    entry = next(
        i
        for i in old.evaluations["services-hardening"].items
        if i.element == "reverse-proxy"
    )
    assert entry.status is DriftStatus.not_applicable
    assert old.evaluations["services-hardening"].status is ModuleStatus.conform
    new = report_for(
        binding(),
        export(
            "7.22.2",
            services=SERVICES_OK.replace("set reverse-proxy disabled=yes\n", ""),
        ),
    )
    entry = next(
        i
        for i in new.evaluations["services-hardening"].items
        if i.element == "reverse-proxy"
    )
    assert entry.status is DriftStatus.drifted


def test_reverse_proxy_section_disable_counts():
    text = export(
        "7.22.2",
        services=SERVICES_OK.replace("set reverse-proxy disabled=yes\n", "")
        + "/ip reverse-proxy\nset enabled=no\n",
    )
    rep = report_for(binding(), text)
    assert rep.evaluations["services-hardening"].status is ModuleStatus.conform


@pytest.mark.parametrize("version", ["7.16.2", "7.20.8", "7.22.2", "7.23.8"])
def test_reverse_proxy_agrees_with_the_posture_check(version):
    for line, enabled in (("set reverse-proxy disabled=yes\n", False), ("", True)):
        text = export(
            version,
            services=SERVICES_OK.replace("set reverse-proxy disabled=yes\n", line),
        )
        doc = doc_of(text)
        result = evaluate_posture(
            doc,
            effective(default_policy()),
            router_label="r",
            suspended_list="suspended",
        )
        finding = any(f.check is CheckId.reverse_proxy_enabled for f in result.findings)
        rep = report_for(binding(), text)
        entry = next(
            i
            for i in rep.evaluations["services-hardening"].items
            if i.element == "reverse-proxy"
        )
        assert (entry.status is DriftStatus.drifted) is finding
        if finding:
            assert enabled


def test_btest_defaults_to_enabled_and_drifts():
    text = export(services=SERVICES_OK.split("/tool")[0])
    items = drift_items(report_for(binding(), text), "services-hardening")
    assert [(i.path, i.desired, i.observed) for i in items] == [
        ("/btest/enabled", False, True)
    ]


def test_exposed_helpers_are_drift():
    extra = "/ip dns\nset allow-remote-requests=yes\n/ip proxy\nset enabled=yes\n/ip socks\nset enabled=yes\n/ip upnp\nset enabled=yes\n"
    items = drift_items(
        report_for(binding(), export(services=SERVICES_OK + extra)),
        "services-hardening",
    )
    assert {i.path for i in items} == {"/dns_allow_remote", "/proxy", "/socks", "/upnp"}


def test_disabled_services_defaults_for_absent_lines():
    # a service that is not disabled in the export is enabled (RouterOS default)
    text = export(services=SERVICES_OK.replace("set telnet disabled=yes\n", ""))
    items = drift_items(report_for(binding(), text), "services-hardening")
    assert [(i.path, i.element) for i in items] == [("/disabled_services", "telnet")]


def test_romon_and_mac_winbox_are_only_compared_once_a_layer_sets_them():
    from app.services.router_control.seed_layers import seed_layer_revisions
    from tests.services.router_control.support import extra_layer

    layers = seed_layer_revisions() + [
        extra_layer(
            "router",
            "id-r-a",
            {
                "services-hardening": {
                    "module_version": 1,
                    "params": {"romon": False, "mac_winbox": False},
                }
            },
        )
    ]
    text = export(services=SERVICES_OK + "/tool romon\nset enabled=yes\n")
    items = drift_items(
        report_for(binding(), text, revisions=layers), "services-hardening"
    )
    assert {i.path for i in items} == {"/romon", "/mac_winbox"}


# ------------------------------------------------------------------ real fixtures


@pytest.mark.parametrize("name", ["Gudu", "Lokogoma", "Lagos_Medallion"])
def test_modules_run_on_real_redacted_fixtures(name):
    loopback = {
        "Gudu": "198.51.100.97",
        "Lokogoma": "192.0.2.90",
        "Lagos_Medallion": "198.51.100.254",
    }[name]
    site = "site-b" if name == "Lagos_Medallion" else "site-a"
    profile = "border" if name == "Lagos_Medallion" else "access"
    rep = report_for(
        binding(name, site=site, profiles=(profile,), loopback_v4=loopback),
        fixture_text(name),
    )
    assert set(rep.evaluations) == {"snmp", "ntp", "services-hardening"}
    assert all(
        e.status is not ModuleStatus.not_evaluated for e in rep.evaluations.values()
    )


def test_fixture_parity_snmp_drift_equals_snmp_exposed():
    for name in ("Gudu", "Lokogoma", "Lagos_Medallion"):
        text = fixture_text(name)
        doc = doc_of(text)
        result = evaluate_posture(
            doc,
            effective(default_policy()),
            router_label=name,
            suspended_list="suspended",
        )
        finding = any(f.check is CheckId.snmp_exposed for f in result.findings)
        rep = report_for(binding(name, site="site-a", profiles=("access",)), text)
        exposure = [
            i
            for i in rep.evaluations["snmp"].items
            if (i.path == "/allowed_sources" and i.status is DriftStatus.unexpected)
            or (i.path == "/write_access" and i.status is DriftStatus.drifted)
        ]
        assert bool(exposure) is finding, name


def test_synthetic_variants_snmp_parity():
    variants = [
        POLLER_SNMP,
        "/snmp\nset enabled=yes\n",
        "/snmp\nset enabled=no\n",
        POLLER_SNMP + "/snmp community\nadd name=a addresses=203.0.113.1/32\n",
        POLLER_SNMP
        + "/snmp community\nadd name=a addresses=192.0.2.1/32 write-access=yes\n",
        POLLER_SNMP
        + "/snmp community\nadd name=a addresses=192.0.2.1/32 disabled=yes write-access=yes\n",
        POLLER_SNMP + "/snmp community\nadd name=a addresses=192.0.2.0/24\n",
    ]
    for snmp_text in variants:
        text = export(snmp=snmp_text)
        result = evaluate_posture(
            doc_of(text),
            effective(default_policy()),
            router_label="r",
            suspended_list="suspended",
        )
        finding = any(f.check is CheckId.snmp_exposed for f in result.findings)
        rep = report_for(binding(), text)
        exposure = [
            i
            for i in rep.evaluations["snmp"].items
            if (i.path == "/allowed_sources" and i.status is DriftStatus.unexpected)
            or (
                i.path == "/write_access"
                and i.status is DriftStatus.drifted
                and i.observed is True
            )
        ]
        assert bool(exposure) is finding, snmp_text


def test_synthetic_version_forms_parse_and_evaluate():
    for version in (
        "7.13.2",
        "7.20.8",
        "7.23.8",
    ):  # [I] synthetic until real exports exist
        rep = report_for(binding(), export(version))
        assert rep.evaluations["snmp"].status is ModuleStatus.conform


def test_non_export_observation_is_unparseable():
    assert snmp.MODULE.extract_observed(None).reason is NotEvaluated.unparseable
    assert ntp.MODULE.extract_observed("text").reason is NotEvaluated.unparseable
    assert (
        services_hardening.MODULE.extract_observed(3).reason is NotEvaluated.unparseable
    )


def test_module_registry_is_complete_and_claims_are_disjoint():
    from app.services.composition.plugin import plugin_problems
    from app.services.router_control.surfaces import claim_problems

    assert set(ROUTER_PLUGIN.modules) == {"snmp", "ntp", "services-hardening"}
    assert plugin_problems(ROUTER_PLUGIN) == []
    assert claim_problems(ROUTER_PLUGIN) == []


@pytest.mark.parametrize("name", ["ssh", "api", "www-ssl"])
def test_disabled_services_refuses_the_management_transport(name):
    layers = seed_layer_revisions() + [
        extra_layer(
            "router",
            "id-r-a",
            {
                "services-hardening": {
                    "module_version": 1,
                    "params": {"disabled_services": {"$add": [name]}},
                }
            },
        )
    ]
    result = compose_for(binding(), layers).modules["services-hardening"]
    assert result.status == "invalid_desired"
    assert [r.code for r in result.reasons] == ["management_transport"]
    assert name in result.reasons[0].detail


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("winbox", "unclaimed_service"),
        ("gopher", "unknown_service"),
        ("Telnet2", "unknown_service"),
    ],
)
def test_disabled_services_refuses_unclaimed_and_unknown_names(name, code):
    layers = seed_layer_revisions() + [
        extra_layer(
            "router",
            "id-r-a",
            {
                "services-hardening": {
                    "module_version": 1,
                    "params": {"disabled_services": {"$add": [name]}},
                }
            },
        )
    ]
    result = compose_for(binding(), layers).modules["services-hardening"]
    assert [r.code for r in result.reasons] == [code]
    rep = report_for(binding(), export(), revisions=layers)
    assert rep.evaluations["services-hardening"].reason is NotEvaluated.invalid_desired


def test_every_known_service_is_accepted_and_the_seed_is_valid():
    from app.services.router_control.modules.services_hardening import KNOWN_SERVICES

    layers = seed_layer_revisions()
    result = compose_for(binding(), layers).modules["services-hardening"]
    assert result.status == "ok"
    assert set(result.params["disabled_services"]) <= set(KNOWN_SERVICES)


def test_known_service_sets_are_derived_from_the_unclaimed_surface_registry():
    from app.services.router_control import surfaces

    assert surfaces.UNCLAIMED_SERVICES == {"ssh", "winbox", "api", "www-ssl"}
    assert surfaces.MANAGEMENT_TRANSPORT_SERVICES <= surfaces.UNCLAIMED_SERVICES


def test_seed_core_and_border_profiles_enable_the_ntp_server():
    layers = seed_layer_revisions()
    for profile in ("core", "border"):
        eff = compose_for(binding(profiles=(profile,), site="site-b"), layers)
        assert eff.modules["ntp"].params["server_enabled"] is True, profile
    eff = compose_for(binding(profiles=("access",)), layers)
    assert "server_enabled" not in eff.modules["ntp"].params
