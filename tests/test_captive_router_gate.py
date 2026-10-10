"""Subscription -> NAS -> router mapping and the fail-closed readiness gate.

Fast SQLite unit lane; readiness comes from real snapshot exports.
"""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services import captive_router_gate as gate_module
from app.services.captive_router_gate import (
    CaptiveRouterGate,
    CaptiveRouterGateStatus,
    RouterUnresolvedCause,
    ServingNasSource,
)
from tests.captive_access_support import (
    nas_with_router,
    open_session,
    ready_network,
    serve_from,
)


def test_provisioning_assignment_on_ready_router_passes(db_session, subscription):
    ready_network(db_session)
    nas, router = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)

    decision = CaptiveRouterGate(db_session).decide(subscription)

    assert decision.status is CaptiveRouterGateStatus.ready
    assert decision.router_ids == (router.id,)
    assert decision.serving_nas[0].sources == (
        ServingNasSource.provisioning_assignment,
    )


def test_no_evidence_is_unresolved(db_session, subscription):
    decision = CaptiveRouterGate(db_session).decide(subscription)

    assert decision.status is CaptiveRouterGateStatus.router_unresolved
    assert decision.unresolved_causes == (RouterUnresolvedCause.no_serving_nas,)


def test_nas_without_active_router_is_unresolved(db_session, subscription):
    ready_network(db_session)
    lonely = NasDevice(name="lonely-nas")
    db_session.add(lonely)
    db_session.flush()
    serve_from(db_session, subscription, lonely)

    decision = CaptiveRouterGate(db_session).decide(subscription)

    assert decision.status is CaptiveRouterGateStatus.router_unresolved
    assert RouterUnresolvedCause.nas_without_active_router in (
        decision.unresolved_causes
    )


def test_inactive_router_does_not_count(db_session, subscription):
    ready_network(db_session)
    nas, router = nas_with_router(db_session)
    router.is_active = False
    serve_from(db_session, subscription, nas)

    assert (
        CaptiveRouterGate(db_session).decide(subscription).status
        is CaptiveRouterGateStatus.router_unresolved
    )


def test_ambiguous_session_ip_is_unresolved(db_session, subscription):
    ready_network(db_session)
    nas_with_router(db_session, nas_ip="198.51.100.9")
    nas_with_router(db_session, nas_ip="198.51.100.9")
    open_session(db_session, subscription, nas_ip="198.51.100.9")

    decision = CaptiveRouterGate(db_session).decide(subscription)

    assert decision.status is CaptiveRouterGateStatus.router_unresolved
    assert decision.unresolved_causes == (
        RouterUnresolvedCause.session_nas_unidentified,
    )


def test_unbound_session_of_the_account_is_serving_evidence(db_session, subscription):
    ready_network(db_session)
    nas, _ = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)
    other_nas, _ = nas_with_router(db_session, ready=False)
    session = open_session(db_session, subscription, nas=other_nas)
    session.subscription_id = None
    db_session.flush()

    decision = CaptiveRouterGate(db_session).decide(subscription)

    assert decision.status is CaptiveRouterGateStatus.router_not_ready
    assert len(decision.routers) == 2


def test_readiness_is_rendered_once_per_router_per_run(
    db_session, subscription, monkeypatch
):
    ready_network(db_session)
    nas, router = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)
    calls: list[frozenset] = []
    real = gate_module.resolve_routers_walled_garden_readiness

    def counting(db, *, query):
        calls.append(query.router_ids)
        return real(db, query=query)

    monkeypatch.setattr(
        gate_module, "resolve_routers_walled_garden_readiness", counting
    )
    gate = CaptiveRouterGate(db_session)
    gate.prefetch([subscription])
    for _ in range(3):
        assert gate.decide(subscription).is_ready

    assert calls == [frozenset({router.id})]


def test_prefetch_loads_session_evidence_in_one_pass(db_session, subscription):
    ready_network(db_session)
    nas, _ = nas_with_router(db_session)
    open_session(db_session, subscription, nas=nas)
    gate = CaptiveRouterGate(db_session)
    gate.prefetch([subscription])
    # Later rows are not seen: the gate is a per-run snapshot.
    db_session.query(RadiusActiveSession).delete()
    db_session.flush()

    decision = gate.decide(subscription)

    assert decision.serving_nas[0].sources == (ServingNasSource.active_session,)
