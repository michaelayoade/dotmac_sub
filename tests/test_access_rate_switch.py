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


@pytest.fixture()
def calls(monkeypatch):
    """Record locks and projections; the sweep itself is covered elsewhere."""
    seen = {"lock": 0, "populate": 0}
    monkeypatch.setattr(
        switch, "_take_lock", lambda db: seen.__setitem__("lock", seen["lock"] + 1)
    )
    monkeypatch.setattr(
        switch, "active_external_radius_targets", lambda db, capability=None: [TARGET]
    )

    def populate(dry_run=True, **kwargs):
        assert dry_run is False
        seen["populate"] += 1
        return {"projection_complete": True}

    seen["fn"] = populate
    return seen


def _observe(monkeypatch, db, *, wrong: set[str] = frozenset(), rejected=()):
    """Make the 'target' report exactly the derived rates (except ``wrong``)."""
    expected = owner.derived_rates(db)

    def observed(target, usernames):
        rates = {}
        for login, rate in expected.items():
            if login in wrong:
                rates[login] = ["1k/1k"]
            elif rate:
                rates[login] = [rate]
        return rates, set(rejected)

    monkeypatch.setattr(radius_population, "observed_rate_limits", observed)


def _clean_service(db):
    ten = _profile(db, "Ten", rate="10M/10M")
    offer = _offer(db, "A", profile=ten)
    sub, credential = _service(db, offer, sub_profile=ten)
    db.commit()
    return ten, offer, sub, credential


def _actions(db):
    return list(db.scalars(select(AuditEvent.action).order_by(AuditEvent.occurred_at)))


def test_dry_run_reports_and_changes_nothing(db_session, calls):
    ten, offer, *_ = _clean_service(db_session)
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    _service(db_session, offer, sub_profile=ten, slot=fifteen)
    db_session.commit()

    outcome = switch.switch_on(db_session, context=_context(), populate=calls["fn"])

    assert outcome.applied is False
    assert outcome.divergences == 1
    assert outcome.by_class == {"credential_wins": 1}
    assert owner.rate_owner_enabled(db_session) is False
    assert calls["populate"] == 0 and calls["lock"] == 0


def test_any_divergence_refuses_the_flip(db_session, calls):
    ten, offer, *_ = _clean_service(db_session)
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    _service(db_session, offer, sub_profile=ten, slot=fifteen)
    db_session.commit()

    with pytest.raises(DomainError) as refused:
        switch.switch_on(
            db_session, context=_context(), apply=True, populate=calls["fn"]
        )

    assert refused.value.code == "access.rate_profile_switch.preflight_divergence"
    assert refused.value.details["divergences"] == 1
    assert owner.rate_owner_enabled(db_session) is False
    assert calls["lock"] == 1 and calls["populate"] == 0
    assert "access.rate_profile_switch.on" not in _actions(db_session)


def test_switch_on_locks_flips_audits_projects_and_reads_back(
    db_session, calls, monkeypatch
):
    _clean_service(db_session)
    _observe(monkeypatch, db_session)

    outcome = switch.switch_on(
        db_session, context=_context(), apply=True, populate=calls["fn"]
    )

    assert outcome.applied and outcome.enabled
    assert outcome.read_back.mismatches == 0
    assert outcome.read_back.logins_checked == 1
    assert owner.rate_owner_enabled(db_session) is True
    assert calls["lock"] == 1 and calls["populate"] == 1
    assert "access.rate_profile_switch.on" in _actions(db_session)


def test_switching_on_twice_is_refused(db_session, calls, monkeypatch):
    _clean_service(db_session)
    _observe(monkeypatch, db_session)
    switch.switch_on(db_session, context=_context(), apply=True, populate=calls["fn"])

    with pytest.raises(DomainError) as refused:
        switch.switch_on(
            db_session, context=_context(), apply=True, populate=calls["fn"]
        )

    assert refused.value.code == "access.rate_profile_switch.already_enabled"


def test_a_read_back_mismatch_switches_the_owner_off_again(
    db_session, calls, monkeypatch
):
    _, _, sub, _ = _clean_service(db_session)
    _observe(monkeypatch, db_session, wrong={sub.login})

    with pytest.raises(DomainError) as failed:
        switch.switch_on(
            db_session, context=_context(), apply=True, populate=calls["fn"]
        )

    assert failed.value.code == "access.rate_profile_switch.read_back_mismatch"
    assert failed.value.details["auto_switched_off"] is True
    assert failed.value.details["mismatches"] == 1
    assert owner.rate_owner_enabled(db_session) is False
    assert calls["populate"] == 2  # projection, then the re-projection after off
    actions = _actions(db_session)
    assert "access.rate_profile_switch.on" in actions
    assert "access.rate_profile_switch.off" in actions


def test_an_incomplete_projection_switches_the_owner_off_again(db_session, calls):
    _clean_service(db_session)
    runs = []

    def populate(dry_run=True, **kwargs):
        runs.append(dry_run)
        if len(runs) == 1:
            raise radius_population.RadiusProjectionIncomplete([{"ok": False}])
        return {"projection_complete": True}

    with pytest.raises(DomainError) as failed:
        switch.switch_on(db_session, context=_context(), apply=True, populate=populate)

    assert failed.value.code == "access.rate_profile_switch.projection_failed"
    assert owner.rate_owner_enabled(db_session) is False
    assert runs == [False, False]


def test_hard_rejected_logins_are_not_compared(db_session, calls, monkeypatch):
    _, _, sub, _ = _clean_service(db_session)
    _observe(monkeypatch, db_session, wrong={sub.login}, rejected={sub.login})

    outcome = switch.switch_on(
        db_session, context=_context(), apply=True, populate=calls["fn"]
    )

    assert outcome.read_back.skipped_rejected == 1
    assert outcome.read_back.mismatches == 0


