"""Governed switch for the single rate owner (``access.rate_profile_switch``).

``radius.rate_owner_enabled`` is owner-command-only: it can be turned on solely
by :func:`switch_on`. A dry run (the default) reports the preflight divergences
and a BASELINE read-back: it runs ``populate(dry_run=True)`` and compares what
that projection would write with the rates currently in every target's
``radreply``. ``--apply`` refuses unless the baseline is clean.

``switch_on(apply=True)``:

1. takes a transaction advisory lock (one switch at a time);
2. re-runs the read-only preflight in the SAME transaction and refuses on any
   divergence outside the approved list;
3. flips the setting and stages an audit record, then commits;
4. invalidates the settings cache, waits until the flag is visible, and runs a
   full ``radius_population.populate`` (the only FreeRADIUS writer), requiring
   the result to say the owner was in force (retried once);
5. reads back the rate of every login that projection actually wrote, against
   the derived rate (test-access logins against the plan rate), requiring zero
   mismatches after one scoped re-projection of any that differ;
6. on any failure, switches the owner OFF again, re-projects, and reports the
   failure with the final state.

:func:`switch_off` is the matching rollback. Both are dry-run by default;
``apply=True`` is explicit. Logins the projection deliberately does not write
(preserved, unbuildable, mode none, hard-reject) are reported, never counted as
mismatches.

Exception record (``access_rate_exception_record.v1``)
------------------------------------------------------

Some services are expected to change rate when the owner is switched on (for
example an imported profile that never matched the package). The dry run can
write them as a reviewed record, ``--emit-approval-list FILE``::

    {"schema": "access_rate_exception_record.v1",
     "entries": [{"subscription_id": ..., "login": ..., "offer_id": ...,
                  "current_rate": ..., "derived_rate": ...,
                  "divergence_class": <a preflight class>,
                  "decision": null, "decided_by": null, "decided_at": null,
                  "reason": null}]}

Reviewers fill in ``decision`` (``apply_derived``: give the service its package
rate; ``moved_offer``: the service was already moved to another package by a
normal plan change, recorded for audit only and never expected to diverge),
``decided_by``, ``decided_at`` and a free-text ``reason``. ``--apply`` takes the
file with its sha256 (``--approved-list FILE --approved-sha256 HEX``) and
refuses unless every entry is decided and the live divergence set equals the
``apply_derived`` entries exactly. The file's sha256 goes into the switch digest
and the audit record. The same record is the input to the later catalog cutover
mapping: each reviewed service is mapped to the specification of its package.

CLI::

    python -m app.services.access_rate_switch on  --reason TEXT [--apply]
    python -m app.services.access_rate_switch off --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import access_rate_profile, radius_population
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.domain_settings import DomainSettings
from app.services.external_radius_targets import active_external_radius_targets
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = access_rate_profile.SWITCH_OWNER
CONCERN = "rate owner switch"
SCOPE = "access:rate_profile:switch"
#: Transaction advisory lock key serialising switch commands.
LOCK_KEY = 3281601311
_MISMATCH_SAMPLE = 20
#: Modes whose reply rows (and therefore rate) the projection writes.
_WRITTEN_MODES = frozenset({"active", "captive"})

_SWITCH_ON = OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="switch_on")
_SWITCH_OFF = OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="switch_off")

# Indirections so tests can run the wait without sleeping.
_sleep = time.sleep
_monotonic = time.monotonic


@dataclass(frozen=True)
class ReadBack:
    """Written rate versus the expected rate, for the logins actually written."""

    targets: int
    compared: int
    not_written: int
    mismatches: int
    sample: tuple[str, ...] = ()
    mismatched_logins: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Baseline:
    """Current radreply versus what a dry-run projection would write."""

    projection_complete: bool
    read_back: ReadBack | None = None
    error: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.projection_complete
            and self.error is None
            and self.read_back is not None
            and self.read_back.mismatches == 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "projection_complete": self.projection_complete,
            "error": self.error,
            "read_back": _read_back_dict(self.read_back),
        }


def _read_back_dict(report: ReadBack | None) -> dict[str, object] | None:
    if report is None:
        return None
    return {
        "targets": report.targets,
        "compared": report.compared,
        "not_written": report.not_written,
        "mismatches": report.mismatches,
        "sample_subscription_ids": list(report.sample),
    }


@dataclass(frozen=True)
class SwitchOutcome:
    action: str
    applied: bool
    enabled: bool
    divergences: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    read_back: ReadBack | None = None
    baseline: Baseline | None = None
    auto_switched_off: bool = False
    unexpected: int = 0
    missing: int = 0
    approved_list_sha256: str | None = None
    switch_digest: str | None = None
    emitted_list_sha256: str | None = None
    record_schema: str | None = None
    apply_derived: int = 0
    moved_offer: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "applied": self.applied,
            "enabled": self.enabled,
            "divergences": self.divergences,
            "by_class": dict(self.by_class),
            "auto_switched_off": self.auto_switched_off,
            "unexpected": self.unexpected,
            "missing": self.missing,
            "approved_list_sha256": self.approved_list_sha256,
            "switch_digest": self.switch_digest,
            "emitted_list_sha256": self.emitted_list_sha256,
            "record_schema": self.record_schema,
            "apply_derived": self.apply_derived,
            "moved_offer": self.moved_offer,
            "baseline": None if self.baseline is None else self.baseline.as_dict(),
            "read_back": _read_back_dict(self.read_back),
        }


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return DomainError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


# --------------------------------------------------------------------------
# Exception record (digest-bound; the cutover mapping input)
# --------------------------------------------------------------------------

#: Schema of the reviewed list of expected rate changes. It is also the input
#: the later catalog cutover maps from: each reviewed service is given the rate
#: of its package.
RECORD_SCHEMA = "access_rate_exception_record.v1"
#: Give the service the package's derived rate (the switch applies it).
DECISION_APPLY_DERIVED = "apply_derived"
#: The service was moved to another package through a normal plan change before
#: the switch. Recorded for audit only: it must NOT still diverge.
DECISION_MOVED_OFFER = "moved_offer"
DECISIONS = (DECISION_APPLY_DERIVED, DECISION_MOVED_OFFER)

#: (subscription_id, divergence class, current rate, derived rate)
ApprovedKey = tuple[str, str, str | None, str | None]


@dataclass(frozen=True)
class ApprovedDivergences:
    """The reviewed exception record, bound to the sha256 of its file bytes.

    Generate it from the dry run (``--emit-approval-list``), fill in the
    decisions, and state the file's sha256 when switching. ``entries`` are the
    ``apply_derived`` decisions the live divergence set must equal exactly;
    ``audit_only`` counts ``moved_offer`` entries, which are recorded for audit
    and are never expected to diverge.
    """

    entries: frozenset[ApprovedKey]
    sha256: str
    audit_only: int = 0

    @property
    def digest(self) -> str:
        """Digest recorded with the switch: list hash plus its sizes."""
        material = json.dumps(
            {
                "approved_list_sha256": self.sha256,
                "apply_derived": len(self.entries),
                "moved_offer": self.audit_only,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _invalid(message: str, **details: object) -> DomainError:
    return _error("approved_list_invalid", message, **details)


def load_approved(path: str | Path, expected_sha256: str) -> ApprovedDivergences:
    """Read and validate the exception record against its stated sha256."""
    raw = Path(path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.strip().lower():
        raise _error(
            "approved_list_digest_mismatch",
            "The approved list does not match the stated sha256.",
        )
    try:
        document = json.loads(raw)
        schema = document["schema"]
        entries = document["entries"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _invalid("The approved list is not a valid record.") from exc
    if schema != RECORD_SCHEMA:
        raise _invalid("Unsupported record schema.", schema=str(schema))
    if not isinstance(entries, list):
        raise _invalid("The record entries are not a list.")

    keys: set[ApprovedKey] = set()
    audit_only = 0
    seen: set[str] = set()
    undecided = 0
    for item in entries:
        try:
            sid = str(item["subscription_id"])
            klass = str(item["divergence_class"])
            current, derived = item["current_rate"], item["derived_rate"]
            decision = item.get("decision")
        except (KeyError, TypeError, AttributeError) as exc:
            raise _invalid("A record entry is missing a field.") from exc
        for required in ("login", "offer_id"):
            if required not in item:
                raise _invalid("A record entry is missing a field.", field=required)
        if klass not in access_rate_profile.DIVERGENCE_CLASSES:
            raise _invalid("Unknown divergence class.", divergence_class=klass)
        if sid in seen:
            raise _invalid("The record repeats a subscription.")
        seen.add(sid)
        if decision in (None, ""):
            undecided += 1
            continue
        if decision not in DECISIONS:
            raise _invalid("Unknown decision.", decision=str(decision))
        for required in ("decided_by", "decided_at"):
            if not str(item.get(required) or "").strip():
                raise _invalid("A decided entry needs who and when.", field=required)
        if decision == DECISION_MOVED_OFFER:
            audit_only += 1
        else:
            keys.add((sid, klass, current, derived))
    if undecided:
        raise _invalid(
            "Every entry needs a decision before the switch.", undecided=undecided
        )
    return ApprovedDivergences(
        entries=frozenset(keys), sha256=actual, audit_only=audit_only
    )


def emit_approval_list(
    rows: Sequence[access_rate_profile.Divergence], path: str | Path
) -> str:
    """Write the live divergences as an exception record with blank decisions."""
    entries = [
        {
            "subscription_id": r.subscription_id,
            "login": r.login,
            "offer_id": r.offer_id,
            "current_rate": r.projected_rate,
            "derived_rate": r.derived_rate,
            "divergence_class": r.klass,
            "decision": None,
            "decided_by": None,
            "decided_at": None,
            "reason": None,
        }
        for r in sorted(rows, key=lambda r: r.subscription_id)
    ]
    raw = (
        json.dumps(
            {"schema": RECORD_SCHEMA, "entries": entries}, indent=2, sort_keys=True
        )
        + "\n"
    ).encode()
    Path(path).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _live_keys(rows: Sequence[access_rate_profile.Divergence]) -> set[ApprovedKey]:
    return {
        (r.subscription_id, r.klass, r.projected_rate, r.derived_rate) for r in rows
    }


def _count(rows: Sequence[access_rate_profile.Divergence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.klass] = counts.get(row.klass, 0) + 1
    return counts


def _check_divergence(
    rows: Sequence[access_rate_profile.Divergence],
    approved: ApprovedDivergences | None,
) -> None:
    """Pass only when the live divergence set equals the approved list exactly."""
    live = _live_keys(rows)
    wanted = set(approved.entries) if approved is not None else set()
    if live == wanted:
        return
    raise _error(
        "preflight_divergence",
        "Services diverge from the approved list; nothing was changed.",
        divergences=len(rows),
        by_class=_count(rows),
        unexpected=len(live - wanted),
        missing=len(wanted - live),
        approved_list_sha256=approved.sha256 if approved else None,
    )


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def _take_lock(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))


def _release_reads(db: Session) -> None:
    """An owner command needs a transaction-free session at entry."""
    if db.in_transaction():
        db.rollback()


def _audit_actor(context: CommandContext) -> AuditActor:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "user":
        return AuditActor.user(actor_id)
    if prefix == "service":
        return AuditActor.service(actor_id)
    return AuditActor.system(actor_id)


def _write_switch(
    db: Session, context: CommandContext, *, enabled: bool, **evidence: object
) -> None:
    """Flip the setting and stage the audit record (inside the owner command)."""
    DomainSettings(SettingDomain.radius).stage_upsert_by_key(
        db,
        access_rate_profile.SETTING_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
        ),
        owner_command=True,
    )
    stage_audit_event(
        db,
        action=f"access.rate_profile_switch.{'on' if enabled else 'off'}",
        entity_type="domain_setting",
        entity_id=f"radius.{access_rate_profile.SETTING_KEY}",
        actor=_audit_actor(context),
        request_id=str(context.correlation_id),
        metadata={
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "reason": context.reason,
            "enabled": enabled,
            **evidence,
        },
    )


def _invalidate_switch_cache() -> bool:
    """Drop the shared settings-cache entry for the switch (platform scope)."""
    try:
        from dotmac_kernel.setting_scopes import SettingScope
        from dotmac_kernel.settings_cache import invalidate

        invalidate(
            str(SettingDomain.radius),
            access_rate_profile.SETTING_KEY,
            scope=SettingScope.platform(),
        )
    except Exception:  # noqa: BLE001 - the poll below is the real check
        logger.warning("rate owner switch cache invalidation failed")
        return False
    return True


def _await_flag(db: Session) -> bool:
    """Poll until the flag reads true, bounded by the cache TTL plus 5 seconds."""
    deadline = _monotonic() + float(settings.settings_cache_ttl_seconds) + 5.0
    while True:
        _release_reads(db)
        if access_rate_profile.rate_owner_enabled(db) is True:
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(1.0)


def flag_from_row(db: Session) -> bool | None:
    """The switch as stored in its row, uncached (None when there is no row)."""
    _release_reads(db)
    row = db.scalars(
        select(DomainSetting).where(
            DomainSetting.domain == SettingDomain.radius,
            DomainSetting.key == access_rate_profile.SETTING_KEY,
            DomainSetting.is_active.is_(True),
        )
    ).first()
    if row is None:
        return None
    value = row.value_json if row.value_json is not None else row.value_text
    return str(value).strip().lower() in {"true", "1"}


# --------------------------------------------------------------------------
# Read-back
# --------------------------------------------------------------------------


def read_back(
    db: Session,
    plan: Mapping[str, Mapping[str, object]],
    *,
    against: str,
    only: set[str] | None = None,
) -> ReadBack:
    """Compare each written login's radreply rate with the expected rate.

    ``plan`` is the projection's own per-login plan. Only logins whose mode is
    written (active or captive) are compared; every other provisioned login
    (preserved, unbuildable, mode none, hard-reject) is counted in
    ``not_written``. ``against="plan"`` compares with the rate the projection
    decided to write (the baseline); ``against="derived"`` compares with the
    rate the owner derives, test-access logins against the plan rate.
    """
    subscriptions, credentials, ctx = access_rate_profile._provisioned_state(db)
    by_login = {s.login: s for s in subscriptions if s.login}
    expected: dict[str, tuple[str, str | None, bool, str | None]] = {}
    for login, item in plan.items():
        if item.get("mode") not in _WRITTEN_MODES:
            continue
        if only is not None and login not in only:
            continue
        subscription = by_login.get(login)
        if subscription is None:
            continue
        has_test = bool(item.get("test_access"))
        if against == "derived":
            credential = credentials.get(login)
            rate = access_rate_profile.derive(ctx, subscription, credential).rate_limit
            test_rate = (
                access_rate_profile.derive(
                    ctx, subscription, credential, include_enforcement=False
                ).rate_limit
                if has_test
                else None
            )
        else:
            rate = item.get("rate")  # type: ignore[assignment]
            test_rate = item.get("test_rate") if has_test else None  # type: ignore[assignment]
        expected[login] = (str(subscription.id), rate, has_test, test_rate)

    targets = active_external_radius_targets(db, capability="users")
    if not targets:
        raise _error("no_target", "No RADIUS projection target is configured.")
    mismatched: dict[str, str] = {}
    for target in targets:
        rates, test_rates = radius_population.observed_rate_limits(target, expected)
        for login, (sub_id, rate, has_test, test_rate) in expected.items():
            if rates.get(login, []) != ([rate] if rate else []):
                mismatched[login] = sub_id
            if has_test and test_rates.get(login, []) != (
                [test_rate] if test_rate else []
            ):
                mismatched[login] = sub_id
    not_written = len(by_login) - len(expected) if only is None else 0
    return ReadBack(
        targets=len(targets),
        compared=len(expected),
        not_written=max(not_written, 0),
        mismatches=len(mismatched),
        sample=tuple(sorted(mismatched.values())[:_MISMATCH_SAMPLE]),
        mismatched_logins=tuple(sorted(mismatched)),
    )


def baseline_read_back(db: Session, populate: Callable[..., object]) -> Baseline:
    """Dry-run the projection and compare it with the current radreply."""
    try:
        result = populate(dry_run=True, include_projection_plan=True)
        complete = (
            isinstance(result, dict) and result.get("projection_complete") is True
        )
        plan = result.get("projection_plan", {}) if isinstance(result, dict) else {}
        report = read_back(db, plan, against="plan")
        db.rollback()
    except Exception as exc:  # noqa: BLE001 - reported, and --apply refuses
        db.rollback()
        return Baseline(projection_complete=False, error=type(exc).__name__)
    return Baseline(projection_complete=complete, read_back=report)


def _project_checked(
    populate: Callable[..., object], *, only: set[str] | None = None
) -> dict[str, object]:
    """Full (or scoped) projection with the plan; any incomplete target raises."""
    kwargs: dict[str, object] = {"dry_run": False, "include_projection_plan": True}
    if only is not None:
        kwargs["only_usernames"] = only
    result = populate(**kwargs)
    if not isinstance(result, dict):
        raise _error("projection_failed", "The projection returned no result.")
    radius_population.require_complete_projection(result)
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def switch_on(
    db: Session,
    *,
    context: CommandContext,
    apply: bool = False,
    populate: Callable[..., object] | None = None,
    approved: ApprovedDivergences | None = None,
    emit_approval_list_to: str | Path | None = None,
    trace: dict[str, object] | None = None,
) -> SwitchOutcome:
    """Turn the single rate owner on (dry-run unless ``apply``).

    ``approved`` is the reviewed list of expected rate changes. The preflight
    passes only when the live divergence set equals it exactly; without a list
    any divergence refuses.
    """
    run_populate = populate or radius_population.populate
    trace = trace if trace is not None else {}

    if not apply:
        rows = access_rate_profile.preflight(db)
        db.rollback()
        emitted = (
            emit_approval_list(rows, emit_approval_list_to)
            if emit_approval_list_to
            else None
        )
        base = baseline_read_back(db, run_populate)
        live = _live_keys(rows)
        wanted = set(approved.entries) if approved is not None else set()
        return SwitchOutcome(
            action="switch_on",
            applied=False,
            enabled=access_rate_profile.rate_owner_enabled(db),
            divergences=len(rows),
            by_class=_count(rows),
            baseline=base,
            unexpected=len(live - wanted),
            missing=len(wanted - live),
            approved_list_sha256=approved.sha256 if approved else None,
            emitted_list_sha256=emitted,
        )

    base = baseline_read_back(db, run_populate)
    trace["baseline_clean"] = base.clean
    if not base.clean:
        raise _error(
            "baseline_not_clean",
            "The current radreply does not match a dry-run projection; "
            "nothing was changed.",
            baseline=base.as_dict(),
        )

    _release_reads(db)

    def flip() -> SwitchOutcome:
        _take_lock(db)
        if access_rate_profile.rate_owner_enabled(db):
            raise _error("already_enabled", "The rate owner is already switched on.")
        rows = access_rate_profile.preflight(db)
        _check_divergence(rows, approved)
        _write_switch(
            db,
            context,
            enabled=True,
            divergences=len(rows),
            approved_list_sha256=approved.sha256 if approved else None,
            switch_digest=approved.digest if approved else None,
            record_schema=RECORD_SCHEMA if approved else None,
            apply_derived=len(approved.entries) if approved else 0,
            moved_offer=approved.audit_only if approved else 0,
        )
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=len(rows),
            approved_list_sha256=approved.sha256 if approved else None,
            switch_digest=approved.digest if approved else None,
            record_schema=RECORD_SCHEMA if approved else None,
            apply_derived=len(approved.entries) if approved else 0,
            moved_offer=approved.audit_only if approved else 0,
        )

    outcome = execute_owner_command(
        db, definition=_SWITCH_ON, context=context, operation=flip
    )

    failure: str | None = None
    report: ReadBack | None = None
    try:
        _invalidate_switch_cache()
        if not _await_flag(db):
            failure = "owner_not_active"
        else:
            result: dict[str, object] = {}
            for attempt in (1, 2):
                result = _project_checked(run_populate)
                if result.get("rate_owner_enabled") is True:
                    break
                if attempt == 1:
                    _invalidate_switch_cache()
                    _await_flag(db)
            else:
                failure = "owner_not_active"
            trace["projection_complete"] = result.get("projection_complete")
            if failure is None:
                plan = dict(result.get("projection_plan", {}))  # type: ignore[call-overload]
                report = read_back(db, plan, against="derived")
                if report.mismatches:
                    # Ordinary churn (FUP events, plan changes) can land between
                    # the projection and the read: re-project just those logins
                    # once and read again before giving up.
                    again = _project_checked(
                        run_populate, only=set(report.mismatched_logins)
                    )
                    plan.update(again.get("projection_plan", {}))
                    report = read_back(db, plan, against="derived")
                trace["mismatches"] = report.mismatches
                if report.mismatches:
                    failure = "read_back_mismatch"
        db.rollback()
    except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
        db.rollback()
        failure = "projection_failed"
        trace["projection_complete"] = False
        logger.error("rate owner switch-on projection failed: %s", type(exc).__name__)
    if failure is None:
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=outcome.divergences,
            approved_list_sha256=outcome.approved_list_sha256,
            switch_digest=outcome.switch_digest,
            record_schema=outcome.record_schema,
            apply_derived=outcome.apply_derived,
            moved_offer=outcome.moved_offer,
            baseline=base,
            read_back=report,
        )

    # Automatic switch-off: restore the previous behaviour and re-project. A
    # failure here is reported with the final state, never raised bare.
    off_context = CommandContext.system(
        actor=context.actor,
        scope=SCOPE,
        reason=f"automatic switch-off after {failure}",
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
    )
    off_error: str | None = None
    reprojected = False
    try:
        off = switch_off(db, context=off_context, apply=True, populate=run_populate)
        reprojected = off.applied
    except Exception as exc:  # noqa: BLE001 - the final state below says it all
        off_error = type(exc).__name__
        logger.error("rate owner automatic switch-off failed: %s", off_error)
    final_flag = flag_from_row(db)
    trace["flag"] = final_flag
    raise _error(
        failure,
        "The rate owner was switched off again; check the final state."
        if final_flag is False
        else "The rate owner could not be confirmed off; check the final state.",
        auto_switched_off=final_flag is False,
        flag=final_flag,
        reprojected=reprojected,
        switch_off_error=off_error,
        mismatches=report.mismatches if report else None,
        sample_subscription_ids=list(report.sample) if report else [],
    )


def switch_off(
    db: Session,
    *,
    context: CommandContext,
    apply: bool = False,
    populate: Callable[..., object] | None = None,
) -> SwitchOutcome:
    """Turn the single rate owner off and re-project (dry-run unless ``apply``)."""
    run_populate = populate or radius_population.populate
    if not apply:
        enabled = access_rate_profile.rate_owner_enabled(db)
        db.rollback()
        return SwitchOutcome(action="switch_off", applied=False, enabled=enabled)
    _release_reads(db)

    def flip() -> SwitchOutcome:
        _take_lock(db)
        _write_switch(db, context, enabled=False)
        return SwitchOutcome(action="switch_off", applied=True, enabled=False)

    outcome = execute_owner_command(
        db, definition=_SWITCH_OFF, context=context, operation=flip
    )
    _invalidate_switch_cache()
    _project_checked(run_populate)
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.access_rate_switch")
    parser.add_argument("action", choices=("on", "off"))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", default="service:access-rate-switch")
    parser.add_argument("--approved-list", help="reviewed expected-changes JSON file")
    parser.add_argument(
        "--approved-sha256", help="sha256 the approved list file must match"
    )
    parser.add_argument(
        "--emit-approval-list",
        help="dry run only: write the live divergences as an approval list",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="make the change; without it the command only reports",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.emit_approval_list and (args.apply or args.action != "on"):
        parser.error("--emit-approval-list is a dry-run option of 'on'")

    from app.db import SessionLocal

    db = SessionLocal()
    trace: dict[str, object] = {}
    try:
        context = CommandContext(
            command_id=uuid4(),
            correlation_id=uuid4(),
            actor=args.actor,
            scope=SCOPE,
            reason=args.reason,
        )
        try:
            if args.action == "on":
                approved = None
                if args.approved_list or args.approved_sha256:
                    if not (args.approved_list and args.approved_sha256):
                        parser.error(
                            "--approved-list and --approved-sha256 go together"
                        )
                    approved = load_approved(args.approved_list, args.approved_sha256)
                outcome = switch_on(
                    db,
                    context=context,
                    apply=args.apply,
                    approved=approved,
                    emit_approval_list_to=args.emit_approval_list,
                    trace=trace,
                )
            else:
                outcome = switch_off(db, context=context, apply=args.apply)
        except Exception as exc:  # noqa: BLE001 - always end with a final state
            try:
                trace.setdefault("flag", flag_from_row(db))
            except Exception:  # noqa: BLE001
                trace.setdefault("flag", None)
            print(
                json.dumps(
                    {
                        "refused" if isinstance(exc, DomainError) else "error": (
                            exc.code
                            if isinstance(exc, DomainError)
                            else type(exc).__name__
                        ),
                        "message": exc.message
                        if isinstance(exc, DomainError)
                        else None,
                        "details": exc.details if isinstance(exc, DomainError) else {},
                        "final_state": {
                            "flag": trace.get("flag"),
                            "projection_complete": trace.get("projection_complete"),
                            "mismatches": trace.get("mismatches"),
                        },
                    },
                    sort_keys=True,
                    default=str,
                )
            )
            return 1
        print(json.dumps(outcome.as_dict(), sort_keys=True))
        return 0 if (outcome.applied or not args.apply) else 1
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
