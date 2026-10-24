"""The governed switch for the single rate owner (``access.rate_profile_switch``)."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.services import access_rate_profile as owner
from app.services import access_rate_switch as switch
from app.services import radius_population
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from tests.test_access_rate_profile import _offer, _profile, _service

TARGET = {"target_name": "t", "target_fingerprint": "f", "db_url": "x"}


def _context(reason: str = "approved switch") -> CommandContext:
    return CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor="service:test",
        scope=switch.SCOPE,
        reason=reason,
    )


class World:
    """A fake projection target: what ``populate`` writes and what is read back."""

    def __init__(self, db, monkeypatch):
        self.db = db
        self.locks = 0
        self.full = 0  # full non-dry projections
        self.scoped: list[set[str]] = []
        self.dry = 0
        self.modes: dict[str, str] = {}  # login -> mode override
        self.test_logins: set[str] = set()
        self.wrong: set[str] = set()  # logins observed with a wrong rate
        self.wrong_test: set[str] = set()
        self.heal_on_scoped = True
        self.owner_flag: list[bool | None] = []  # scripted rate_owner_enabled
        self.fail_on: set[int] = set()  # populate call numbers that raise
        self.calls = 0
        monkeypatch.setattr(switch, "_take_lock", self._lock)
        monkeypatch.setattr(
            switch,
            "active_external_radius_targets",
            lambda db, capability=None: [TARGET],
        )
        monkeypatch.setattr(radius_population, "observed_rate_limits", self._observe)
        monkeypatch.setattr(switch, "_invalidate_switch_cache", self._invalidate)
        self.invalidations = 0
        clock = {"t": 0.0}
        monkeypatch.setattr(switch, "_monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            switch, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
        )

    def _lock(self, db):
        self.locks += 1

    def _invalidate(self):
        self.invalidations += 1
        return True

    def plan(self, only=None):
        rates = owner.derived_rates(self.db)
        plan = {}
        for login, rate in rates.items():
            if only is not None and login not in only:
                continue
            mode = self.modes.get(login, "active")
            plan[login] = {
                "mode": mode,
                "rate": rate,
                "test_access": login in self.test_logins,
                "test_rate": rate if login in self.test_logins else None,
            }
        return plan

    def populate(self, dry_run=True, only_usernames=None, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise radius_population.RadiusProjectionIncomplete(
                [{"target_name": "t", "target_fingerprint": "f", "ok": False}]
            )
        if dry_run:
            self.dry += 1
        elif only_usernames is not None:
            self.scoped.append(set(only_usernames))
            if self.heal_on_scoped:
                self.wrong -= set(only_usernames)
        else:
            self.full += 1
        flag = (
            self.owner_flag.pop(0)
            if self.owner_flag
            else owner.rate_owner_enabled(self.db)
        )
        return {
            "projection_complete": True,
            "rate_owner_enabled": flag,
            "projection_plan": self.plan(only_usernames),
        }

    def _observe(self, target, usernames):
        expected = owner.derived_rates(self.db)
        rates, test_rates = {}, {}
        for login, rate in expected.items():
            if login in self.wrong:
                rates[login] = ["1k/1k"]
            elif rate:
                rates[login] = [rate]
            if login in self.test_logins:
                test_rates[login] = ["1k/1k"] if login in self.wrong_test else [rate]
        return rates, test_rates


@pytest.fixture()
def world(db_session, monkeypatch):
    return World(db_session, monkeypatch)


def _clean_service(db):
    ten = _profile(db, "Ten", rate="10M/10M")
    offer = _offer(db, "A", profile=ten)
    sub, credential = _service(db, offer, sub_profile=ten)
    db.commit()
    return ten, offer, sub, credential


def _actions(db):
    return list(db.scalars(select(AuditEvent.action).order_by(AuditEvent.occurred_at)))


def _on(db, world, **kwargs):
    return switch.switch_on(
        db, context=_context(), apply=True, populate=world.populate, **kwargs
    )


# ------------------------------------------------------------ basic flow


def test_dry_run_reports_and_changes_nothing(db_session, world):
    ten, offer, *_ = _clean_service(db_session)
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    _service(db_session, offer, sub_profile=ten, slot=fifteen)
    db_session.commit()

    outcome = switch.switch_on(db_session, context=_context(), populate=world.populate)

    assert outcome.applied is False
    assert outcome.divergences == 1
    assert outcome.by_class == {"credential_wins": 1}
    assert outcome.baseline.clean
    assert owner.rate_owner_enabled(db_session) is False
    assert world.locks == 0 and world.full == 0 and world.dry == 1


def test_any_divergence_refuses_the_flip(db_session, world):
    ten, offer, *_ = _clean_service(db_session)
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    _service(db_session, offer, sub_profile=ten, slot=fifteen)
    db_session.commit()

    with pytest.raises(DomainError) as refused:
        _on(db_session, world)

    assert refused.value.code == "access.rate_profile_switch.preflight_divergence"
    assert refused.value.details["divergences"] == 1
    assert owner.rate_owner_enabled(db_session) is False
    assert world.locks == 1 and world.full == 0
    assert "access.rate_profile_switch.on" not in _actions(db_session)


def test_switch_on_locks_flips_audits_projects_and_reads_back(db_session, world):
    _clean_service(db_session)

    outcome = _on(db_session, world)

    assert outcome.applied and outcome.enabled
    assert outcome.read_back.mismatches == 0
    assert outcome.read_back.compared == 1
    assert owner.rate_owner_enabled(db_session) is True
    assert world.locks == 1 and world.full == 1
    assert world.invalidations >= 1
    assert "access.rate_profile_switch.on" in _actions(db_session)


def test_switching_on_twice_is_refused(db_session, world):
    _clean_service(db_session)
    _on(db_session, world)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world)

    assert refused.value.code == "access.rate_profile_switch.already_enabled"


def test_switch_off_is_refused_in_this_release(db_session, world):
    """No legacy precedence remains: turning the flag off would only stop a
    production process from starting. Rolling back is a redeploy."""
    _clean_service(db_session)
    _on(db_session, world)

    for apply in (False, True):
        with pytest.raises(DomainError) as refused:
            switch.switch_off(
                db_session,
                context=_context("rollback"),
                apply=apply,
                populate=world.populate,
            )
        assert refused.value.code == "access.rate_profile_switch.switch_off_unavailable"
    assert owner.rate_owner_enabled(db_session) is True
    assert "access.rate_profile_switch.off" not in _actions(db_session)


def test_the_switch_is_still_closed_to_everything_else(db_session):
    from fastapi import HTTPException

    from app.models.domain_settings import SettingDomain
    from app.services.domain_settings import DomainSettings

    with pytest.raises(HTTPException):
        DomainSettings(SettingDomain.radius)._validate_relationship_change(
            db_session, SettingDomain.radius, "rate_owner_enabled", True
        )


# ------------------------------------------------ B1: what was written


def test_b1_only_written_logins_are_compared(db_session, world):
    """Preserved (absent), mode-none and hard-reject logins are not mismatches."""
    ten, offer, sub, _ = _clean_service(db_session)
    none_sub, _ = _service(db_session, offer, sub_profile=ten)
    reject_sub, _ = _service(db_session, offer, sub_profile=ten)
    preserved_sub, _ = _service(db_session, offer, sub_profile=ten)
    db_session.commit()
    world.modes = {none_sub.login: "none", reject_sub.login: "reject"}
    world.wrong = {none_sub.login, reject_sub.login, preserved_sub.login}
    original = world.plan

    def without_preserved(only=None):
        plan = original(only)
        plan.pop(preserved_sub.login, None)
        return plan

    world.plan = without_preserved

    outcome = _on(db_session, world)

    assert outcome.read_back.compared == 1
    assert outcome.read_back.not_written == 3
    assert outcome.read_back.mismatches == 0


def test_b1_test_access_is_compared_against_the_plan_rate(db_session, world):
    _, _, sub, _ = _clean_service(db_session)
    world.test_logins = {sub.login}
    world.heal_on_scoped = False
    original = world.populate

    def drift(dry_run=True, only_usernames=None, **kwargs):
        result = original(dry_run=dry_run, only_usernames=only_usernames, **kwargs)
        if not dry_run:
            world.wrong_test = {sub.login}  # only the test row is wrong
        return result

    world.populate = drift

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.read_back_mismatch"
    assert failed.value.details["flag"] is True  # reported, never switched off


def test_b1_baseline_must_be_clean_before_apply(db_session, world):
    _, _, sub, _ = _clean_service(db_session)
    world.wrong = {sub.login}  # radreply already disagrees with the projection

    dry = switch.switch_on(db_session, context=_context(), populate=world.populate)
    assert dry.baseline.clean is False
    assert dry.baseline.read_back.mismatches == 1

    with pytest.raises(DomainError) as refused:
        _on(db_session, world)

    assert refused.value.code == "access.rate_profile_switch.baseline_not_clean"
    assert owner.rate_owner_enabled(db_session) is False
    assert world.locks == 0 and world.full == 0


def test_b1_an_incomplete_dry_run_projection_is_not_clean(db_session, world):
    _clean_service(db_session)
    world.fail_on = {1}  # the baseline dry run

    dry = switch.switch_on(db_session, context=_context(), populate=world.populate)

    assert dry.baseline.clean is False
    assert dry.baseline.error == "RadiusProjectionIncomplete"


# ------------------------------------------------ B2: the owner really ran


def test_b2_waits_for_the_flag_and_requires_it_in_the_result(db_session, world):
    _clean_service(db_session)

    outcome = _on(db_session, world)

    assert outcome.enabled
    assert world.invalidations == 1


def test_b2_retries_once_when_the_projection_ran_in_legacy_mode(db_session, world):
    _clean_service(db_session)
    world.owner_flag = [False, False, True]  # baseline dry run, first, second

    outcome = _on(db_session, world)

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert world.full == 2
    assert world.invalidations == 2


def test_b2_gives_up_and_reports_when_the_owner_never_runs(db_session, world):
    _clean_service(db_session)
    world.owner_flag = [False, False, False]

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.owner_not_active"
    assert failed.value.details["flag"] is True
    assert "auto_switched_off" not in failed.value.details


def test_b2_a_flag_that_never_becomes_visible_is_reported(
    db_session, world, monkeypatch
):
    _clean_service(db_session)
    monkeypatch.setattr(owner, "rate_owner_enabled", lambda db: False)  # stale
    start = world.invalidations

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.owner_not_active"
    assert world.invalidations > start
    assert world.full == 0  # nothing was projected on a flag it could not see


# ------------------------------------------------ B3: robust failure paths


def test_b3_a_mismatch_is_reprojected_once_before_giving_up(db_session, world):
    _, _, sub, _ = _clean_service(db_session)
    world.wrong = {sub.login}
    # The baseline must be clean; the drift appears after the full projection.
    world.wrong = set()
    original = world.populate

    def churn(dry_run=True, only_usernames=None, **kwargs):
        result = original(dry_run=dry_run, only_usernames=only_usernames, **kwargs)
        if not dry_run and only_usernames is None:
            world.wrong = {sub.login}
        return result

    world.populate = churn

    outcome = _on(db_session, world)

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert world.scoped == [{sub.login}]


def test_b3_a_persistent_mismatch_is_reported_after_one_retry(db_session, world):
    _, _, sub, _ = _clean_service(db_session)
    original = world.populate

    def drift(dry_run=True, only_usernames=None, **kwargs):
        result = original(dry_run=dry_run, only_usernames=only_usernames, **kwargs)
        if not dry_run:
            world.wrong = {sub.login}
        return result

    world.populate = drift

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.read_back_mismatch"
    assert failed.value.details["mismatches"] == 1
    assert failed.value.details["flag"] is True
    assert len(world.scoped) == 1
    # Nothing is switched off automatically: there is no legacy path to return to.
    assert "access.rate_profile_switch.off" not in _actions(db_session)


def test_b3_an_incomplete_projection_is_reported_with_the_final_state(
    db_session, world
):
    _clean_service(db_session)
    world.fail_on = {2}  # baseline is call 1; the first real projection is call 2

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.projection_failed"
    assert failed.value.details["flag"] is True


def test_b3_the_cli_prints_a_structured_final_state_on_any_exception(
    db_session, monkeypatch, capsys
):
    def boom(*args, trace=None, **kwargs):
        trace["projection_complete"] = False
        trace["mismatches"] = 3
        raise RuntimeError("unexpected")

    monkeypatch.setattr(switch, "switch_on", boom)
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    assert switch.main(["on", "--reason", "check", "--apply"]) == 1

    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "RuntimeError"
    assert out["final_state"] == {
        "flag": None,
        "projection_complete": False,
        "mismatches": 3,
    }


def test_cli_is_dry_run_unless_applied(db_session, world, monkeypatch, capsys):
    _clean_service(db_session)
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(radius_population, "populate", world.populate)

    assert switch.main(["on", "--reason", "check"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["divergences"] == 0
    assert out["baseline"]["clean"] is True
    assert owner.rate_owner_enabled(db_session) is False
    with pytest.raises(SystemExit):
        switch.main(["on"])  # a reason is required


# ------------------------------------- B4: the exception record


def _divergent(db):
    """A clean service plus one residue service; returns the residue's key."""
    ten, offer, *_ = _clean_service(db)
    fifteen = _profile(db, "Fifteen", rate="15M/15M")
    residue, _ = _service(db, offer, sub_profile=ten, slot=fifteen)
    db.commit()
    return (str(residue.id), "credential_wins", "15M/15M", "10M/10M")


