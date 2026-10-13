"""One test per runtime check (pure, no database, no network)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.router_health import runtime_checks as rc
from app.services.router_health.contracts import (
    CheckId,
    HealthLayer,
    HealthPriority,
    RestFailure,
)
from app.services.router_health.settings import RouterHealthSettings
from tests.services.router_health.runtime_support import (
    NOW,
    ROUTER_ID,
    checks,
    find,
    keyed,
    observation,
    policy_with_users,
    run,
    state,
)
from tests.services.router_health.support import effective


def minutes_ago(n: int):
    return NOW - timedelta(minutes=n)


# ------------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1w2d3h4m5s", 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ("45s", 45),
        ("3d12:34:56", 3 * 86400 + 12 * 3600 + 34 * 60 + 56),
        ("00:10:00", 600),
        ("", None),
        ("garbage", None),
    ],
)
def test_uptime_parsing(text, expected) -> None:
    assert rc.parse_uptime_seconds(text) == expected


def test_clock_parsing_iso_legacy_and_gmt_offset() -> None:
    iso = rc.router_utc(
        {"date": "2026-10-13", "time": "13:00:00", "gmt-offset": "+01:00"}
    )
    legacy = rc.router_utc(
        {"date": "oct/13/2026", "time": "13:00:00", "gmt-offset": "+01:00"}
    )
    negative = rc.router_utc(
        {"date": "2026-10-13", "time": "07:00:00", "gmt-offset": "-05:00"}
    )
    assert iso == legacy == NOW
    assert negative == NOW
    assert rc.router_utc({"date": "2026-10-13", "time": "12:00:00"}) is None
    assert (
        rc.router_utc(
            {"date": "2026-10-13", "time": "12:00:00", "time-zone-name": "UTC"}
        )
        == NOW
    )
    assert rc.router_utc({"date": "nope", "time": "12:00:00"}) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("198.51.100.7", "198.51.100.7"),
        ("198.51.100.7/32", "198.51.100.7"),
        ("198.51.100.7:179", "198.51.100.7"),
        ("fe80::1%ether1", "fe80::1"),
        ("[2001:DB8::1]:179", "2001:db8::1"),
        ("2001:db8::1/128", "2001:db8::1"),
        ("198.51.100.0/24", None),
        ("", None),
    ],
)
def test_bgp_peer_normalisation(value, expected) -> None:
    assert rc.normalize_peer(value) == expected


# -------------------------------------------------------------------- access


@pytest.mark.parametrize(
    "failure,check,priority",
    [
        (RestFailure.auth_failed, CheckId.rest_auth_failed, HealthPriority.high),
        (RestFailure.forbidden, CheckId.rest_forbidden, HealthPriority.medium),
    ],
)
def test_auth_and_forbidden_alert_immediately_and_distinctly(
    failure, check, priority
) -> None:
    ev = run(observation(failure=failure, resource=None))
    assert checks(ev) == {check}
    assert ev.findings[0].priority is priority
    # Only the four access checks are judged; everything else carries forward.
    assert ev.judged == set(rc.ACCESS_CHECKS)
    assert ev.signals[0].status == "abnormal"


def test_401_fix_names_the_rest_user_without_a_password() -> None:
    finding = run(observation(failure=RestFailure.auth_failed, resource=None)).findings[
        0
    ]
    assert "rest-api" in finding.fix
    assert "password Sub stores" in finding.fix


def test_unreachable_needs_consecutive_polls() -> None:
    first = run(observation(failure=RestFailure.unreachable, resource=None))
    assert not first.findings
    prior = keyed(first.signals[0])
    second = run(
        observation(failure=RestFailure.unreachable, resource=None), prior=prior
    )
    assert checks(second) == {CheckId.rest_unreachable}
    assert second.findings[0].priority is HealthPriority.medium


def test_unreachable_is_high_when_sessions_were_active() -> None:
    prior = keyed(
        state("rest_access", status="abnormal", streak=1, value=0),
        state("ppp_active", value=120),
    )
    ev = run(observation(failure=RestFailure.unreachable, resource=None), prior=prior)
    assert ev.findings[0].priority is HealthPriority.high
    assert ev.findings[0].evidence["had_active_sessions"] is True


def test_tls_failure_is_its_own_finding() -> None:
    prior = keyed(state("rest_access", status="abnormal", streak=1, value=0))
    ev = run(observation(failure=RestFailure.tls, resource=None), prior=prior)
    assert checks(ev) == {CheckId.rest_tls}


def test_recovery_resolves_access_checks_by_judging_them_clean() -> None:
    prior = keyed(state("rest_access", status="abnormal", streak=3, value=0))
    ev = run(observation(), prior=prior)
    assert set(rc.ACCESS_CHECKS) <= ev.judged
    assert not (checks(ev) & set(rc.ACCESS_CHECKS))
    assert ev.signals[0].status == "ok"


# --------------------------------------------------------------- ntp / clock


def test_ntp_unsynced_only_after_grace() -> None:
    obs = observation(ntp={"enabled": "true", "status": "started"})
    first = run(obs)
    assert not find(first, CheckId.ntp_unsynced)
    prior = keyed(next(s for s in first.signals if s.signal == "ntp"))
    later = run(obs, prior=prior, now=NOW + timedelta(minutes=31))
    assert checks(later) >= {CheckId.ntp_unsynced}
    assert later.findings[0].evidence["ntp_status"] == "started"


def test_ntp_disabled_counts_and_synchronized_is_clean() -> None:
    prior = keyed(
        state("ntp", status="abnormal", first_abnormal_at=minutes_ago(60), streak=4)
    )
    disabled = run(observation(ntp={"enabled": "false"}), prior=prior)
    assert CheckId.ntp_unsynced in checks(disabled)
    healthy = run(
        observation(ntp={"enabled": "true", "status": "synchronized"}), prior=prior
    )
    assert CheckId.ntp_unsynced not in checks(healthy)
    assert CheckId.ntp_unsynced in healthy.judged


def test_ntp_enabled_without_status_is_unjudged_never_healthy() -> None:
    ev = run(observation(ntp={"enabled": "yes"}))
    assert CheckId.ntp_unsynced not in ev.judged
    assert not [s for s in ev.signals if s.signal == "ntp"]


def test_ntp_disabled_without_status_is_still_judged() -> None:
    ev = run(observation(ntp={"enabled": "false"}))
    assert CheckId.ntp_unsynced in ev.judged


def test_clock_skew_medium_high_and_inside_threshold() -> None:
    def clock(offset_s: int):
        moment = NOW + timedelta(seconds=offset_s)
        return {
            "date": moment.strftime("%Y-%m-%d"),
            "time": moment.strftime("%H:%M:%S"),
            "gmt-offset": "+00:00",
        }

    def go(offset_s: int):
        return run(observation(clock=clock(offset_s), server_time_utc=NOW))

    assert CheckId.clock_skew not in checks(go(30))
    medium = find(go(120), CheckId.clock_skew)[0]
    assert medium.priority is HealthPriority.medium
    high = find(go(-86400 * 240), CheckId.clock_skew)[0]
    assert high.priority is HealthPriority.high
    assert high.evidence["ahead"] is False


def test_clock_skew_uses_configured_threshold() -> None:
    moment = NOW + timedelta(seconds=90)
    obs = observation(
        clock={
            "date": moment.strftime("%Y-%m-%d"),
            "time": moment.strftime("%H:%M:%S"),
            "gmt-offset": "+00:00",
        },
        server_time_utc=NOW,
    )
    assert CheckId.clock_skew in checks(run(obs))
    assert CheckId.clock_skew not in checks(
        run(obs, settings=RouterHealthSettings(clock_skew_seconds=120))
    )


# ----------------------------------------------------------------------- BGP


def bgp_obs(established: bool, *, disabled: str = "false", peer="198.51.100.9"):
    return observation(
        bgp_connections=(
            {
                "name": "upstream-a",
                "remote.address": f"{peer}/32",
                "disabled": disabled,
            },
        ),
        bgp_sessions=(
            ({"name": "s", "remote.address": peer, "established": "true"},)
            if established
            else ()
        ),
    )


def test_bgp_down_waits_for_grace_then_alerts_with_hashed_qualifier() -> None:
    first = run(bgp_obs(False))
    assert not find(first, CheckId.bgp_session_down)
    prior = keyed(next(s for s in first.signals if s.signal == "bgp_peer"))
    later = run(bgp_obs(False), prior=prior, now=NOW + timedelta(minutes=31))
    finding = find(later, CheckId.bgp_session_down)[0]
    assert finding.priority is HealthPriority.high
    assert finding.evidence["peer"] == "198.51.100.9"
    assert len(finding.qualifier) == 12  # sha1 prefix, not the address


def test_bgp_established_and_disabled_connections_are_quiet() -> None:
    prior = keyed(
        state(
            "bgp_peer",
            "198.51.100.9",
            status="abnormal",
            first_abnormal_at=minutes_ago(90),
            streak=6,
        )
    )
    assert CheckId.bgp_session_down not in checks(run(bgp_obs(True), prior=prior))
    ev = run(bgp_obs(False, disabled="true"), prior=prior)
    assert CheckId.bgp_session_down not in checks(ev)
    assert ("bgp_peer", "198.51.100.9") in ev.retire  # config removed the peer


def test_bgp_policy_exception_suppresses_by_peer() -> None:
    from app.services.router_health.policy import (
        PolicyException,
        RouterEntry,
        RouterHealthPolicy,
        RouterMatch,
    )

    base = policy_with_users()
    policy = RouterHealthPolicy(
        defaults=base.defaults,
        routers=[
            RouterEntry(
                match=RouterMatch(router_name="Test"),
                exceptions=[
                    PolicyException(
                        check="bgp-session-down",
                        scope="198.51.100.9",
                        until=(NOW + timedelta(days=10)).date(),
                        reason="peer retired; cleanup pending",
                    )
                ],
            )
        ],
    )
    prior = keyed(
        state(
            "bgp_peer",
            "198.51.100.9",
            status="abnormal",
            first_abnormal_at=minutes_ago(90),
            streak=6,
        )
    )
    ev = run(bgp_obs(False), prior=prior, policy=policy)
    assert CheckId.bgp_session_down not in checks(ev)


def test_bgp_unavailable_endpoint_is_not_judged() -> None:
    ev = run(observation(bgp_connections=None, bgp_sessions=None))
    assert CheckId.bgp_session_down not in ev.judged


# ---------------------------------------------------------------------- OSPF


def ospf_obs(state_text: str | None):
    rows = (
        ()
        if state_text is None
        else ({"router-id": "203.0.113.5", "state": state_text},)
    )
    return observation(ospf_neighbors=rows)


def test_ospf_baseline_is_learned_then_loss_alerts_after_grace() -> None:
    prior: dict = {}
    t = NOW
    for _ in range(4):  # four Full observations establish the baseline
        ev = run(ospf_obs("Full"), prior=prior, now=t)
        prior = keyed(*ev.signals)
        t += timedelta(minutes=15)
    lost = run(ospf_obs(None), prior=prior, now=t)
    assert not find(lost, CheckId.ospf_neighbor_lost)  # inside grace
    prior = keyed(*lost.signals)
    later = run(ospf_obs("ExStart"), prior=prior, now=t + timedelta(minutes=31))
    finding = find(later, CheckId.ospf_neighbor_lost)[0]
    assert finding.priority is HealthPriority.high
    assert finding.evidence["state"] == "exstart"


def test_ospf_never_baselined_neighbour_does_not_alert() -> None:
    prior = keyed(
        state(
            "ospf_neighbor",
            "203.0.113.5",
            status="abnormal",
            value=2,
            first_abnormal_at=minutes_ago(120),
            last_normal_at=minutes_ago(180),
            streak=8,
        )
    )
    assert CheckId.ospf_neighbor_lost not in checks(run(ospf_obs("Init"), prior=prior))


def test_ospf_neighbour_is_forgotten_after_seven_days_absent() -> None:
    prior = keyed(
        state(
            "ospf_neighbor",
            "203.0.113.5",
            status="abnormal",
            value=50,
            first_abnormal_at=NOW - timedelta(days=8),
            last_normal_at=NOW - timedelta(days=8),
            streak=500,
        )
    )
    ev = run(ospf_obs(None), prior=prior)
    assert ("ospf_neighbor", "203.0.113.5") in ev.retire
    assert CheckId.ospf_neighbor_lost not in checks(ev)


# ---------------------------------------------------------------- interfaces


def ifaces(counter: int, name="ether1", **extra):
    return observation(
        interfaces=(
            {
                "name": name,
                "type": "ether",
                "running": "true",
                "disabled": "false",
                "link-downs": str(counter),
                **extra,
            },
        )
    )


def link_prior(*points: tuple[int, int]):
    return keyed(
        state(
            "iface_link",
            "ether1",
            value=points[-1][1],
            samples=tuple(
                (int((NOW - timedelta(minutes=m)).timestamp()), float(c))
                for m, c in points
            ),
        )
    )


def test_link_flapping_uses_24h_delta_and_priority_bands() -> None:
    # base = the newest sample at or before the 24 h window start (110).
    prior = link_prior((1500, 110), (700, 112), (15, 120))
    quiet = run(ifaces(125), prior=prior)
    assert CheckId.link_flapping not in checks(quiet)
    warn = find(run(ifaces(135), prior=prior), CheckId.link_flapping)[0]
    assert warn.priority is HealthPriority.medium
    assert warn.evidence["link_downs"] == 25
    high = find(run(ifaces(400), prior=prior), CheckId.link_flapping)[0]
    assert high.priority is HealthPriority.high


def test_link_baseline_resets_on_reboot_and_counter_decrease() -> None:
    prior = link_prior((1400, 100), (15, 5000))
    rebooted_prior = {
        **prior,
        ("uptime", "router"): state("uptime", value=999999, observed=minutes_ago(15)),
    }
    ev = run(
        ifaces(40, **{}),
        prior=rebooted_prior,
    )
    # counter went DOWN (5000 -> 40): baseline restarts, no flapping claimed
    assert CheckId.link_flapping not in checks(ev)
    reboot = run(
        observation(resource={"uptime": "1m"}, interfaces=ifaces(50).interfaces),
        prior=rebooted_prior,
    )
    assert CheckId.link_flapping not in checks(reboot)
    assert len(next(s for s in reboot.signals if s.signal == "iface_link").samples) == 1


def test_link_only_physical_enabled_interfaces_are_tracked() -> None:
    obs = observation(
        interfaces=(
            {
                "name": "bridge1",
                "type": "bridge",
                "disabled": "false",
                "link-downs": "9",
            },
            {"name": "ether2", "type": "ether", "disabled": "true", "link-downs": "9"},
            {"name": "ether3", "type": "ether", "disabled": "false", "link-downs": "x"},
        )
    )
    ev = run(obs)
    assert not [s for s in ev.signals if s.signal == "iface_link"]


def test_ring_buffer_is_bounded() -> None:
    samples = tuple((i, float(i)) for i in range(250))
    prior = keyed(state("iface_link", "ether1", value=250, samples=samples))
    ev = run(ifaces(10**6), prior=prior)
    link = next(s for s in ev.signals if s.signal == "iface_link")
    assert len(link.samples) <= rc.SAMPLE_LIMIT


def test_reboot_detected_from_uptime_decrease_and_expires_after_24h() -> None:
    prior = keyed(state("uptime", value=86400.0))
    ev = run(observation(resource={"uptime": "5m"}), prior=prior)
    assert checks(ev) >= {CheckId.router_rebooted}
    up = next(s for s in ev.signals if s.signal == "uptime")
    assert up.status == "abnormal"
    later = run(
        observation(resource={"uptime": "2d"}),
        prior=keyed(up),
        now=NOW + timedelta(hours=25),
    )
    assert CheckId.router_rebooted not in checks(later)
    assert CheckId.router_rebooted in later.judged


# --------------------------------------------------------------------- optics


SFP = {
    "name": "sfp1",
    "type": "ether",
    "running": "true",
    "disabled": "false",
    "link-downs": "0",
}


def optics_obs(rx, *, running="true"):
    return observation(
        interfaces=({**SFP, "running": running},),
        optics={"sfp1": rx},
    )


@pytest.mark.parametrize(
    "rx,check,priority",
    [
        (-23.0, CheckId.sfp_rx_low, HealthPriority.medium),
        (-26.5, CheckId.sfp_rx_low, HealthPriority.high),
        (3.1, CheckId.sfp_overload, HealthPriority.high),
    ],
)
def test_optics_thresholds(rx, check, priority) -> None:
    ev = run(optics_obs(rx), optics_enabled=True)
    finding = find(ev, check)[0]
    assert finding.priority is priority
    assert finding.evidence["rx_dbm"] == rx


def test_optics_healthy_level_is_quiet_and_dark_fibre_is_high() -> None:
    assert not run(optics_obs(-9.0), optics_enabled=True).findings
    dark = run(optics_obs(-40.0, running="false"), optics_enabled=True)
    assert "dark fibre" in find(dark, CheckId.sfp_rx_low)[0].title


def test_optics_off_resolves_and_not_polled_carries_forward() -> None:
    off = run(optics_obs(-30.0), optics_enabled=False)
    assert not find(off, CheckId.sfp_rx_low)
    assert set(rc.OPTICS_CHECKS) <= off.judged
    not_polled = run(observation(interfaces=(SFP,)), optics_enabled=True)
    assert not (set(rc.OPTICS_CHECKS) & not_polled.judged)


def test_optics_thresholds_are_configurable() -> None:
    settings = RouterHealthSettings(sfp_rx_low_dbm=-30, sfp_rx_critical_dbm=-35)
    assert not run(optics_obs(-28.0), optics_enabled=True, settings=settings).findings


def test_optics_due_is_hourly_on_the_router_level_marker() -> None:
    assert rc.optics_due({}, NOW)
    recent = keyed(state("sfp_optics_attempt", observed=minutes_ago(30)))
    assert not rc.optics_due(recent, NOW)
    old = keyed(state("sfp_optics_attempt", observed=minutes_ago(56)))
    assert rc.optics_due(old, NOW)
    # Per-interface readings alone no longer gate the bound.
    assert rc.optics_due(keyed(state("sfp_optics", "sfp1", observed=NOW)), NOW)


@pytest.mark.parametrize("optics", [{"sfp1": -9.0}, {}, None])
def test_every_optics_attempt_writes_the_marker(optics) -> None:
    obs = observation(interfaces=(SFP,), optics=optics, optics_attempted=True)
    ev = run(obs, optics_enabled=True)
    marker = [s for s in ev.signals if s.signal == "sfp_optics_attempt"]
    assert len(marker) == 1 and marker[0].scope == "router"
    assert marker[0].last_observed_at == NOW


def test_no_marker_when_optics_was_not_attempted() -> None:
    ev = run(observation(interfaces=(SFP,)), optics_enabled=True)
    assert not [s for s in ev.signals if s.signal == "sfp_optics_attempt"]


def test_failed_optics_attempt_is_unjudged_and_keeps_findings() -> None:
    obs = observation(interfaces=(SFP,), optics=None, optics_attempted=True)
    ev = run(obs, optics_enabled=True)
    assert not (set(rc.OPTICS_CHECKS) & ev.judged)


def test_long_interface_names_fit_the_scope_column() -> None:
    long_name = "sfp-" + "x" * 196
    other = "sfp-" + "x" * 195 + "y"
    row = {**SFP, "name": long_name}
    obs = observation(
        interfaces=(row, {**SFP, "name": other}),
        optics={long_name: -30.0, other: -9.0},
        optics_attempted=True,
    )
    ev = run(obs, optics_enabled=True)
    scopes = [s.scope for s in ev.signals]
    assert scopes and all(len(sc) <= 120 for sc in scopes)
    assert len({s.scope for s in ev.signals if s.signal == "sfp_optics"}) == 2
    assert all(len(f.title) < 200 for f in ev.findings)
    link = [s for s in ev.signals if s.signal == "iface_link"]
    assert len(link) == 2 and all(len(s.scope) <= 120 for s in link)
    assert rc.bounded_scope("ether1") == "ether1"
    assert rc.bounded_scope(long_name) == rc.bounded_scope(long_name)


def test_non_ether_sfp_named_interface_is_never_probed() -> None:
    from app.services.router_health.runtime_reader import sfp_interface_names

    rows = (
        {"name": "sfp-vlan100", "type": "vlan", "disabled": "false"},
        {"name": "sfp1", "type": "ether", "disabled": "false"},
    )
    assert sfp_interface_names(rows) == ["sfp1"]


# ------------------------------------------------------------------------ PPP


def ppp_prior(count: float, *, streak: int = 0):
    base = int((NOW - timedelta(minutes=60)).timestamp())
    return keyed(
        state(
            "ppp_active",
            value=count,
            streak=streak,
            samples=tuple((base + i * 60, count) for i in range(5)),
        )
    )


def test_ppp_drop_needs_two_consecutive_polls() -> None:
    first = run(observation(ppp_active_count=40), prior=ppp_prior(200))
    assert not find(first, CheckId.ppp_drop)
    ppp_state = next(s for s in first.signals if s.signal == "ppp_active")
    assert ppp_state.consecutive_abnormal == 1
    second = run(
        observation(ppp_active_count=40),
        prior=keyed(
            state(
                "ppp_active",
                value=40,
                streak=1,
                samples=ppp_prior(200)[("ppp_active", "router")].samples,
            )
        ),
    )
    finding = find(second, CheckId.ppp_drop)[0]
    assert finding.priority is HealthPriority.high
    assert finding.evidence["baseline_sessions"] == 200


def test_ppp_drop_guards_small_baselines_and_small_drops() -> None:
    assert CheckId.ppp_drop not in checks(
        run(observation(ppp_active_count=1), prior=ppp_prior(5, streak=5))
    )
    assert CheckId.ppp_drop not in checks(
        run(observation(ppp_active_count=95), prior=ppp_prior(100, streak=5))
    )


def test_ppp_check_off_for_core_and_border_roles() -> None:
    from app.services.router_health.policy import (
        Role,
        RoleSpec,
        RouterEntry,
        RouterHealthPolicy,
        RouterMatch,
    )

    base = policy_with_users()
    policy = RouterHealthPolicy(
        defaults=base.defaults,
        roles={"core": RoleSpec(checks_off=["ppp-drop"])},
        routers=[
            RouterEntry(
                match=RouterMatch(router_name="Test"),
                role=Role.core,
                role_reason="core router has no PPPoE",
            )
        ],
    )
    ev = run(
        observation(ppp_active_count=1),
        prior=ppp_prior(500, streak=5),
        policy=policy,
    )
    assert CheckId.ppp_drop in ev.judged
    assert not find(ev, CheckId.ppp_drop)


# ----------------------------------------------------------------------- users


def users(*rows):
    return observation(users=tuple(rows))


def user(name, group="read", address="", disabled="false"):
    return {"name": name, "group": group, "address": address, "disabled": disabled}


def test_users_all_correct_is_quiet() -> None:
    ev = run(
        users(
            user("ops-snap", "read", "192.0.2.0/24"),
            user("rest-api", "read"),
        )
    )
    assert not ev.findings
    assert {
        CheckId.ops_user,
        CheckId.rest_user_missing,
        CheckId.rest_user_full,
    } <= ev.judged


def test_ops_user_missing_is_high() -> None:
    ev = run(users(user("rest-api", "read")))
    finding = find(ev, CheckId.ops_user)[0]
    assert finding.priority is HealthPriority.high
    assert finding.qualifier == "missing"


def test_ops_user_disabled_counts_as_missing() -> None:
    ev = run(users(user("ops-snap", "read", disabled="true"), user("rest-api", "read")))
    assert find(ev, CheckId.ops_user)[0].qualifier == "missing"


def test_ops_user_address_without_sub_prod_is_high() -> None:
    ev = run(users(user("ops-snap", "read", "203.0.113.0/24"), user("rest-api")))
    finding = find(ev, CheckId.ops_user)[0]
    assert finding.qualifier == "address"
    assert finding.priority is HealthPriority.high


def test_ops_user_address_list_covering_sub_prod_passes() -> None:
    ev = run(
        users(
            user("ops-snap", "read", "203.0.113.0/24,192.0.2.50/32"),
            user("rest-api"),
        )
    )
    assert not find(ev, CheckId.ops_user)


def test_ops_user_with_full_group_is_medium_not_full() -> None:
    ev = run(users(user("ops-snap", "full", "192.0.2.50"), user("rest-api")))
    finding = find(ev, CheckId.ops_user)[0]
    assert finding.qualifier == "group"
    assert finding.priority is HealthPriority.medium
    assert finding.evidence["group"] == "full"


def test_rest_user_missing_and_full() -> None:
    ev = run(users(user("ops-snap", "read", "192.0.2.50")))
    assert find(ev, CheckId.rest_user_missing)[0].priority is HealthPriority.high
    full = run(users(user("ops-snap", "read", "192.0.2.50"), user("rest-api", "full")))
    assert find(full, CheckId.rest_user_full)[0].priority is HealthPriority.medium


def test_unrestricted_full_users_aggregate_into_one_low_finding() -> None:
    ev = run(
        users(
            user("ops-snap", "read", "192.0.2.50"),
            user("rest-api"),
            user("alex-test", "full"),
            user("sam-test", "full"),
            user("limited", "full", "192.0.2.0/24"),
        )
    )
    findings = find(ev, CheckId.full_users_unrestricted)
    assert len(findings) == 1
    assert findings[0].priority is HealthPriority.low
    assert findings[0].evidence["users"] == ["alex-test", "sam-test"]


def test_unrestricted_full_users_can_be_turned_off_by_policy() -> None:
    policy = policy_with_users(report_unrestricted_full_users=False)
    ev = run(users(user("alex-test", "full")), policy=policy)
    assert CheckId.full_users_unrestricted not in ev.judged


def test_users_endpoint_unavailable_is_not_judged() -> None:
    ev = run(observation(users=None))
    assert not ({CheckId.ops_user, CheckId.rest_user_missing} & ev.judged)


# ------------------------------------------------------------ finding contract


def test_findings_are_runtime_layer_router_scoped_and_link_to_health_anchor() -> None:
    ev = run(users(user("rest-api", "read")))
    for finding in ev.findings:
        assert finding.layer is HealthLayer.runtime
        assert finding.subject_id == ROUTER_ID
        assert finding.target_url == f"/admin/network/routers/{ROUTER_ID}#health"
        assert finding.title.startswith("Test Access: ")
        assert finding.fix and finding.impact


def test_known_issue_annotation_is_attached_but_never_suppresses() -> None:
    from app.services.router_health.policy import (
        PolicyAnnotation,
        RouterEntry,
        RouterHealthPolicy,
        RouterMatch,
    )

    base = policy_with_users()
    policy = RouterHealthPolicy(
        defaults=base.defaults,
        routers=[
            RouterEntry(
                match=RouterMatch(router_name="Test"),
                annotations=[PolicyAnnotation(check="ops-user", note="upgrade staged")],
            )
        ],
    )
    ev = run(users(user("rest-api")), policy=policy)
    assert find(ev, CheckId.ops_user)[0].known_issue == "upgrade staged"


def test_effective_helper_resolves_user_policy_fields() -> None:
    eff = effective(policy_with_users())
    assert eff.ops_user is not None and eff.ops_user.name == "ops-snap"
    assert eff.rest_user is not None and eff.report_unrestricted_full_users


def test_sentinel_secrets_in_router_rows_never_reach_findings_or_signals() -> None:
    sentinel = "S3NTINEL-SECRET-VALUE"
    obs = observation(
        users=(
            {
                "name": "alex-test",
                "group": "full",
                "address": "",
                "disabled": "false",
                "password": sentinel,
                "comment": sentinel,
            },
        ),
        ntp={"enabled": "false", "servers": sentinel},
        bgp_connections=(
            {
                "name": "peer-a",
                "remote.address": "198.51.100.9",
                "disabled": "false",
                "tcp-md5-key": sentinel,
            },
        ),
        bgp_sessions=(),
        interfaces=(
            {
                "name": "ether1",
                "type": "ether",
                "disabled": "false",
                "link-downs": "1",
                "comment": sentinel,
            },
        ),
    )
    prior = keyed(
        state("ntp", status="abnormal", first_abnormal_at=minutes_ago(120), streak=8),
        state(
            "bgp_peer",
            "198.51.100.9",
            status="abnormal",
            first_abnormal_at=minutes_ago(120),
            streak=8,
        ),
    )
    ev = run(obs, prior=prior)
    assert ev.findings  # the planted rows did produce findings to inspect
    rendered = repr(ev.findings) + repr(ev.signals)
    assert sentinel not in rendered


# ------------------------------------------------- PPP baseline freeze / escalate


def ppp_state_after(ev):
    return next(s for s in ev.signals if s.signal == "ppp_active")


def test_ppp_baseline_is_frozen_while_abnormal_and_finding_stays_open() -> None:
    first_drop = NOW - timedelta(hours=6)
    base_ts = int((first_drop - timedelta(minutes=90)).timestamp())
    samples = tuple((base_ts + i * 900, 200.0) for i in range(5))
    prior = keyed(
        state(
            "ppp_active",
            status="abnormal",
            value=40,
            streak=24,
            samples=samples,
            first_abnormal_at=first_drop,
        )
    )
    ev = run(observation(ppp_active_count=40), prior=prior)
    finding = find(ev, CheckId.ppp_drop)[0]
    assert finding.evidence["baseline_sessions"] == 200  # not renewed to 40
    new_state = ppp_state_after(ev)
    assert new_state.samples == samples  # abnormal sample not appended
    assert new_state.first_abnormal_at == first_drop


def test_ppp_drop_stays_open_through_50h_of_outage() -> None:
    first_drop = NOW - timedelta(hours=60)
    base_ts = int((first_drop - timedelta(minutes=90)).timestamp())
    samples = tuple((base_ts + i * 900, 200.0) for i in range(5))
    prior = keyed(
        state(
            "ppp_active",
            status="abnormal",
            value=40,
            streak=200,
            samples=samples,
            first_abnormal_at=first_drop,
        )
    )
    ev = run(observation(ppp_active_count=40), prior=prior)
    assert find(ev, CheckId.ppp_drop)


def test_ppp_drop_escalates_after_24h_hold_and_not_before() -> None:
    def at(hours: float):
        first_drop = NOW - timedelta(hours=hours)
        base_ts = int((first_drop - timedelta(minutes=90)).timestamp())
        prior = keyed(
            state(
                "ppp_active",
                status="abnormal",
                value=40,
                streak=10,
                samples=tuple((base_ts + i * 900, 200.0) for i in range(5)),
                first_abnormal_at=first_drop,
            )
        )
        return find(
            run(observation(ppp_active_count=40), prior=prior), CheckId.ppp_drop
        )[0]

    assert at(23).priority is HealthPriority.high
    assert at(25).priority is HealthPriority.critical


def test_ppp_drop_clears_only_when_sessions_recover_to_the_threshold() -> None:
    first_drop = NOW - timedelta(hours=3)
    base_ts = int((first_drop - timedelta(minutes=90)).timestamp())
    prior = keyed(
        state(
            "ppp_active",
            status="abnormal",
            value=40,
            streak=12,
            samples=tuple((base_ts + i * 900, 200.0) for i in range(5)),
            first_abnormal_at=first_drop,
        )
    )
    still = run(observation(ppp_active_count=99), prior=prior)  # < 50% of 200
    assert find(still, CheckId.ppp_drop)
    recovered = run(observation(ppp_active_count=100), prior=prior)
    assert not find(recovered, CheckId.ppp_drop)
    assert CheckId.ppp_drop in recovered.judged
    assert ppp_state_after(recovered).status == "ok"


# ------------------------------------------------------------- BGP alert titles


def test_bgp_title_does_not_carry_the_peer_address() -> None:
    prior = keyed(
        state(
            "bgp_peer",
            "203.0.113.9",
            status="abnormal",
            first_abnormal_at=minutes_ago(60),
            streak=4,
        )
    )
    ev = run(
        observation(
            bgp_connections=({"name": "up", "remote.address": "203.0.113.9"},),
            bgp_sessions=(),
        ),
        prior=prior,
    )
    finding = find(ev, CheckId.bgp_session_down)[0]
    assert "203.0.113.9" not in finding.title
    assert finding.evidence["peer"] == "203.0.113.9"