def test_switch_off_flips_audits_and_reprojects(db_session, calls, monkeypatch):
    _clean_service(db_session)
    _observe(monkeypatch, db_session)
    switch.switch_on(db_session, context=_context(), apply=True, populate=calls["fn"])
    calls["populate"] = 0

    dry = switch.switch_off(db_session, context=_context(), populate=calls["fn"])
    assert dry.applied is False and dry.enabled is True
    assert owner.rate_owner_enabled(db_session) is True

    outcome = switch.switch_off(
        db_session, context=_context("rollback"), apply=True, populate=calls["fn"]
    )

    assert outcome.applied and outcome.enabled is False
    assert owner.rate_owner_enabled(db_session) is False
    assert calls["populate"] == 1
    assert "access.rate_profile_switch.off" in _actions(db_session)


def test_the_switch_is_still_closed_to_everything_else(db_session):
    from fastapi import HTTPException

    from app.models.domain_settings import SettingDomain
    from app.services.domain_settings import DomainSettings

    with pytest.raises(HTTPException):
        DomainSettings(SettingDomain.radius)._validate_relationship_change(
            db_session, SettingDomain.radius, "rate_owner_enabled", True
        )


def test_cli_is_dry_run_unless_applied(db_session, calls, monkeypatch, capsys):
    _clean_service(db_session)
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    assert switch.main(["on", "--reason", "check"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["divergences"] == 0
    assert owner.rate_owner_enabled(db_session) is False
    with pytest.raises(SystemExit):
        switch.main(["on"])  # a reason is required


# ------------------------------------------------ approved divergence list


def _divergent(db):
    """A clean service plus one residue service; returns the residue's key."""
    ten, offer, *_ = _clean_service(db)
    fifteen = _profile(db, "Fifteen", rate="15M/15M")
    residue, _ = _service(db, offer, sub_profile=ten, slot=fifteen)
    db.commit()
    return (str(residue.id), "15M/15M", "10M/10M")


def _approved_file(tmp_path, entries):
    path = tmp_path / "approved.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "subscription_id": sid,
                        "current_rate": current,
                        "derived_rate": derived,
                    }
                    for sid, current, derived in entries
                ]
            }
        )
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_exactly_matching_approved_list_lets_the_switch_through(
    db_session, calls, monkeypatch, tmp_path
):
    key = _divergent(db_session)
    path, digest = _approved_file(tmp_path, [key])
    approved = switch.load_approved(path, digest)
    _observe(monkeypatch, db_session)

    dry = switch.switch_on(db_session, context=_context(), approved=approved)
    assert (dry.unexpected, dry.missing) == (0, 0)

    outcome = switch.switch_on(
        db_session,
        context=_context(),
        apply=True,
        populate=calls["fn"],
        approved=approved,
    )

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert outcome.approved_list_sha256 == digest
    assert outcome.switch_digest == approved.digest
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "access.rate_profile_switch.on")
    ).one()
    assert event.metadata_["approved_list_sha256"] == digest
    assert event.metadata_["switch_digest"] == approved.digest


def test_a_live_divergence_missing_from_the_list_refuses(db_session, calls, tmp_path):
    key = _divergent(db_session)
    path, digest = _approved_file(tmp_path, [])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        switch.switch_on(
            db_session,
            context=_context(),
            apply=True,
            populate=calls["fn"],
            approved=approved,
        )

    assert refused.value.details["unexpected"] == 1
    assert owner.rate_owner_enabled(db_session) is False
    assert key  # the residue service was the unexpected one


def test_a_listed_change_that_no_longer_diverges_refuses(db_session, calls, tmp_path):
    _clean_service(db_session)
    path, digest = _approved_file(tmp_path, [(str(uuid4()), "15M/15M", "10M/10M")])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        switch.switch_on(
            db_session,
            context=_context(),
            apply=True,
            populate=calls["fn"],
            approved=approved,
        )

    assert refused.value.details["missing"] == 1
    assert owner.rate_owner_enabled(db_session) is False


def test_a_changed_rate_in_the_list_refuses(db_session, calls, tmp_path):
    sid, current, _derived = _divergent(db_session)
    path, digest = _approved_file(tmp_path, [(sid, current, "12M/12M")])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        switch.switch_on(
            db_session,
            context=_context(),
            apply=True,
            populate=calls["fn"],
            approved=approved,
        )

    assert refused.value.details["unexpected"] == 1
    assert refused.value.details["missing"] == 1


def test_the_list_must_match_its_stated_digest(tmp_path):
    path, digest = _approved_file(tmp_path, [(str(uuid4()), "15M/15M", "10M/10M")])

    with pytest.raises(DomainError) as refused:
        switch.load_approved(path, "0" * 64)
    assert (
        refused.value.code == "access.rate_profile_switch.approved_list_digest_mismatch"
    )

    path.write_text("{}")
    with pytest.raises(DomainError):
        switch.load_approved(path, hashlib.sha256(b"{}").hexdigest())
    assert digest != hashlib.sha256(b"{}").hexdigest()


def test_cli_takes_a_digest_bound_list(
    db_session, calls, monkeypatch, capsys, tmp_path
):
    key = _divergent(db_session)
    path, digest = _approved_file(tmp_path, [key])
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    code = switch.main(
        [
            "on",
            "--reason",
            "check",
            "--approved-list",
            str(path),
            "--approved-sha256",
            digest,
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (out["unexpected"], out["missing"]) == (0, 0)
    assert out["approved_list_sha256"] == digest