def _entry(sid, klass="credential_wins", current="15M/15M", derived="10M/10M", **kw):
    entry = {
        "subscription_id": sid,
        "login": "login",
        "offer_id": str(uuid4()),
        "current_rate": current,
        "derived_rate": derived,
        "divergence_class": klass,
        "decision": "apply_derived",
        "decided_by": "reviewer",
        "decided_at": "2026-10-23T12:00:00Z",
        "reason": "package rate",
    }
    entry.update(kw)
    return entry


def _record(tmp_path, entries, schema=switch.RECORD_SCHEMA, name="record.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"schema": schema, "entries": entries}))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _decide(path, **fields):
    """Fill in the blank decisions of an emitted record; return its digest."""
    document = json.loads(path.read_text())
    for entry in document["entries"]:
        entry.update(
            decision="apply_derived",
            decided_by="reviewer",
            decided_at="2026-10-23T12:00:00Z",
            reason="package rate",
        )
        entry.update(fields)
    path.write_text(json.dumps(document))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_round_trip_emit_decide_switch(db_session, world, tmp_path):
    key = _divergent(db_session)
    record = tmp_path / "record.json"
    dry = switch.switch_on(
        db_session,
        context=_context(),
        populate=world.populate,
        emit_approval_list_to=record,
    )
    assert dry.emitted_list_sha256 == hashlib.sha256(record.read_bytes()).hexdigest()
    document = json.loads(record.read_text())
    assert document["schema"] == "access_rate_exception_record.v1"
    (entry,) = document["entries"]
    assert (
        entry["subscription_id"],
        entry["divergence_class"],
        entry["current_rate"],
        entry["derived_rate"],
    ) == key
    assert entry["login"] and entry["offer_id"]
    assert entry["decision"] is None and entry["decided_by"] is None

    # An undecided record cannot be used.
    with pytest.raises(DomainError) as undecided:
        switch.load_approved(record, dry.emitted_list_sha256)
    assert undecided.value.code == "access.rate_profile_switch.approved_list_invalid"
    assert undecided.value.details["undecided"] == 1

    digest = _decide(record)
    approved = switch.load_approved(record, digest)
    outcome = _on(db_session, world, approved=approved)

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert outcome.approved_list_sha256 == digest
    assert outcome.switch_digest == approved.digest
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "access.rate_profile_switch.on")
    ).one()
    assert event.metadata_["approved_list_sha256"] == digest
    assert event.metadata_["switch_digest"] == approved.digest
    assert event.metadata_["record_schema"] == switch.RECORD_SCHEMA
    assert event.metadata_["apply_derived"] == 1


