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


def test_switch_off_flips_audits_and_reprojects(db_session, world):
    _clean_service(db_session)
    _on(db_session, world)
    world.full = 0

    dry = switch.switch_off(db_session, context=_context(), populate=world.populate)
    assert dry.applied is False and dry.enabled is True
    assert owner.rate_owner_enabled(db_session) is True

    outcome = switch.switch_off(
        db_session, context=_context("rollback"), apply=True, populate=world.populate
    )

    assert outcome.applied and outcome.enabled is False
    assert owner.rate_owner_enabled(db_session) is False
    assert world.full == 1
    assert "access.rate_profile_switch.off" in _actions(db_session)


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
    assert owner.rate_owner_enabled(db_session) is False


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


def test_b2_gives_up_and_switches_off_when_the_owner_never_runs(db_session, world):
    _clean_service(db_session)
    world.owner_flag = [False, False, False]

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.owner_not_active"
    assert failed.value.details["auto_switched_off"] is True
    assert owner.rate_owner_enabled(db_session) is False


def test_b2_a_flag_that_never_becomes_visible_switches_off(
    db_session, world, monkeypatch
):
    _clean_service(db_session)
    real = owner.rate_owner_enabled
    seen = {"on": False}

    def stale(db):
        # The row is written, but this reader keeps seeing the old value until
        # the off command runs.
        return False if not seen["on"] else real(db)

    monkeypatch.setattr(owner, "rate_owner_enabled", stale)
    start = world.invalidations

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.owner_not_active"
    assert world.invalidations > start
    assert world.full == 1  # only the automatic switch-off re-projection


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


def test_b3_a_persistent_mismatch_switches_off_after_one_retry(db_session, world):
    _, _, sub, _ = _clean_service(db_session)
    original = world.populate

    def drift(dry_run=True, only_usernames=None, **kwargs):
        result = original(dry_run=dry_run, only_usernames=only_usernames, **kwargs)
        if not dry_run and owner.rate_owner_enabled(world.db):
            world.wrong = {sub.login}
        return result

    world.populate = drift

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.read_back_mismatch"
    assert failed.value.details["auto_switched_off"] is True
    assert failed.value.details["mismatches"] == 1
    assert owner.rate_owner_enabled(db_session) is False
    assert len(world.scoped) == 1
    actions = _actions(db_session)
    assert "access.rate_profile_switch.on" in actions
    assert "access.rate_profile_switch.off" in actions


def test_b3_an_incomplete_projection_switches_the_owner_off_again(db_session, world):
    _clean_service(db_session)
    world.fail_on = {2}  # baseline is call 1; the first real projection is call 2

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.projection_failed"
    assert failed.value.details["auto_switched_off"] is True
    assert owner.rate_owner_enabled(db_session) is False


def test_b3_a_failing_automatic_switch_off_is_reported_not_raised_bare(
    db_session, world
):
    _clean_service(db_session)
    world.fail_on = {2, 3}  # the projection fails, then the off re-projection too

    with pytest.raises(DomainError) as failed:
        _on(db_session, world)

    assert failed.value.code == "access.rate_profile_switch.projection_failed"
    assert failed.value.details["switch_off_error"] == "RadiusProjectionIncomplete"
    # The flag itself did flip back, which the final state reports from the row.
    assert failed.value.details["flag"] is False
    assert switch.flag_from_row(db_session) is False


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


# ------------------------------------- B4: approved list (pending decision)


def _divergent(db):
    """A clean service plus one residue service; returns the residue's key."""
    ten, offer, *_ = _clean_service(db)
    fifteen = _profile(db, "Fifteen", rate="15M/15M")
    residue, _ = _service(db, offer, sub_profile=ten, slot=fifteen)
    db.commit()
    return (str(residue.id), "credential_wins", "15M/15M", "10M/10M")


