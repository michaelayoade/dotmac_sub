"""IPv4 retention policy: owner decision paths and the daily sweep."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.audit import AuditEvent
from app.models.domain_settings import SettingDomain
from app.models.event_store import EventStore
from app.models.network import IPAssignment
from app.models.radius_active_session import RadiusActiveSession
from app.services import ip_lifecycle
from app.services.ip_assignment_lifecycle import (
    IPv4AssignmentLifecycleError,
    IPv4AssignmentRepairDecision as Decision,
    RepairServiceIPv4AssignmentCommand,
    preview_service_ipv4_assignment_repair,
    repair_service_ipv4_assignment,
)
from app.services.owner_commands import CommandContext
from app.services.settings_spec import get_spec
from tests.test_ip_assignment_lifecycle import (
    _address,
    _assignment,
    _pool,
    _subscriber,
    _subscription,
)

ROOT = Path(__file__).resolve().parents[1]
S = SubscriptionStatus
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)
OLD = NOW - timedelta(days=45)
RECENT = NOW - timedelta(days=5)


def _age(db, subscription, *, updated_at, **cols) -> None:
    db.execute(
        update(Subscription)
        .where(Subscription.id == subscription.id)
        .values(updated_at=updated_at, **cols)
    )
    db.commit()
    db.expire_all()


def _held(db, offer, status, ip, *, updated_at=OLD, subscriber=None, **cols):
    subscriber = subscriber or _subscriber(db, "ret")
    subscription = _subscription(db, subscriber, offer, status=status, served_ip=ip)
    pool = _pool(db, uuid4().hex[:6])
    address = _address(db, pool, ip)
    assignment = _assignment(db, subscriber, address, subscription=subscription)
    db.commit()
    _age(db, subscription, updated_at=updated_at, **cols)
    return subscriber, subscription, assignment, address


def _session(db, ip: str) -> None:
    db.add(
        RadiusActiveSession(
            username=f"u-{uuid4().hex[:8]}",
            acct_session_id=uuid4().hex,
            framed_ip_address=ip,
            session_start=NOW,
        )
    )
    db.commit()


def _active(db, assignment) -> bool:
    db.expire_all()
    return bool(
        db.scalar(select(IPAssignment.is_active).where(IPAssignment.id == assignment.id))
    )


def _cmd(preview, sub_id, ids, cutoff, key="ret-1"):
    return RepairServiceIPv4AssignmentCommand(
        context=CommandContext.system(
            actor="test", scope="ip_assignment_lifecycle_repair",
            reason="retention test", idempotency_key=key,
        ),
        subscription_id=sub_id,
        desired_address_id=None,
        deactivate_assignment_ids=ids,
        preview_fingerprint=preview.fingerprint,
        retention_cutoff=cutoff,
    )


def _preview(db, sub, assignment, cutoff=CUTOFF):
    return preview_service_ipv4_assignment_repair(
        db,
        subscription_id=sub.id,
        desired_address_id=None,
        deactivate_assignment_ids=(assignment.id,),
        retention_cutoff=cutoff,
    )


# --- settings ---------------------------------------------------------------


def test_settings_defaults() -> None:
    days = get_spec(SettingDomain.network, "ipv4_retention_days_after_disable")
    mode = get_spec(SettingDomain.network, "ipv4_retention_release_mode")
    assert days is not None and days.default == 30
    assert mode is not None and mode.default == "shadow"
    assert mode.allowed == {"shadow", "apply"}
    cap = get_spec(SettingDomain.network, "ipv4_retention_max_per_run")
    assert cap is not None and cap.default > 0


# --- owner decision paths ---------------------------------------------------


@pytest.mark.parametrize("status", [S.disabled, S.hidden, S.archived])
def test_owner_releases_past_retention_and_is_idempotent(
    db_session, catalog_offer, status
) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, status, "10.30.1.10")
    sub_id, assignment_id = sub.id, assignment.id
    preview = _preview(db_session, sub, assignment)
    assert preview.decision is Decision.ready_release_retention_expired
    db_session.commit()
    cmd = _cmd(preview, sub_id, (assignment_id,), CUTOFF)
    first = repair_service_ipv4_assignment(db_session, cmd)
    second = repair_service_ipv4_assignment(db_session, cmd)
    assert first.deactivated_count == 1 and second.replayed is True
    assert not _active(db_session, assignment)
    db_session.refresh(sub)
    assert sub.ipv4_address is None  # served column cleared in the same command


def test_release_then_reactivate_serves_no_stale_framed_ip(
    db_session, catalog_offer
) -> None:
    from app.services.connection_type_provisioning import (
        build_radius_reply_attributes,
    )

    _, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.1.20")
    sub_id, assignment_id = sub.id, assignment.id
    preview = _preview(db_session, sub, assignment)
    db_session.commit()
    repair_service_ipv4_assignment(
        db_session, _cmd(preview, sub_id, (assignment_id,), CUTOFF, key="ret-react")
    )
    sub = db_session.get(Subscription, sub_id)
    sub.status = S.active  # reactivation
    db_session.commit()
    db_session.refresh(sub)
    assert sub.ipv4_address is None
    framed = [
        a["value"]
        for a in build_radius_reply_attributes(db_session, sub)
        if a["attribute"] == "Framed-IP-Address"
    ]
    assert "10.30.1.20" not in framed
    assert len(framed) == len(set(framed))


def test_owner_keeps_served_column_that_names_another_address(
    db_session, catalog_offer
) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.1.21")
    db_session.execute(
        update(Subscription).where(Subscription.id == sub.id).values(ipv4_address="10.30.1.99", updated_at=OLD)
    )
    sub_id, assignment_id = sub.id, assignment.id
    preview = _preview(db_session, sub, assignment)
    db_session.commit()
    repair_service_ipv4_assignment(
        db_session, _cmd(preview, sub_id, (assignment_id,), CUTOFF, key="ret-keep")
    )
    assert db_session.get(Subscription, sub_id).ipv4_address == "10.30.1.99"


def test_owner_refuses_recent_status(db_session, catalog_offer) -> None:
    _, sub, assignment, _ = _held(
        db_session, catalog_offer, S.disabled, "10.30.1.11", updated_at=RECENT
    )
    assert _preview(db_session, sub, assignment).decision is Decision.retention_not_expired


def test_owner_guard_unchanged_without_cutoff(db_session, catalog_offer) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.1.12")
    assert (
        _preview(db_session, sub, assignment, cutoff=None).decision
        is Decision.subscription_not_terminal
    )


@pytest.mark.parametrize("status", [S.active, S.blocked, S.suspended, S.pending, S.paused, S.stopped])
def test_owner_never_releases_retaining_statuses(db_session, catalog_offer, status) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, status, "10.30.1.13")
    assert (
        _preview(db_session, sub, assignment).decision
        is Decision.subscription_not_terminal
    )


def test_owner_refuses_live_session_and_apply_cannot_bypass(db_session, catalog_offer) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.1.14")
    sub_id, assignment_id = sub.id, assignment.id
    ready = _preview(db_session, sub, assignment)
    assert ready.decision is Decision.ready_release_retention_expired
    _session(db_session, "10.30.1.14")  # session appears after the review
    assert _preview(db_session, sub, assignment).decision is Decision.address_in_live_use
    db_session.rollback()
    with pytest.raises(IPv4AssignmentLifecycleError):
        repair_service_ipv4_assignment(
            db_session, _cmd(ready, sub_id, (assignment_id,), CUTOFF)
        )
    assert _active(db_session, assignment)


def test_owner_refuses_non_terminal_sibling_column_but_not_terminal(
    db_session, catalog_offer
) -> None:
    subscriber, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.1.15")
    other = _subscriber(db_session, "other")
    dead = _subscription(db_session, other, catalog_offer, status=S.canceled, served_ip="10.30.1.15")
    db_session.commit()
    assert _preview(db_session, sub, assignment).decision is Decision.ready_release_retention_expired
    live = _subscription(db_session, other, catalog_offer, status=S.active, served_ip="10.30.1.15")
    db_session.commit()
    assert _preview(db_session, sub, assignment).decision is Decision.address_in_live_use
    assert dead.id != live.id


def test_owner_refuses_management_address(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "mgmt")
    sub = _subscription(db_session, subscriber, catalog_offer, status=S.disabled)
    address = _address(db_session, _pool(db_session, "mg"), "10.30.1.16", allocation_type="management")
    assignment = _assignment(db_session, subscriber, address, subscription=sub)
    db_session.commit()
    _age(db_session, sub, updated_at=OLD)
    assert _preview(db_session, sub, assignment).decision is Decision.address_not_releasable


# --- sweep -------------------------------------------------------------------


def _sweep(db, mode, limit=100):
    return ip_lifecycle.run_ipv4_retention_release(
        db, mode=mode, retention_days=30, limit=limit, now=NOW
    )


def test_shadow_writes_nothing(db_session, catalog_offer) -> None:
    _, s1, a1, _ = _held(db_session, catalog_offer, S.disabled, "10.30.2.1")
    _, s2, a2, _ = _held(db_session, catalog_offer, S.canceled, "10.30.2.2")
    ids = (s1.id, s2.id)
    events = db_session.scalar(select(func.count()).select_from(EventStore))
    audits = db_session.scalar(select(func.count()).select_from(AuditEvent))
    result = _sweep(db_session, "shadow")
    assert result["counts"]["would_release"] == 2
    assert result["counts"]["released"] == 0
    assert _active(db_session, a1) and _active(db_session, a2)
    assert db_session.scalar(select(func.count()).select_from(EventStore)) == events
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == audits
    cols = dict(
        db_session.execute(
            select(Subscription.id, Subscription.ipv4_address).where(Subscription.id.in_(ids))
        ).all()
    )
    assert cols == {s1.id: "10.30.2.1", s2.id: "10.30.2.2"}


def test_apply_releases_only_past_retention_terminal(db_session, catalog_offer) -> None:
    _, _, old_dis, _ = _held(db_session, catalog_offer, S.disabled, "10.30.3.1")
    _, _, old_arc, _ = _held(db_session, catalog_offer, S.archived, "10.30.3.2")
    _, _, old_can, _ = _held(db_session, catalog_offer, S.canceled, "10.30.3.3")
    _, _, new_can, _ = _held(
        db_session, catalog_offer, S.canceled, "10.30.3.4", updated_at=RECENT, canceled_at=OLD
    )
    _, _, new_dis, _ = _held(db_session, catalog_offer, S.disabled, "10.30.3.5", updated_at=RECENT)
    result = _sweep(db_session, "apply")
    assert result["counts"]["would_release"] == 3
    assert result["counts"]["released"] == 3
    assert not any(_active(db_session, a) for a in (old_dis, old_arc, old_can))
    assert _active(db_session, new_can) and _active(db_session, new_dis)


@pytest.mark.parametrize("status", [S.active, S.blocked, S.suspended])
def test_apply_never_releases_active_blocked_suspended(db_session, catalog_offer, status) -> None:
    _, _, assignment, _ = _held(db_session, catalog_offer, status, "10.30.4.1")
    result = _sweep(db_session, "apply")
    assert result["examined"] == 0
    assert _active(db_session, assignment)


def test_sibling_rebind_is_reported_not_released(db_session, catalog_offer) -> None:
    subscriber, sub, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.5.1")
    sibling = _subscription(db_session, subscriber, catalog_offer, status=S.active, served_ip="10.30.5.1")
    db_session.commit()
    result = _sweep(db_session, "apply")
    assert result["counts"]["rebind_to_sibling"] == 1
    assert result["counts"]["released"] == 0
    assert _active(db_session, assignment)
    assert sibling.id


def test_live_session_conflict_is_skipped_for_retention_and_terminal(db_session, catalog_offer) -> None:
    _, _, dis, _ = _held(db_session, catalog_offer, S.disabled, "10.30.6.1")
    _, _, can, _ = _held(db_session, catalog_offer, S.canceled, "10.30.6.2")
    _session(db_session, "10.30.6.1")
    _session(db_session, "10.30.6.2")
    result = _sweep(db_session, "apply")
    assert result["counts"]["skipped_live_conflict"] == 2
    assert result["counts"]["released"] == 0
    assert _active(db_session, dis) and _active(db_session, can)


def test_other_subscribers_live_column_is_conflict_not_rebind(db_session, catalog_offer) -> None:
    _, _, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.7.1")
    stranger = _subscriber(db_session, "stranger")
    _subscription(db_session, stranger, catalog_offer, status=S.active, served_ip="10.30.7.1")
    db_session.commit()
    result = _sweep(db_session, "shadow")
    assert result["counts"]["skipped_live_conflict"] == 1
    assert result["counts"]["rebind_to_sibling"] == 0
    assert _active(db_session, assignment)


def test_run_size_is_bounded(db_session, catalog_offer) -> None:
    for i in range(3):
        _held(db_session, catalog_offer, S.disabled, f"10.30.8.{i + 1}")
    result = _sweep(db_session, "shadow", limit=2)
    assert result["examined"] == 2


def test_unknown_mode_is_refused(db_session) -> None:
    with pytest.raises(ValueError):
        _sweep(db_session, "yolo")


# --- architecture ------------------------------------------------------------


def test_sweep_writes_only_through_the_canonical_owner() -> None:
    source = (ROOT / "app/services/ip_lifecycle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_ipv4_retention_release"
    )
    body = ast.get_source_segment(source, func) or ""
    assert "repair_service_ipv4_assignment(" in body
    for forbidden in ("is_active =", ".ipv4_address =", "db.add(", "db.delete(", ".commit("):
        assert forbidden not in body
    # no other module drives the retention cutoff into the owner
    users = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "app").rglob("*.py")
        if "retention_cutoff" in p.read_text(encoding="utf-8")
    }
    assert users == {
        "app/services/ip_assignment_lifecycle.py",
        "app/services/ip_lifecycle.py",
    }


def test_task_only_calls_the_sweep_runner() -> None:
    source = (ROOT / "app/tasks/radius.py").read_text(encoding="utf-8")
    assert "run_ipv4_retention_release" in source
    assert "is_active" not in source


def test_canceled_release_clears_served_column_and_refuses_management(
    db_session, catalog_offer
) -> None:
    _, sub, assignment, _ = _held(db_session, catalog_offer, S.canceled, "10.30.9.1")
    mg = _subscriber(db_session, "mgc")
    msub = _subscription(db_session, mg, catalog_offer, status=S.canceled, served_ip="10.30.9.2")
    maddr = _address(db_session, _pool(db_session, "mgc"), "10.30.9.2", allocation_type="management")
    _assignment(db_session, mg, maddr, subscription=msub)
    db_session.commit()
    _age(db_session, msub, updated_at=OLD)
    sub_id = sub.id
    result = _sweep(db_session, "apply")
    assert result["counts"]["released"] == 1
    assert result["counts"]["skipped_other"] == 1
    assert not _active(db_session, assignment)
    assert db_session.get(Subscription, sub_id).ipv4_address is None


def test_report_has_uncapped_total_legacy_count_and_cursor_advances(
    db_session, catalog_offer
) -> None:
    for i in range(3):
        _held(db_session, catalog_offer, S.disabled, f"10.30.10.{i + 1}")
    legacy = _subscriber(db_session, "legacy")
    laddr = _address(db_session, _pool(db_session, "lg"), "10.30.10.50")
    _assignment(db_session, legacy, laddr)
    db_session.commit()
    first = _sweep(db_session, "shadow", limit=2)
    assert first["eligible_total"] == 3 and first["examined"] == 2
    assert first["legacy_unlinked_active_v4_assignments"] == 1
    assert first["next_cursor"] is not None
    second = ip_lifecycle.run_ipv4_retention_release(
        db_session, mode="shadow", retention_days=30, limit=2, now=NOW,
        after=tuple(first["next_cursor"]),
    )
    assert second["examined"] == 1 and second["next_cursor"] is None
    assert set(first["samples"]["would_release"]).isdisjoint(second["samples"]["would_release"])


def test_post_commit_conflict_counted_without_failing_the_release(
    db_session, catalog_offer, monkeypatch
) -> None:
    from app.services import ip_assignment_lifecycle as owner

    _, _, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.11.1")
    real = owner.find_ipv4_live_use

    def after_release_in_use(db, *, subscription_id, addresses):
        if not _active(db, assignment):
            return owner.IPv4LiveUseEvidence(True, ())
        return real(db, subscription_id=subscription_id, addresses=addresses)

    monkeypatch.setattr(owner, "find_ipv4_live_use", after_release_in_use)
    result = _sweep(db_session, "apply")
    assert result["counts"]["post_commit_conflict"] == 1
    assert result["counts"]["released"] == 1
    assert result["counts"]["apply_failed"] == 0


def test_raising_post_commit_recheck_is_not_apply_failed(
    db_session, catalog_offer, monkeypatch
) -> None:
    from app.services import ip_assignment_lifecycle as owner

    _, _, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.11.2")
    real = owner.find_ipv4_live_use

    def boom(db, *, subscription_id, addresses):
        if not _active(db, assignment):
            raise RuntimeError("redis/db hiccup")
        return real(db, subscription_id=subscription_id, addresses=addresses)

    monkeypatch.setattr(owner, "find_ipv4_live_use", boom)
    result = _sweep(db_session, "apply")
    assert result["counts"]["released"] == 1
    assert result["counts"]["apply_failed"] == 0


def test_address_lock_unavailable_is_skipped_locked(
    db_session, catalog_offer, monkeypatch
) -> None:
    from app.services import ip_assignment_lifecycle as owner

    _, _, assignment, _ = _held(db_session, catalog_offer, S.disabled, "10.30.11.3")

    def locked(db, command):
        raise IPv4AssignmentLifecycleError(
            code="network.ip_assignment_lifecycle.address_lock_unavailable",
            message="locked",
        )

    monkeypatch.setattr(owner, "repair_service_ipv4_assignment", locked)
    result = _sweep(db_session, "apply")
    assert result["counts"]["skipped_locked"] == 1
    assert result["counts"]["apply_failed"] == 0
    assert _active(db_session, assignment)


@pytest.mark.parametrize("raw", ['["x"]', "{}", "not json", '["2026-01-01T00:00:00+00:00","nope"]', None])
def test_malformed_cursor_resets(raw) -> None:
    assert ip_lifecycle.parse_retention_cursor(raw) is None


def test_valid_cursor_round_trips() -> None:
    uid = str(uuid4())
    assert ip_lifecycle.parse_retention_cursor(
        f'["2026-01-01T00:00:00+00:00","{uid}"]'
    ) == ("2026-01-01T00:00:00+00:00", uid)


def test_post_commit_conflict_alert_rule_exists() -> None:
    text = (ROOT / "deploy/observability/ipv4_retention.rules.yml").read_text()
    assert 'ipv4_retention_release{class="post_commit_conflict"} > 0' in text