def test_decisions_are_validated(tmp_path):
    sid = str(uuid4())
    for entry, field in (
        (_entry(sid, decision=None), "undecided"),
        (_entry(sid, decision=""), "undecided"),
        (_entry(sid, decision="keep_override"), "decision"),
        (_entry(sid, decided_by=""), "field"),
        (_entry(sid, decided_at=None), "field"),
    ):
        path, digest = _record(tmp_path, [entry])
        with pytest.raises(DomainError) as invalid:
            switch.load_approved(path, digest)
        assert invalid.value.code == "access.rate_profile_switch.approved_list_invalid"
        assert field in invalid.value.details


def test_schema_and_classes_are_validated(tmp_path):
    sid = str(uuid4())
    path, digest = _record(tmp_path, [_entry(sid)], schema="other.v1")
    with pytest.raises(DomainError) as wrong_schema:
        switch.load_approved(path, digest)
    assert wrong_schema.value.details["schema"] == "other.v1"

    path, digest = _record(tmp_path, [_entry(sid, klass="made_up")])
    with pytest.raises(DomainError) as unknown:
        switch.load_approved(path, digest)
    assert unknown.value.details["divergence_class"] == "made_up"

    path, digest = _record(tmp_path, [_entry(sid), _entry(sid)])
    with pytest.raises(DomainError):
        switch.load_approved(path, digest)

    entry = _entry(sid)
    del entry["login"]
    path, digest = _record(tmp_path, [entry])
    with pytest.raises(DomainError):
        switch.load_approved(path, digest)

    path.write_text("not json")
    with pytest.raises(DomainError):
        switch.load_approved(path, hashlib.sha256(path.read_bytes()).hexdigest())

    with pytest.raises(DomainError) as mismatch:
        switch.load_approved(path, "0" * 64)
    assert (
        mismatch.value.code
        == "access.rate_profile_switch.approved_list_digest_mismatch"
    )