def _approved_file(tmp_path, entries):
    path = tmp_path / "approved.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "subscription_id": sid,
                        "class": klass,
                        "current_rate": current,
                        "derived_rate": derived,
                    }
                    for sid, klass, current, derived in entries
                ]
            }
        )
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_list_comes_from_the_dry_run_and_lets_the_switch_through(
    db_session, world, tmp_path
):
    key = _divergent(db_session)
    emitted = tmp_path / "emitted.json"
    dry = switch.switch_on(
        db_session,
        context=_context(),
        populate=world.populate,
        emit_approval_list_to=emitted,
    )
    assert dry.emitted_list_sha256 == hashlib.sha256(emitted.read_bytes()).hexdigest()
    entry = json.loads(emitted.read_text())["entries"][0]
    assert (
        entry["subscription_id"],
        entry["class"],
        entry["current_rate"],
        entry["derived_rate"],
    ) == key

    approved = switch.load_approved(emitted, dry.emitted_list_sha256)
    outcome = _on(db_session, world, approved=approved)

    assert outcome.enabled and outcome.read_back.mismatches == 0
    assert outcome.approved_list_sha256 == dry.emitted_list_sha256
    assert outcome.switch_digest == approved.digest
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "access.rate_profile_switch.on")
    ).one()
    assert event.metadata_["approved_list_sha256"] == dry.emitted_list_sha256
    assert event.metadata_["switch_digest"] == approved.digest


def test_a_live_divergence_missing_from_the_list_refuses(db_session, world, tmp_path):
    _divergent(db_session)
    path, digest = _approved_file(tmp_path, [])
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)

    assert refused.value.details["unexpected"] == 1
    assert owner.rate_owner_enabled(db_session) is False


def test_a_listed_change_that_no_longer_diverges_refuses(db_session, world, tmp_path):
    _clean_service(db_session)
    path, digest = _approved_file(
        tmp_path, [(str(uuid4()), "credential_wins", "15M/15M", "10M/10M")]
    )
    approved = switch.load_approved(path, digest)

    with pytest.raises(DomainError) as refused:
        _on(db_session, world, approved=approved)

    assert refused.value.details["missing"] == 1


def test_a_changed_rate_or_class_in_the_list_refuses(db_session, world, tmp_path):
    sid, klass, current, _derived = _divergent(db_session)
    for entry in (
        (sid, klass, current, "12M/12M"),
        (sid, "subscription_differs", current, "10M/10M"),
    ):
        path, digest = _approved_file(tmp_path, [entry])
        approved = switch.load_approved(path, digest)
        with pytest.raises(DomainError) as refused:
            _on(db_session, world, approved=approved)
        assert refused.value.details["unexpected"] == 1
        assert refused.value.details["missing"] == 1


def test_the_list_must_match_its_digest_and_carry_known_classes(tmp_path):
    path, _ = _approved_file(
        tmp_path, [(str(uuid4()), "credential_wins", "15M/15M", "10M/10M")]
    )
    with pytest.raises(DomainError) as refused:
        switch.load_approved(path, "0" * 64)
    assert (
        refused.value.code == "access.rate_profile_switch.approved_list_digest_mismatch"
    )

    bad, bad_digest = _approved_file(
        tmp_path, [(str(uuid4()), "made_up", "15M/15M", "10M/10M")]
    )
    with pytest.raises(DomainError) as invalid:
        switch.load_approved(bad, bad_digest)
    assert invalid.value.code == "access.rate_profile_switch.approved_list_invalid"


def test_cli_emits_and_takes_a_digest_bound_list(
    db_session, world, monkeypatch, capsys, tmp_path
):
    key = _divergent(db_session)
    emitted = tmp_path / "emitted.json"
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(radius_population, "populate", world.populate)

    assert (
        switch.main(["on", "--reason", "check", "--emit-approval-list", str(emitted)])
        == 0
    )
    digest = json.loads(capsys.readouterr().out)["emitted_list_sha256"]
    assert digest == hashlib.sha256(emitted.read_bytes()).hexdigest()

    code = switch.main(
        [
            "on",
            "--reason",
            "check",
            "--approved-list",
            str(emitted),
            "--approved-sha256",
            digest,
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (out["unexpected"], out["missing"]) == (0, 0)
    assert out["approved_list_sha256"] == digest
    assert key
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
