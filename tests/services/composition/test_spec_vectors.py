"""§1.13 test vectors T1-T25 plus the merge rules they pin (the parity suite)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.composition.compose import (
    validate_exception,
    validate_layer_document,
)
from app.services.composition.drift import (
    ModuleStatus,
    NotEvaluated,
    Unknown,
)
from app.services.composition.evaluate import evaluate_module
from app.services.composition.exceptions import (
    ExceptionKind,
    ExceptionSpec,
    ExceptionState,
    exception_state,
)
from app.services.composition.fields import AccessKind, FieldSpec, Kind, Strategy
from app.services.composition.layers import LayerError
from app.services.composition.widening import mode_widening, widening

from .demo_plugin import (
    PLUGIN,
    block,
    doc,
    override,
    rev,
    run,
    subject,
)


def values(eff, module="m"):
    return eff.modules[module].params


def origin(eff, ptr, module="m"):
    return eff.modules[module].provenance[ptr]


# --------------------------------------------------------------------- T1-T5


def test_t1_later_tier_wins_and_shadow_is_kept():
    g = rev("global", "baseline", block("m", note="g", flag=False))
    s = rev("site", "site-a", block("m", note="s"))
    eff = run([s, g])
    assert values(eff)["note"] == "s"
    prov = origin(eff, "/note")
    assert prov.origin.layer_name == "site-a" and prov.origin.tier == "site"
    assert [(sh.origin.layer_name, sh.value) for sh in prov.shadowed] == [
        ("baseline", "g")
    ]


def test_t2_higher_weight_profile_wins_without_conflict():
    a = rev("profile", "access", block("m", x=1), weight=100)
    b = rev("profile", "aggregation", block("m", x=2), weight=200)
    eff = run([b, a])
    assert values(eff)["x"] == 2
    assert not eff.modules["m"].conflicts
    assert [s.value for s in origin(eff, "/x").shadowed] == [1]


def test_t3_equal_weight_conflict_keeps_name_order_and_blocks_planning():
    a = rev("group", "a", block("m", x=1), weight=100)
    b = rev("group", "b", block("m", x=2), weight=100)
    eff = run([a, b])
    assert values(eff)["x"] == 2  # name order, as NetBox
    result = eff.modules["m"]
    assert [c.path for c in result.conflicts] == ["/x"]
    assert result.blocked


def test_t3_same_values_do_not_conflict():
    a = rev("group", "a", block("m", x=1), weight=100)
    b = rev("group", "b", block("m", x=1), weight=100)
    assert not run([a, b]).modules["m"].conflicts


@pytest.mark.parametrize("first_adds", [True, False])
def test_t4_restrictive_allow_list_remove_beats_add_in_any_order(first_adds):
    base = rev("global", "g", block("m", sources=["192.0.2.0/24"]))
    add = {"$add": ["10.0.0.0/8"]}
    rem = {"$remove": ["10.0.0.0/8"]}
    # the same element is added by one layer and removed by the other
    a = rev("group", "a", block("m", sources=add if first_adds else rem), weight=100)
    b = rev("group", "b", block("m", sources=rem if first_adds else add), weight=100)
    eff = run([base, a, b])
    assert "10.0.0.0/8" not in values(eff)["sources"]
    assert not eff.modules["m"].conflicts


def test_t4_deny_list_add_beats_remove():
    base = rev("global", "g", block("m", denied=["telnet"]))
    a = rev("group", "a", block("m", denied={"$remove": ["telnet"]}), weight=100)
    b = rev("group", "b", block("m", denied={"$add": ["telnet"]}), weight=100)
    assert "telnet" in values(run([base, a, b]))["denied"]
    assert "telnet" in values(run([base, b, a]))["denied"]


def test_t4_restrictive_ordered_more_secure_wins():
    a = rev("group", "a", block("m", level="low"), weight=100)
    b = rev("group", "b", block("m", level="high"), weight=100)
    eff = run([a, b])
    assert values(eff)["level"] == "high" and not eff.modules["m"].conflicts
    eff2 = run([b, a])
    assert values(eff2)["level"] == "high"


def test_t5_replace_list_replaces():
    g = rev("global", "g", block("m", servers=["a.example", "b.example"]))
    s = rev("site", "s", block("m", servers=["c.example"]))
    assert values(run([g, s]))["servers"] == ["c.example"]


# --------------------------------------------------------------------- T6-T8


def test_t6_union_add_remove_replace_with_element_origins():
    g = rev("global", "g", block("m", routes=["A", "B"]))
    s = rev("site", "s", block("m", routes=["B", "C"]))
    assert values(run([g, s]))["routes"] == ["A", "B", "C"]
    eff = run([g, s])
    assert origin(eff, "/routes/B").contributors[0].layer_name == "g"
    assert [o.layer_name for o in origin(eff, "/routes/B").contributors] == ["g", "s"]
    assert origin(eff, "/routes/C").origin.layer_name == "s"
    r = rev("router", "r1", block("m", routes={"$remove": ["A"]}))
    eff = run([g, s, r])
    assert values(eff)["routes"] == ["B", "C"]
    assert "/routes/A" in eff.modules["m"].tombstones
    x = rev("group", "x", block("m", routes={"$replace": ["D"]}), weight=10)
    eff = run([g, s, x])
    assert values(eff)["routes"] == ["D"]
    assert origin(eff, "/routes/D").origin.layer_name == "x"


def test_t7_canonical_equality_deduplicates():
    g = rev("global", "g", block("m", sources=["10.0.0.1"]))
    s = rev("site", "s", block("m", sources=["10.0.0.1/32"]))
    assert values(run([g, s]))["sources"] == ["10.0.0.1/32"]


def test_t8_keyed_merge_and_delete():
    g = rev("global", "g", block("m", peers=[{"remote": "192.0.2.10", "asn": 1}]))
    s = rev(
        "site",
        "s",
        block(
            "m",
            peers=[
                {"remote": "192.0.2.10", "hold": 90},
                {"remote": "192.0.2.20", "asn": 2},
            ],
        ),
    )
    eff = run([g, s])
    assert values(eff)["peers"] == [
        {"remote": "192.0.2.10", "asn": 1, "hold": 90},
        {"remote": "192.0.2.20", "asn": 2},
    ]
    assert origin(eff, "/peers/192.0.2.10/asn").origin.layer_name == "g"
    assert origin(eff, "/peers/192.0.2.10/hold").origin.layer_name == "s"
    r = rev(
        "router", "r1", block("m", peers=[{"remote": "192.0.2.10", "$delete": True}])
    )
    eff = run([g, s, r])
    assert values(eff)["peers"] == [{"remote": "192.0.2.20", "asn": 2}]
    assert "/peers/192.0.2.10" in eff.modules["m"].tombstones
    deleted = eff.modules["m"].tombstones["/peers/192.0.2.10"]
    assert deleted.deleted_by.layer_name == "r1"


# -------------------------------------------------------------------- T9-T13


def test_t9_delete_inside_object_leaves_the_rest_and_a_tombstone():
    g = rev("global", "g", block("m", btest={"enabled": False, "authenticate": True}))
    r = rev("router", "r1", block("m", btest={"authenticate": {"$delete": True}}))
    eff = run([g, r])
    assert values(eff)["btest"] == {"enabled": False}
    assert "/btest/authenticate" in eff.modules["m"].tombstones


def test_t9_deleting_a_required_field_is_invalid_desired():
    g = rev(
        "global",
        "g",
        block("req", name="x", btest={"enabled": False, "authenticate": True}),
    )
    r = rev("router", "r1", block("req", btest={"authenticate": {"$delete": True}}))
    eff = run([g, r])
    reasons = eff.modules["req"].reasons
    assert [(x.code, x.path) for x in reasons] == [
        ("missing_required", "/btest/authenticate")
    ]
    assert eff.modules["req"].status == "invalid_desired"


def test_t9_required_cannot_be_deleted_at_global_tier():
    layer = rev("global", "g", block("req", name={"$delete": True}))
    issues = validate_layer_document(PLUGIN, layer.layer, layer.document)
    assert any("required at the global tier" in i for i in issues)


def test_t10_empty_object_patch_is_a_noop():
    g = rev("global", "g", block("m", btest={"enabled": False, "authenticate": True}))
    s = rev("site", "s", block("m", btest={}))
    before = run([g]).modules["m"].effective_hash
    after = run([g, s]).modules["m"].effective_hash
    assert before == after
    assert values(run([g, s]))["btest"] == {"enabled": False, "authenticate": True}


def test_t11_null_only_on_nullable_fields():
    ok = rev("global", "g", block("m", note=None))
    eff = run([ok])
    assert values(eff)["note"] is None
    assert origin(eff, "/note").origin.layer_name == "g"  # an explicit value
    bad = rev("global", "g2", block("m", x=None))
    issues = validate_layer_document(PLUGIN, bad.layer, bad.document)
    assert any("null" in i for i in issues)
    with pytest.raises(LayerError):
        run([bad])


def test_t12_params_of_unenrolled_modules_are_inert():
    s = rev("site", "s", block("m", mode=None, x=5))
    g = rev("global", "g", block("ntp", servers=["time.example"]))
    eff = run([s, g])
    assert "m" not in eff.modules
    assert "/m/x" in eff.inert
    assert "ntp" in eff.modules


def test_t13_module_delete_is_rejected_on_write():
    layer = rev("global", "g", {"schema": 1, "modules": {"m": {"$delete": True}}})
    issues = validate_layer_document(PLUGIN, layer.layer, layer.document)
    assert any("cannot be deleted" in i for i in issues)
    with pytest.raises(LayerError):
        run([layer])


# ------------------------------------------------------------------ T14-T16


def test_t14_exception_activity_and_lapse_window():
    exc = override("X-1", "m", "/btest", {"enabled": True}, until=date(2026, 11, 13))
    assert exception_state(exc, date(2026, 11, 13)) is ExceptionState.active
    assert exception_state(exc, date(2026, 11, 14)) is ExceptionState.lapsed
    assert exception_state(exc, date(2026, 11, 21)) is ExceptionState.lapsed
    assert exception_state(exc, date(2026, 11, 22)) is ExceptionState.expired
    g = rev("global", "g", block("m", btest={"enabled": False}))
    active = run([g], exceptions=[exc], today=date(2026, 11, 13))
    assert values(active)["btest"]["enabled"] is True
    po = origin(active, "/btest/enabled")
    assert po.origin.tier == "exception" and po.origin.detail["until"] == "2026-11-13"
    assert [s.value for s in po.shadowed] == [False]
    lapsed = run([g], exceptions=[exc], today=date(2026, 11, 14))
    assert values(lapsed)["btest"]["enabled"] is False
    assert [x.id for x in lapsed.lapsed] == ["X-1"]
    assert not run([g], exceptions=[exc], today=date(2026, 11, 22)).lapsed


def test_t14_widening_exception_needs_approval():
    exc = override(
        "X-2", "m", "/btest", {"enabled": True}, until=date(2026, 11, 13), widening=True
    )
    g = rev("global", "g", block("m", btest={"enabled": False}))
    assert exception_state(exc, date(2026, 10, 15)) is ExceptionState.pending_approval
    assert values(run([g], exceptions=[exc]))["btest"]["enabled"] is False


def test_t15_exception_on_a_derived_field_is_rejected():
    exc = override(
        "X-3", "fw", "", {"ipp_bgp": ["192.0.2.1"]}, until=date(2026, 11, 13)
    )
    assert any("derived" in i for i in validate_exception(PLUGIN, exc))
    with pytest.raises(LayerError):
        run([rev("global", "g", block("fw"))], exceptions=[exc])


def test_t15_layers_cannot_set_derived_fields_either():
    layer = rev("global", "g", block("fw", ipp_bgp=["192.0.2.1"]))
    assert any(
        "derived" in i
        for i in validate_layer_document(PLUGIN, layer.layer, layer.document)
    )


def test_t16_derivation_excludes_disabled_peers_and_names_inputs():
    g = rev(
        "global",
        "g",
        doc(
            block("bgp"),
            block("fw"),
            block(
                "bgp",
                peers=[
                    {"remote": "192.0.2.1"},
                    {"remote": "192.0.2.2", "enabled": False},
                    {"remote": "192.0.2.3", "enabled": True},
                ],
            ),
        ),
    )
    eff = run([g])
    assert eff.modules["fw"].params["ipp_bgp"] == ["192.0.2.1", "192.0.2.3"]
    prov = eff.modules["fw"].provenance["/ipp_bgp"]
    assert prov.origin.op == "derived" and prov.origin.detail["rule"] == "ipp-bgp"
    assert prov.origin.detail["inputs"][0]["ref"] == "bgp:/peers"


# ------------------------------------------------------------------ T17-T18


def _stack():
    return [
        rev("global", "g", block("m", routes=["A", "B"], servers=["x.example"], x=1)),
        rev(
            "site",
            "s",
            block("m", routes=["B", "C"], peers=[{"remote": "192.0.2.9", "asn": 7}]),
        ),
        rev("profile", "access", block("m", x=2, sources=["10.0.0.0/8"]), weight=100),
        rev(
            "profile",
            "aggregation",
            block("m", x=3, sources={"$remove": ["10.0.0.0/8"]}),
            weight=200,
        ),
        rev("group", "a", block("m", note="a"), weight=100),
        rev("group", "b", block("m", note="b"), weight=100),
        rev("router", "r1", block("m", routes={"$remove": ["A"]})),
    ]


def test_t17_permutation_invariance_of_hash_provenance_and_conflicts():
    import random

    baseline = run(_stack())
    exc = [
        override("X-1", "m", "/btest", {"enabled": True}, until=date(2026, 12, 1)),
        override("X-2", "m", "/btest", {"enabled": False}, until=date(2026, 12, 1)),
    ]
    reference = run(_stack(), exceptions=exc)
    rng = random.Random(4242)  # noqa: S311
    for _ in range(25):
        layers = _stack()
        rng.shuffle(layers)
        exceptions = list(exc)
        rng.shuffle(exceptions)
        eff = run(layers, exceptions=exceptions)
        assert eff.router_hash == reference.router_hash
        for key, result in reference.modules.items():
            other = eff.modules[key]
            assert other.effective_hash == result.effective_hash
            assert other.provenance == result.provenance
            assert other.conflicts == result.conflicts
        assert run(layers).router_hash == baseline.router_hash


@pytest.mark.parametrize("module", ["m"])
def test_t18_idempotence_for_every_strategy_via_repeated_patch(module):
    from app.services.composition.merge import Merger, Origin

    patch = {
        "x": 1,
        "servers": ["a.example"],
        "routes": ["B", "A"],
        "sources": ["10.0.0.0/8"],
        "peers": [{"remote": "192.0.2.5", "asn": 1}],
        "btest": {"enabled": False},
    }
    origin_a = Origin("global", 0, "L", "L", 1, 0)
    once, twice = (
        Merger(module, PLUGIN.specs(module)),
        Merger(module, PLUGIN.specs(module)),
    )
    once.merge_patch(patch, origin_a)
    twice.merge_patch(patch, origin_a)
    twice.merge_patch(patch, origin_a)
    assert once.values == twice.values
    removal = {
        "routes": {"$remove": ["A"]},
        "peers": [{"remote": "192.0.2.5", "$delete": True}],
    }
    once.merge_patch(removal, origin_a)
    twice.merge_patch(removal, origin_a)
    twice.merge_patch(removal, origin_a)
    assert once.values == twice.values


# ------------------------------------------------------------------ T19-T20


def test_t19_double_claim_invalidates_both_modules():
    g = rev(
        "global",
        "g",
        doc(
            block("bgp", peers=[{"remote": "192.0.2.1"}]),
            block("rr", peers=[{"remote": "192.0.2.1"}]),
        ),
    )
    eff = run([g])
    for key in ("bgp", "rr"):
        assert eff.modules[key].status == "invalid_desired"
        assert [r.code for r in eff.modules[key].reasons] == ["double_claim"]


def test_t19_disjoint_claims_are_fine():
    g = rev(
        "global",
        "g",
        doc(
            block("bgp", peers=[{"remote": "192.0.2.1"}]),
            block("rr", peers=[{"remote": "192.0.2.2"}]),
        ),
    )
    eff = run([g])
    assert all(r.ok for r in eff.modules.values())


def test_t20_own_loopback_is_a_self_reference():
    g = rev("global", "g", block("ntp", servers=["198.51.100.254", "time.example"]))
    eff = run([g], subj=subject(facts={"loopbacks": ("198.51.100.254",)}))
    assert [(r.code, r.path) for r in eff.modules["ntp"].reasons] == [
        ("self_reference", "/servers")
    ]
    clean = run([g], subj=subject(facts={"loopbacks": ("198.51.100.1",)}))
    assert clean.modules["ntp"].ok


# ------------------------------------------------------------------ T21-T23


def test_t22_unobserved_is_unknown_never_missing():
    g = rev("global", "g", block("m", x=1))
    eff = run([g])
    module = PLUGIN.modules["m"]
    for reason in (
        NotEvaluated.no_snapshot,
        NotEvaluated.stale,
        NotEvaluated.unparseable,
    ):
        evaluation = evaluate_module(module, eff.modules["m"], Unknown(reason), {})
        assert evaluation.status is ModuleStatus.not_evaluated
        assert evaluation.reason is reason
        assert not evaluation.items


def test_t22_invalid_desired_is_not_evaluated():
    eff = run([rev("global", "g", block("req", name="x"))])
    evaluation = evaluate_module(PLUGIN.modules["req"], eff.modules["req"], {}, {})
    assert evaluation.reason is NotEvaluated.invalid_desired


def test_t23_stale_layer_block_is_invalid_desired():
    g = rev("global", "g", block("m", version=2, x=1))
    eff = run([g])
    result = eff.modules["m"]
    assert result.status == "invalid_desired"
    assert [r.code for r in result.reasons] == ["stale_layer"]
    assert "x" not in result.params


# ---------------------------------------------------------------- T24, T25


def test_t24_widening_classifier():
    allow = FieldSpec(
        Kind.list, Strategy.union, access=AccessKind.allow_list, canon="cidr"
    )
    assert widening(allow, ["192.0.2.1/32"], ["192.0.2.0/24"])  # /32 -> covering /24
    assert not widening(allow, ["192.0.2.0/24"], ["192.0.2.1/32"])  # narrowing
    assert widening(allow, ["192.0.2.1/32"], ["192.0.2.1/32", "198.51.100.1/32"])
    deny = FieldSpec(Kind.list, Strategy.union, access=AccessKind.deny_list)
    assert widening(deny, ["telnet", "ftp"], ["telnet"])
    assert not widening(deny, ["telnet"], ["telnet", "ftp"])
    secure = FieldSpec(Kind.scalar, access=AccessKind.secure_bool, secure=False)
    assert widening(secure, False, True)  # write_access false -> true
    assert not widening(secure, True, False)
    ordered = FieldSpec(
        Kind.scalar, access=AccessKind.ordered, ranking=("enforce", "observe", "off")
    )
    assert widening(ordered, "enforce", "observe")  # module mode relaxed
    assert mode_widening("enforce", "observe") and mode_widening("observe", "off")
    assert not mode_widening("observe", "enforce")
    neutral = FieldSpec(Kind.list, Strategy.replace, canon="host")
    assert not widening(neutral, ["a.example"], ["b.example"])  # NTP server change


def test_t25_same_rank_keyed_field_conflict_is_field_level_only():
    g = rev(
        "global", "g", block("m", peers=[{"remote": "192.0.2.1", "asn": 1, "hold": 90}])
    )
    a = rev(
        "group", "a", block("m", peers=[{"remote": "192.0.2.1", "asn": 2}]), weight=100
    )
    b = rev(
        "group",
        "b",
        block("m", peers=[{"remote": "192.0.2.1", "asn": 3, "hold": 90}]),
        weight=100,
    )
    result = run([g, a, b]).modules["m"]
    assert [c.path for c in result.conflicts] == ["/peers/192.0.2.1/asn"]
    assert result.params["peers"][0]["asn"] == 3  # name order for display


# ------------------------------------------------------------ further rules


def test_matching_is_and_across_dimensions_or_within_one():
    only_a = rev(
        "group",
        "a",
        block("m", x=1),
        criteria={"tags": ("canary", "other"), "profiles": ("access",)},
    )
    assert (
        values(run([only_a], subj=subject(profiles=("access",), tags=("other",))))["x"]
        == 1
    )
    assert (
        "m"
        not in run([only_a], subj=subject(profiles=("core",), tags=("other",))).modules
    )
    assert (
        "m"
        not in run([only_a], subj=subject(profiles=("access",), tags=("none",))).modules
    )


def test_inactive_layers_are_skipped():
    layer = rev("global", "g", block("m", x=1))
    from dataclasses import replace

    inactive = replace(layer, layer=replace(layer.layer, active=False))
    assert run([inactive]).modules == {}


def test_tier_rules_are_enforced_on_write():
    bad_site = rev("site", "s", block("m", x=1), criteria={"tags": ("t",)})
    assert validate_layer_document(PLUGIN, bad_site.layer, bad_site.document)
    bad_global = rev("global", "g", block("m", x=1), criteria={"sites": ("s",)})
    assert validate_layer_document(PLUGIN, bad_global.layer, bad_global.document)
    bad_weight = rev("profile", "p", block("m", x=1), weight=450, kind="role")
    assert validate_layer_document(PLUGIN, bad_weight.layer, bad_weight.document)


def test_directive_rules_on_write():
    cases = [
        block("m", routes={"$add": ["A"], "$remove": ["A"]}),
        block("m", routes={"$bogus": ["A"]}),
        block("m", servers={"$add": ["a.example"]}),  # $add only on union
        block(
            "m", servers={"$replace": ["a.example"]}
        ),  # $replace not on replace lists
        block("m", peers=[{"remote": "192.0.2.1"}, {"remote": "192.0.2.1"}]),
        block("m", peers=[{"asn": 1}]),
        block("m", x={"$add": [1]}),
        block("m", sources=["not-a-cidr"]),
        block("m", x="text"),
        block("m", btest=5),
    ]
    for case in cases:
        layer = rev("global", "g", case)
        assert validate_layer_document(PLUGIN, layer.layer, layer.document), case


def test_keyed_replace_and_union_replace_use_exact_lists():
    g = rev(
        "global",
        "g",
        block("m", peers=[{"remote": "192.0.2.1"}, {"remote": "192.0.2.2"}]),
    )
    s = rev(
        "site", "s", block("m", peers={"$replace": [{"remote": "192.0.2.3", "asn": 5}]})
    )
    eff = run([g, s])
    assert values(eff)["peers"] == [{"remote": "192.0.2.3", "asn": 5}]
    assert "/peers/192.0.2.1" in eff.modules["m"].tombstones


def test_replace_directive_conflict_at_equal_rank():
    a = rev("group", "a", block("m", routes={"$replace": ["A"]}), weight=100)
    b = rev("group", "b", block("m", routes={"$replace": ["B"]}), weight=100)
    assert [c.path for c in run([a, b]).modules["m"].conflicts] == ["/routes"]


def test_delete_vs_set_at_equal_rank_is_a_conflict():
    a = rev("group", "a", block("m", x=1), weight=100)
    b = rev("group", "b", block("m", x={"$delete": True}), weight=100)
    assert run([a, b]).modules["m"].conflicts


def test_keyed_order_field_sorts_stably():
    g = rev(
        "global",
        "g",
        block("m", peers=[{"remote": "192.0.2.9"}, {"remote": "192.0.2.2"}]),
    )
    assert [p["remote"] for p in values(run([g]))["peers"]] == [
        "192.0.2.2",
        "192.0.2.9",
    ]


def test_mode_override_exception_and_suppress_and_annotation():
    g = rev("global", "g", block("m", x=1))
    mode = ExceptionSpec(
        "X-m",
        "m",
        ExceptionKind.mode_override,
        "temporary observe window",
        date(2026, 10, 14),
        date(2026, 11, 1),
        subject_key="r1",
        mode="observe",
    )
    eff = run([g], exceptions=[mode])
    assert eff.modules["m"].mode == "observe"
    assert eff.modules["m"].provenance["/$mode"].origin.tier == "exception"
    sup = ExceptionSpec(
        "X-s",
        "m",
        ExceptionKind.suppress,
        "known deviation under review",
        date(2026, 10, 14),
        date(2026, 11, 1),
        subject_key="r1",
        scope="/x",
    )
    note = ExceptionSpec(
        "X-a",
        "m",
        ExceptionKind.annotation,
        "owner note for the router",
        date(2026, 10, 14),
        date(2026, 11, 1),
        subject_key="r1",
    )
    eff = run([g], exceptions=[sup, note])
    assert [s.scope for s in eff.modules["m"].suppressions] == ["/x"]
    assert eff.annotations == (("m", "owner note for the router", "X-a"),)
    assert (
        eff.modules["m"].params["x"] == 1
    )  # suppress and annotation never change the state


def test_exception_validation_rules():
    base = override("X", "m", "/btest", {"enabled": True}, until=date(2026, 11, 13))
    from dataclasses import replace

    assert not validate_exception(PLUGIN, base)
    assert validate_exception(PLUGIN, replace(base, reason="short"))
    assert validate_exception(PLUGIN, replace(base, until=date(2027, 12, 1)))
    assert validate_exception(PLUGIN, replace(base, until=None))
    assert validate_exception(PLUGIN, replace(base, permanent=True))
    assert validate_exception(PLUGIN, replace(base, subject_key=None))
    assert validate_exception(PLUGIN, replace(base, module_key="nope"))


def test_two_exceptions_on_one_path_conflict():
    g = rev("global", "g", block("m", btest={"enabled": False}))
    a = override("X-a", "m", "/btest", {"enabled": True}, until=date(2026, 12, 1))
    b = override("X-b", "m", "/btest", {"enabled": False}, until=date(2026, 12, 1))
    assert run([g], exceptions=[a, b]).modules["m"].conflicts


def test_provenance_is_complete_and_points_only_at_applicable_layers():
    from app.services.composition.compose import provenance_gaps

    eff = run(_stack())
    names = {layer.name for layer in eff.layers} | {"m@1"}
    for result in eff.modules.values():
        assert not provenance_gaps(PLUGIN, result)
        for prov in result.provenance.values():
            assert prov.origin.layer_name in names
            assert all(o.layer_name in names for o in prov.contributors)