def _moved_service(
    db, *, slot=None, applied_at="2026-10-22T09:00:00+00:00", applied=True
):
    """A service moved from package A to package B by a plan change."""
    from datetime import date, datetime

    from app.models.subscription_change import (
        SubscriptionChangeRequest,
        SubscriptionChangeStatus,
    )

    old_profile = _profile(db, "Old", rate="8M/8M", code="old")
    new_profile = _profile(db, "New", rate="10M/10M", code="new")
    old_offer = _offer(db, "Old", profile=old_profile)
    new_offer = _offer(db, "New", profile=new_profile)
    sub, _ = _service(db, new_offer, sub_profile=new_profile, slot=slot)
    if applied:
        db.add(
            SubscriptionChangeRequest(
                subscription_id=sub.id,
                current_offer_id=old_offer.id,
                requested_offer_id=new_offer.id,
                effective_date=date(2026, 10, 1),
                status=SubscriptionChangeStatus.applied,
                applied_at=datetime.fromisoformat(applied_at),
            )
        )
    db.commit()
    return sub, old_offer, new_offer


def _moved_entry(sub, pre_move_offer, **kw):
    return _entry(
        str(sub.id),
        decision="moved_offer",
        offer_id=str(pre_move_offer.id),
        decided_at="2026-10-23T12:00:00Z",
        **kw,
    )


