"""Drift report: statuses, exit codes, refusal and output shape."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from app.services.composition.drift import ModuleStatus, NotEvaluated
from app.services.composition.exceptions import ExceptionKind, ExceptionSpec
from app.services.router_control.report import (
    EXIT_CONFORM,
    EXIT_DRIFT,
    EXIT_NOTHING_EVALUATED,
    EXIT_REFUSED,
    RouterOutcome,
    build_fleet_report,
    render_text,
    to_json,
)
from app.services.router_control.seed_layers import seed_layer_revisions
from tests.services.router_control.support import (
    NOW,
    SERVICES_OK,
    TODAY,
    binding,
    export,
    extra_layer,
    report_for,
    snapshot,
)


def fleet(bindings, snaps, layers=None, exceptions=None):
    return build_fleet_report(
        bindings,
        snaps,
        layers if layers is not None else seed_layer_revisions(),
        exceptions or [],
        today=TODAY,
        now=NOW,
        seed="test",
    )


def test_exit_codes_conform_drift_refused_and_nothing_evaluated():
    ok, bad = binding("r-ok"), binding("r-bad")
    good_text = export()
    drifting = export(services=SERVICES_OK.split("/tool")[0])
    assert fleet([ok], {ok.router_id: snapshot(good_text)}).exit_code == EXIT_CONFORM
    assert (
        fleet(
            [ok, bad],
            {ok.router_id: snapshot(good_text), bad.router_id: snapshot(drifting)},
        ).exit_code
        == EXIT_DRIFT
    )
    assert fleet([ok], {}).exit_code == EXIT_NOTHING_EVALUATED
    garbled = fleet([ok], {ok.router_id: snapshot("not an export")})
    assert garbled.exit_code == EXIT_REFUSED


def test_no_snapshot_and_stale_are_not_evaluated_never_missing():
    rep = report_for(binding(), None)
    assert {e.reason for e in rep.evaluations.values()} == {NotEvaluated.no_snapshot}
    assert all(not e.items for e in rep.evaluations.values())
    stale = report_for(binding(), export(), captured_at=NOW - timedelta(hours=72))
    assert {e.reason for e in stale.evaluations.values()} == {NotEvaluated.stale}
    assert all(
        e.status is ModuleStatus.not_evaluated for e in stale.evaluations.values()
    )


def test_unparseable_snapshot_is_refused():
    rep = report_for(binding(), "garbage\nmore garbage\n")
    assert {e.reason for e in rep.evaluations.values()} == {NotEvaluated.unparseable}


def test_missing_required_value_makes_the_module_invalid_desired_and_refuses():
    # no site: the ntp servers (required) are never set
    rep = report_for(binding(site=None), export())
    ntp = rep.evaluations["ntp"]
    assert (ntp.status, ntp.reason) == (
        ModuleStatus.not_evaluated,
        NotEvaluated.invalid_desired,
    )
    assert rep.effective["ntp"]["reasons"][0]["code"] == "missing_required"
    assert (
        fleet([binding(site=None)], {"id-r-a": snapshot(export())}).exit_code
        == EXIT_REFUSED
    )


def test_equal_weight_conflict_refuses_but_still_reports_drift():
    layers = seed_layer_revisions() + [
        extra_layer(
            "group",
            "g1",
            {"snmp": {"module_version": 1, "params": {"trap_target": "192.0.2.50"}}},
            weight=10,
            criteria={"tags": ("canary",)},
        ),
        extra_layer(
            "group",
            "g2",
            {"snmp": {"module_version": 1, "params": {"trap_target": "192.0.2.51"}}},
            weight=10,
            criteria={"tags": ("canary",)},
        ),
    ]
    b = binding(tags=("canary",))
    rep = report_for(b, export(), revisions=layers)
    assert [c["path"] for c in rep.conflicts] == ["/trap_target"]
    assert rep.evaluations["snmp"].items  # drift is still computed
    assert (
        fleet([b], {b.router_id: snapshot(export())}, layers).exit_code == EXIT_REFUSED
    )


def test_invalid_layers_refuse_with_issues_and_no_evaluation():
    layers = seed_layer_revisions() + [
        extra_layer("global", "bad", {"snmp": {"$delete": True}}, weight=1),
    ]
    rep = report_for(binding(), export(), revisions=layers)
    assert rep.outcome is RouterOutcome.refused
    assert any("cannot be deleted" in issue for issue in rep.issues)
    assert fleet([binding()], {}, layers).exit_code == EXIT_REFUSED


def test_unmanaged_and_retired_are_skipped_and_observe_only_drift_is_labelled():
    for lifecycle in ("unmanaged", "retired"):
        assert (
            report_for(binding(lifecycle=lifecycle), export()).outcome
            is RouterOutcome.skipped
        )
    drifting = export(services=SERVICES_OK.split("/tool")[0])
    rep = report_for(binding(lifecycle="observe_only"), drifting)
    assert rep.evaluations["services-hardening"].status is ModuleStatus.observe_only
    # not enforced, but still drift: exit 1 and labelled in both outputs
    observed = fleet(
        [binding(lifecycle="observe_only")], {"id-r-a": snapshot(drifting)}
    )
    assert observed.exit_code == EXIT_DRIFT
    assert "[observe-only: not enforced, still drift]" in render_text(observed)
    assert "observe-only drift:" in render_text(observed)
    assert to_json(observed)["routers"][0]["observe_only_drift"]
    assert (
        report_for(binding(lifecycle="bogus"), export()).outcome
        is RouterOutcome.refused
    )
    assert report_for(binding(profiles=()), export()).outcome is RouterOutcome.refused


def test_mode_off_and_observe_modules():
    layers = seed_layer_revisions() + [
        extra_layer(
            "router",
            "id-r-a",
            {
                "ntp": {"module_version": 1, "mode": "off"},
                "snmp": {"module_version": 1, "mode": "observe"},
            },
        ),
    ]
    rep = report_for(
        binding(), export(services=SERVICES_OK.split("/tool")[0]), revisions=layers
    )
    assert rep.evaluations["ntp"].status is ModuleStatus.off
    assert rep.evaluations["snmp"].status is ModuleStatus.observe_only


def test_dated_exception_turns_drift_into_conform_then_lapses():
    drifting = export(services=SERVICES_OK.split("/tool")[0])
    exc = ExceptionSpec(
        "X-1",
        "services-hardening",
        ExceptionKind.override,
        "engineers run bandwidth tests from the management ranges",
        TODAY,
        TODAY + timedelta(days=30),
        subject_key="id-r-a",
        path="/btest",
        patch={"enabled": True, "authenticate": True},
        approved=True,
    )
    rep = report_for(binding(), drifting, exceptions=[exc])
    assert rep.evaluations["services-hardening"].status is ModuleStatus.conform
    origin = next(
        r
        for r in rep.effective["services-hardening"]["values"]
        if r["path"] == "/btest/enabled"
    )["origin"]
    assert origin["tier"] == "exception" and origin["shadowed"][0]["value"] is False
    unapproved = report_for(
        binding(), drifting, exceptions=[replace(exc, approved=False)]
    )
    assert unapproved.evaluations["services-hardening"].status is ModuleStatus.drift
    later = build_fleet_report(
        [binding()],
        {"id-r-a": snapshot(drifting)},
        seed_layer_revisions(),
        [exc],
        today=TODAY + timedelta(days=31),
        now=NOW,
    )
    assert [x.id for x in later.routers[0].lapsed] == ["X-1"]
    assert (
        later.routers[0].evaluations["services-hardening"].status is ModuleStatus.drift
    )


def test_suppress_exception_marks_items_excepted():
    drifting = export(services=SERVICES_OK.split("/tool")[0])
    exc = ExceptionSpec(
        "X-2",
        "services-hardening",
        ExceptionKind.suppress,
        "known deviation under review",
        TODAY,
        TODAY + timedelta(days=30),
        subject_key="id-r-a",
        scope="/btest/enabled",
    )
    rep = report_for(binding(), drifting, exceptions=[exc])
    assert rep.evaluations["services-hardening"].status is ModuleStatus.excepted
    assert all(
        i.excepted
        for i in rep.evaluations["services-hardening"].items
        if i.status.value == "drifted"
    )


def test_output_shape_text_and_json_carry_origins_hashes_and_exit_code():
    b = binding()
    result = fleet(
        [b], {b.router_id: snapshot(export(services=SERVICES_OK.split("/tool")[0]))}
    )
    text = render_text(result)
    assert "== r-a" in text and "exit 1" in text
    assert "<- global fleet-baseline r1" in text
    assert (
        "layers: global:fleet-baseline(r1) > site:site-a(r1) > profile:access(r1)"
        in text
    )
    data = json.loads(json.dumps(to_json(result), default=str))
    router = data["routers"][0]
    assert data["exit_code"] == 1 and router["router_hash"]
    assert set(router["modules"]) == {"snmp", "ntp", "services-hardening"}
    row = next(
        r for r in router["effective"]["ntp"]["values"] if r["path"] == "/servers"
    )
    assert row["origin"]["layer"] == "site-a" and row["origin"]["tier"] == "site"
    assert router["effective"]["ntp"]["provenance_gaps"] == []


def test_report_is_deterministic_across_input_order():
    bs = [binding("r-b", router_id="id-b"), binding("r-a", router_id="id-a")]
    snaps = {b.router_id: snapshot(export()) for b in bs}
    layers = seed_layer_revisions()
    first = to_json(fleet(bs, snaps, layers))
    second = to_json(fleet(list(reversed(bs)), snaps, list(reversed(layers))))
    assert first == second
    assert [r["router"] for r in first["routers"]] == ["r-a", "r-b"]


def test_inert_params_are_listed():
    layers = seed_layer_revisions() + [
        extra_layer(
            "site",
            "site-a2",
            {"unknown-later": {"module_version": 1, "params": {"x": 1}}},
            weight=5,
            criteria={"sites": ("site-a",)},
        ),
    ]
    rep = report_for(binding(), export(), revisions=layers)
    assert (
        rep.outcome is RouterOutcome.refused
    )  # unknown modules are not silently ignored
    assert any("unknown module" in i for i in rep.issues)


def test_observe_mode_module_drift_is_reported_and_exits_1():
    layers = seed_layer_revisions() + [
        extra_layer(
            "router",
            "id-r-a",
            {"services-hardening": {"module_version": 1, "mode": "observe"}},
        )
    ]
    drifting = export(services=SERVICES_OK.split("/tool")[0])
    observed = fleet([binding()], {"id-r-a": snapshot(drifting)}, layers)
    rep = observed.routers[0]
    assert rep.evaluations["services-hardening"].status is ModuleStatus.observe_only
    assert observed.exit_code == EXIT_DRIFT
    assert [m for m, _ in rep.observe_only_drift] == ["services-hardening"] * len(
        rep.observe_only_drift
    )
    text = render_text(observed)
    assert "observe-only drift:" in text and "exit 1" in text
    # an observe-mode module with nothing drifting stays conform (exit 0)
    clean = fleet([binding()], {"id-r-a": snapshot(export())}, layers)
    assert clean.exit_code == EXIT_CONFORM
    assert "observe-only drift:" not in render_text(clean)


def test_a_binding_without_a_loopback_says_the_self_reference_check_did_not_run():
    rep = report_for(binding(loopback_v4=None), export())
    assert rep.not_checked and "self-reference" in rep.not_checked[0]
    fl = fleet([binding(loopback_v4=None)], {"id-r-a": snapshot(export())})
    assert "NOT CHECKED" in render_text(fl)
    assert to_json(fl)["routers"][0]["not_checked"]
    assert not report_for(binding(), export()).not_checked