def test_a_verified_moved_offer_entry_is_audit_only(db_session, world, tmp_path):
    sid, *_ = _divergent(db_session)
    sub, old_offer, _new = _moved_service(db_session)
    path, digest = _record(tmp_path, [_entry(sid), _moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)
    assert len(approved.entries) == 1 and approved.audit_only == 1
    assert switch.unverified_moves(db_session, approved) == []

    outcome = _on(db_session, world, approved=approved)

    assert outcome.enabled and outcome.moved_offer == 1 and outcome.apply_derived == 1


def test_s2_a_move_must_have_changed_the_offer(db_session, world, tmp_path):
    sub, _old, new_offer = _moved_service(db_session)
    path, digest = _record(tmp_path, [_moved_entry(sub, new_offer)])  # not pre-move
    approved = switch.load_approved(path, digest)

    assert switch.unverified_moves(db_session, approved) == [str(sub.id)]
    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)
    assert refused.value.code == "access.rate_profile_switch.moved_offer_unverified"
    assert owner.rate_owner_enabled(db_session) is False


def test_s2_a_move_needs_an_applied_change_request(db_session, world, tmp_path):
    sub, old_offer, _new = _moved_service(db_session, applied=False)
    path, digest = _record(tmp_path, [_moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)
    assert refused.value.code == "access.rate_profile_switch.moved_offer_unverified"


def test_s2_the_change_must_predate_the_decision(db_session, world, tmp_path):
    sub, old_offer, _new = _moved_service(
        db_session, applied_at="2026-10-24T09:00:00+00:00"
    )
    path, digest = _record(tmp_path, [_moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)
    assert refused.value.code == "access.rate_profile_switch.moved_offer_unverified"
    dry = switch.switch_on(
        db_session, context=_context(), populate=world.populate, approved=approved
    )
    assert dry.unverified_moves == 1


def test_a_moved_service_that_still_diverges_is_refused(db_session, world, tmp_path):
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M", code="fifteen")
    sub, old_offer, _new = _moved_service(db_session, slot=fifteen)
    path, digest = _record(tmp_path, [_moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)
    assert refused.value.code == "access.rate_profile_switch.preflight_divergence"
    assert refused.value.details["unexpected"] == 1


def test_a_live_divergence_missing_from_the_record_refuses(db_session, world, tmp_path):
    _divergent(db_session)
    path, digest = _record(tmp_path, [])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)

    assert refused.value.details["unexpected"] == 1
    assert owner.rate_owner_enabled(db_session) is False


def test_a_recorded_change_that_no_longer_diverges_refuses(db_session, world, tmp_path):
    _clean_service(db_session)
    path, digest = _record(tmp_path, [_entry(str(uuid4()))])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)

    assert refused.value.details["missing"] == 1


def test_a_class_or_rate_mismatch_refuses(db_session, world, tmp_path):
    sid, klass, current, _derived = _divergent(db_session)
    for entry in (
        _entry(sid, derived="12M/12M"),
        _entry(sid, klass="subscription_differs"),
    ):
        path, digest = _record(tmp_path, [entry], name=f"{uuid4().hex}.json")
        approved = switch.load_approved(path, digest)
        with pytest.raises(DomainError) as refused:
            _on(db_session, world, approved=approved)
        assert refused.value.details["unexpected"] == 1
        assert refused.value.details["missing"] == 1


def test_cli_emits_then_takes_the_decided_record(
    db_session, world, monkeypatch, capsys, tmp_path
):
    _divergent(db_session)
    record = tmp_path / "record.json"
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(radius_population, "populate", world.populate)

    assert (
        switch.main(["on", "--reason", "check", "--emit-approval-list", str(record)])
        == 0
    )
    capsys.readouterr()
    digest = _decide(record)

    code = switch.main(
        [
            "on",
            "--reason",
            "check",
            "--approved-list",
            str(record),
            "--approved-sha256",
            digest,
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (out["unexpected"], out["missing"]) == (0, 0)
    assert out["approved_list_sha256"] == digest
    with pytest.raises(SystemExit):
        switch.main(["on", "--reason", "x", "--apply", "--emit-approval-list", "f"])


# ------------------------------------- the projection evidence is real


from tests.test_access_rate_profile import _radius_env  # noqa: E402,F401


@pytest.mark.usefixtures("_radius_env")
def test_populate_reports_its_plan_and_whether_the_owner_ran(db_session, monkeypatch):
    _clean_service(db_session)
    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: True)

    result = radius_population.populate(dry_run=True, include_projection_plan=True)

    assert result["rate_owner_enabled"] is True
    (item,) = result["projection_plan"].values()
    assert item == {
        "mode": "active",
        "rate": "10M/10M",
        "test_access": False,
        "test_rate": None,
    }
    plain = radius_population.populate(dry_run=True)
    assert "projection_plan" not in plain and "rate_owner_enabled" not in plain


def test_observed_rates_are_read_with_selects_only(tmp_path, monkeypatch):
    from sqlalchemy import text

    from app.services.external_radius_targets import get_external_engine
    from app.services.test_connection_policy import TEST_RADIUS_PREFIX

    url = f"sqlite:///{tmp_path / 'radius.db'}"
    engine = get_external_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INT)"
            )
        )
        for attribute, value in (
            ("Mikrotik-Rate-Limit", "10M/10M"),
            (TEST_RADIUS_PREFIX + "Mikrotik-Rate-Limit", "20M/20M"),
            ("Framed-IP-Address", "192.0.2.1"),
        ):
            conn.execute(
                text("INSERT INTO radreply VALUES ('a', :a, ':=', :v)"),
                {"a": attribute, "v": value},
            )
    target = {
        "db_url": url,
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
    }

    rates, test_rates = radius_population.observed_rate_limits(target, ["a", "b"])

    assert rates == {"a": ["10M/10M"]}
    assert test_rates == {"a": ["20M/20M"]}


# ------------------------------------------------------------ S2 nits


def test_s2_a_move_must_end_on_the_current_offer(db_session, world, tmp_path):
    """The applied change must be to the offer the service is on now."""
    from datetime import date, datetime

    from app.models.subscription_change import (
        SubscriptionChangeRequest,
        SubscriptionChangeStatus,
    )

    sub, old_offer, new_offer = _moved_service(db_session, applied=False)
    elsewhere = _offer(db_session, "Elsewhere")
    db_session.add(
        SubscriptionChangeRequest(
            subscription_id=sub.id,
            current_offer_id=old_offer.id,
            requested_offer_id=elsewhere.id,  # not the offer it is on
            effective_date=date(2026, 10, 1),
            status=SubscriptionChangeStatus.applied,
            applied_at=datetime.fromisoformat("2026-10-22T09:00:00+00:00"),
        )
    )
    db_session.commit()
    path, digest = _record(tmp_path, [_moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)

    assert switch.unverified_moves(db_session, approved) == [str(sub.id)]
    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)
    assert refused.value.code == "access.rate_profile_switch.moved_offer_unverified"
    assert new_offer.id == sub.offer_id


def test_s2_a_moved_offer_entry_needs_the_pre_move_offer_id(tmp_path):
    for blank in ("", None, "   "):
        path, digest = _record(
            tmp_path,
            [_entry(str(uuid4()), decision="moved_offer", offer_id=blank)],
            name=f"{uuid4().hex}.json",
        )
        with pytest.raises(DomainError) as invalid:
            switch.load_approved(path, digest)
        assert invalid.value.code == "access.rate_profile_switch.approved_list_invalid"


# ------------------------------------------------ no-target mode (PR-1c)


def _declare_env(monkeypatch, env: str) -> None:
    import dataclasses

    monkeypatch.setattr(
        switch, "settings", dataclasses.replace(switch.settings, app_env_declared=env)
    )


def _no_targets(monkeypatch, env: str = "staging") -> None:
    monkeypatch.setattr(
        switch, "active_external_radius_targets", lambda db, capability=None: []
    )
    _declare_env(monkeypatch, env)


def _no_target_on(db, world, ack, **kwargs):
    return switch.switch_on(
        db,
        context=_context(),
        apply=True,
        populate=world.populate,
        no_radius_target=True,
        acknowledge_divergence_sha256=ack,
        **kwargs,
    )


def _dry_digest(db, world, tmp_path):
    record = tmp_path / "live.json"
    dry = switch.switch_on(
        db,
        context=_context(),
        populate=world.populate,
        emit_approval_list_to=record,
        no_radius_target=True,
    )
    return dry, record


def test_no_target_refuses_while_a_user_target_exists_even_with_the_flag(
    db_session, world, monkeypatch
):
    _clean_service(db_session)
    _declare_env(monkeypatch, "staging")  # the world fixture still has a target

    with pytest.raises(DomainError) as refused:
        _no_target_on(db_session, world, "0" * 64)

    assert refused.value.code == "access.rate_profile_switch.no_target_mode_refused"
    assert refused.value.details["reason"] == "targets_present"
    assert owner.rate_owner_enabled(db_session) is False
    assert world.locks == 0 and world.calls == 0


@pytest.mark.parametrize("env", ["prod", "production"])
def test_no_target_refuses_in_production_even_with_zero_targets(
    db_session, world, monkeypatch, env
):
    _clean_service(db_session)
    _no_targets(monkeypatch, env)

    with pytest.raises(DomainError) as refused:
        _no_target_on(db_session, world, "0" * 64)

    assert refused.value.code == "access.rate_profile_switch.no_target_mode_refused"
    assert refused.value.details["reason"] == "production"
    assert owner.rate_owner_enabled(db_session) is False


def test_zero_targets_without_the_flag_is_the_usual_no_target_refusal(
    db_session, world, monkeypatch
):
    _clean_service(db_session)
    _no_targets(monkeypatch)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world)

    assert refused.value.code == "access.rate_profile_switch.no_target"
    assert "--no-radius-target" in refused.value.message
    assert owner.rate_owner_enabled(db_session) is False


def test_the_dry_run_reports_eligibility_and_the_digest(
    db_session, world, monkeypatch, tmp_path
):
    _divergent(db_session)
    _no_targets(monkeypatch)

    dry, record = _dry_digest(db_session, world, tmp_path)

    assert dry.no_target_eligible is True and dry.no_target_refusal is None
    assert dry.projection_target == "none"
    assert dry.baseline is None and world.calls == 0  # nothing to project
    assert dry.divergences == 1
    # The acknowledged digest IS the emitted record's digest.
    assert dry.divergence_set_sha256 == dry.emitted_list_sha256
    assert dry.divergence_set_sha256 == hashlib.sha256(record.read_bytes()).hexdigest()

    # With a user target the same dry run says the mode is not available.
    monkeypatch.setattr(
        switch, "active_external_radius_targets", lambda db, capability=None: [TARGET]
    )
    again = switch.switch_on(
        db_session,
        context=_context(),
        populate=world.populate,
        no_radius_target=True,
    )
    assert again.no_target_eligible is False
    assert again.no_target_refusal == "targets_present"


def test_a_changed_divergence_set_refuses_the_acknowledgement(
    db_session, world, monkeypatch, tmp_path
):
    _divergent(db_session)
    _no_targets(monkeypatch)
    dry, _record = _dry_digest(db_session, world, tmp_path)

    for ack in (None, "", "0" * 64):
        with pytest.raises(DomainError) as refused:
            _no_target_on(db_session, world, ack)
        assert (
            refused.value.code == "access.rate_profile_switch.divergence_ack_mismatch"
        )
        assert owner.rate_owner_enabled(db_session) is False

    # The set changes after the acknowledgement was taken.
    twelve = _profile(db_session, "Twelve", rate="12M/12M", code="twelve")
    other_offer = _offer(db_session, "B", profile=twelve)
    stale_profile = _profile(db_session, "Stale", rate="7M/7M", code="stale")
    _service(db_session, other_offer, sub_profile=twelve, slot=stale_profile)
    db_session.commit()
    with pytest.raises(DomainError) as stale:
        _no_target_on(db_session, world, dry.divergence_set_sha256)
    assert stale.value.code == "access.rate_profile_switch.divergence_ack_mismatch"
    assert owner.rate_owner_enabled(db_session) is False


def test_no_target_happy_path_flips_audits_and_projects_nothing(
    db_session, world, monkeypatch, tmp_path
):
    key = _divergent(db_session)
    _no_targets(monkeypatch)
    dry, _record = _dry_digest(db_session, world, tmp_path)

    outcome = _no_target_on(db_session, world, dry.divergence_set_sha256)

    assert outcome.applied and outcome.enabled
    assert outcome.projection == "skipped_no_target"
    assert outcome.projection_target == "none"
    assert outcome.divergence_set_sha256 == dry.divergence_set_sha256
    assert owner.rate_owner_enabled(db_session) is True
    assert switch.flag_from_row(db_session) is True
    assert world.calls == 0 and world.full == 0 and world.dry == 0  # no populate
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "access.rate_profile_switch.on")
    ).one()
    meta = event.metadata_
    assert meta["projection_target"] == "none"
    assert meta["app_env_declared"] == "staging"
    assert meta["divergences"] == 1
    assert meta["by_class"] == {key[1]: 1}
    assert meta["divergence_set_sha256"] == dry.divergence_set_sha256


def test_no_target_still_validates_a_supplied_approved_list(
    db_session, world, monkeypatch, tmp_path
):
    """The list is optional here, but if given it is fully verified."""
    sub, old_offer, new_offer = _moved_service(db_session, applied=False)
    _no_targets(monkeypatch)
    dry, _live = _dry_digest(db_session, world, tmp_path)
    path, digest = _record(tmp_path, [_moved_entry(sub, old_offer)])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _no_target_on(db_session, world, dry.divergence_set_sha256, approved=approved)

    assert refused.value.code == "access.rate_profile_switch.moved_offer_unverified"
    assert owner.rate_owner_enabled(db_session) is False
    assert new_offer.id == sub.offer_id


def test_the_eligibility_is_rechecked_under_the_lock(
    db_session, world, monkeypatch, tmp_path
):
    _clean_service(db_session)
    _no_targets(monkeypatch)
    dry, _record = _dry_digest(db_session, world, tmp_path)
    answers = iter(
        [None, "targets_present"]
    )  # the pre-check passes, then a target appears
    monkeypatch.setattr(switch, "no_target_refusal", lambda db: next(answers))

    with pytest.raises(DomainError) as refused:
        _no_target_on(db_session, world, dry.divergence_set_sha256)

    assert refused.value.code == "access.rate_profile_switch.no_target_mode_refused"
    assert world.locks == 1
    assert owner.rate_owner_enabled(db_session) is False


def test_a_flag_that_is_not_visible_after_commit_is_reported(
    db_session, world, monkeypatch, tmp_path
):
    _clean_service(db_session)
    _no_targets(monkeypatch)
    dry, _record = _dry_digest(db_session, world, tmp_path)
    monkeypatch.setattr(switch, "_await_flag", lambda db, expected=True: False)

    with pytest.raises(DomainError) as failed:
        _no_target_on(db_session, world, dry.divergence_set_sha256)

    assert failed.value.code == "access.rate_profile_switch.owner_not_active"
    assert failed.value.details["flag"] is True  # committed, but not yet visible


def test_the_ordinary_path_is_unchanged_with_a_target(db_session, world):
    _clean_service(db_session)

    outcome = _on(db_session, world)

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert outcome.projection is None  # projected for real
    assert world.full == 1


def test_cli_no_target_flags(db_session, world, monkeypatch, capsys, tmp_path):
    _divergent(db_session)
    _no_targets(monkeypatch)
    record = tmp_path / "live.json"
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(radius_population, "populate", world.populate)

    assert (
        switch.main(
            [
                "on",
                "--reason",
                "check",
                "--no-radius-target",
                "--emit-approval-list",
                str(record),
            ]
        )
        == 0
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["no_target_eligible"] is True and dry["projection_target"] == "none"

    assert (
        switch.main(
            [
                "on",
                "--reason",
                "check",
                "--apply",
                "--no-radius-target",
                "--acknowledge-divergence-sha256",
                dry["divergence_set_sha256"],
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["projection"] == "skipped_no_target"

    # The acknowledgement only makes sense for 'on --no-radius-target'.
    for argv in (
        ["on", "--reason", "x", "--acknowledge-divergence-sha256", "ab"],
        [
            "off",
            "--reason",
            "x",
            "--no-radius-target",
            "--acknowledge-divergence-sha256",
            "ab",
        ],
    ):
        with pytest.raises(SystemExit):
            switch.main(argv)
