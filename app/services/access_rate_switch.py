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
6. on any failure, reports the failure with the final state. This release has
   no legacy precedence to return to, so nothing is switched off automatically
   and :func:`switch_off` is refused: rolling back means redeploying the
   previous release (a production process will not start while the flag is
   off).

The dry run is the default; ``apply=True`` is explicit. Logins the projection deliberately does not write
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain, SettingValueType
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
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
    unverified_moves: int = 0
    projection: str | None = None
    projection_target: str | None = None
    divergence_set_sha256: str | None = None
    no_target_eligible: bool | None = None
    no_target_refusal: str | None = None

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
            "unverified_moves": self.unverified_moves,
            "projection": self.projection,
            "projection_target": self.projection_target,
            "divergence_set_sha256": self.divergence_set_sha256,
            "no_target_eligible": self.no_target_eligible,
            "no_target_refusal": self.no_target_refusal,
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
class MovedClaim:
    """A ``moved_offer`` decision: the service left its pre-move package."""

    subscription_id: str
    pre_move_offer_id: str
    decided_at: datetime


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
    moved: tuple[MovedClaim, ...] = ()

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
    moved: list[MovedClaim] = []
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
            try:
                decided_at = datetime.fromisoformat(
                    str(item["decided_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise _invalid("decided_at is not a timestamp.") from exc
            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=UTC)
            pre_move = str(item.get("offer_id") or "").strip()
            if not pre_move:
                raise _invalid(
                    "A moved_offer entry needs the pre-move offer_id.",
                    subscription_id=sid,
                )
            moved.append(MovedClaim(sid, pre_move, decided_at))
        else:
            keys.add((sid, klass, current, derived))
    if undecided:
        raise _invalid(
            "Every entry needs a decision before the switch.", undecided=undecided
        )
    return ApprovedDivergences(
        entries=frozenset(keys),
        sha256=actual,
        audit_only=audit_only,
        moved=tuple(moved),
    )


def record_bytes(rows: Sequence[access_rate_profile.Divergence]) -> bytes:
    """The exception record for the live divergences, decisions left blank.

    This one serialisation is both what ``--emit-approval-list`` writes and what
    a digest acknowledgement hashes, so the two can never disagree.
    """
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
    return (
        json.dumps(
            {"schema": RECORD_SCHEMA, "entries": entries}, indent=2, sort_keys=True
        )
        + "\n"
    ).encode()


def divergence_set_sha256(rows: Sequence[access_rate_profile.Divergence]) -> str:
    """sha256 of the live divergence set, as the emitted record would hash."""
    return hashlib.sha256(record_bytes(rows)).hexdigest()


def emit_approval_list(
    rows: Sequence[access_rate_profile.Divergence], path: str | Path
) -> str:
    """Write the live divergences as an exception record with blank decisions."""
    raw = record_bytes(rows)
    Path(path).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def unverified_moves(db: Session, approved: ApprovedDivergences | None) -> list[str]:
    """Subscription ids whose ``moved_offer`` claim is not backed by evidence.

    A claim holds only when the subscription's current offer differs from the
    recorded pre-move offer AND an applied subscription change request exists
    for it whose requested offer is the current offer, applied before the
    decision was made.
    """
    if approved is None:
        return []
    bad: list[str] = []
    for claim in approved.moved:
        try:
            subscription = db.get(Subscription, UUID(claim.subscription_id))
        except ValueError:
            subscription = None
        applied = (
            db.scalars(
                select(SubscriptionChangeRequest.applied_at).where(
                    SubscriptionChangeRequest.subscription_id == subscription.id,
                    SubscriptionChangeRequest.requested_offer_id
                    == subscription.offer_id,
                    SubscriptionChangeRequest.status
                    == SubscriptionChangeStatus.applied,
                    SubscriptionChangeRequest.applied_at.isnot(None),
                )
            ).all()
            if subscription is not None
            else []
        )

        def before(stamp: datetime, claim: MovedClaim = claim) -> bool:
            aware = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            return aware < claim.decided_at

        if (
            subscription is None
            or str(subscription.offer_id) == claim.pre_move_offer_id
            or not any(before(stamp) for stamp in applied if stamp is not None)
        ):
            bad.append(claim.subscription_id)
    return bad


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


@contextmanager
def _populate_lock() -> Iterator[None]:
    """Hold the Celery populate single-flight lock for a whole apply.

    The beat/event populate task takes the same session advisory lock, so while
    the switch holds it no sweep can compute with the old flag and write after
    the switch's projection. Busy means refuse before anything is changed.
    """
    from app.tasks._postgres_lock import postgres_session_advisory_lock
    from app.tasks.radius_population import _POPULATE_LOCK_KEY

    with postgres_session_advisory_lock(_POPULATE_LOCK_KEY) as acquired:
        if not acquired:
            raise _error(
                "populate_busy",
                "A RADIUS populate run holds the projection lock; nothing was "
                "changed. Retry when it has finished.",
            )
        yield


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


def _await_flag(db: Session, expected: bool = True) -> bool:
    """Poll until the flag reads ``expected``, bounded by the cache TTL plus 5s."""
    deadline = _monotonic() + float(settings.settings_cache_ttl_seconds) + 5.0
    while True:
        _release_reads(db)
        if access_rate_profile.rate_owner_enabled(db) is expected:
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(1.0)


def flag_from_row(db: Session) -> bool | None:
    """The switch as stored in its row, uncached (None when there is no row)."""
    _release_reads(db)
    row = DomainSettings(SettingDomain.radius).get_optional_by_key(
        db, access_rate_profile.SETTING_KEY, active_only=True
    )
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
    by_id = {str(s.id): s for s in subscriptions}
    expected: dict[str, tuple[str, str | None, bool, str | None]] = {}
    #: Written logins whose canonical owner cannot be resolved: never skipped.
    unresolved: dict[str, str] = {}
    for login, item in plan.items():
        if item.get("mode") not in _WRITTEN_MODES:
            continue
        if only is not None and login not in only:
            continue
        # The projection chose one canonical owner for the login; check against
        # that subscription, never an arbitrary sibling sharing the login.
        plan_sub_id = item.get("subscription_id")
        subscription = by_id.get(str(plan_sub_id)) if plan_sub_id else None
        if subscription is None or subscription.login != login:
            unresolved[login] = str(plan_sub_id) if plan_sub_id else "unresolved"
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
        raise _error(
            "no_target",
            "No RADIUS projection target is configured. In a non-production "
            "environment with no user target at all, use --no-radius-target.",
        )
    mismatched: dict[str, str] = dict(unresolved)
    for target in targets:
        rates, test_rates = radius_population.observed_rate_limits(target, expected)
        for login, (sub_id, rate, has_test, test_rate) in expected.items():
            if rates.get(login, []) != ([rate] if rate else []):
                mismatched[login] = sub_id
            if has_test and test_rates.get(login, []) != (
                [test_rate] if test_rate else []
            ):
                mismatched[login] = sub_id
    not_written = len(by_login) - len(expected) - len(unresolved) if only is None else 0
    return ReadBack(
        targets=len(targets),
        compared=len(expected) + len(unresolved),
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


#: Environments in which the no-target mode is never allowed.
NO_TARGET_FORBIDDEN_ENVIRONMENTS = frozenset({"prod", "production"})


def no_target_refusal(db: Session) -> str | None:
    """Why the no-target mode may not be used here, or None when it may.

    The mode exists for an environment that has never projected: with zero
    active user targets the switch cannot change any live rate, so the
    projection, baseline and read-back have nothing to act on. It is refused
    wherever a user target exists (a live rate could change) and in production.
    """
    if settings.app_env_declared in NO_TARGET_FORBIDDEN_ENVIRONMENTS:
        return "production"
    if active_external_radius_targets(db, capability="users"):
        return "targets_present"
    return None


def _refuse_no_target_mode(reason: str) -> DomainError:
    return _error(
        "no_target_mode_refused",
        "The no-RADIUS-target mode is only for a non-production environment "
        "with no active user target; nothing was changed.",
        reason=reason,
    )


def switch_on(
    db: Session,
    *,
    context: CommandContext,
    apply: bool = False,
    populate: Callable[..., object] | None = None,
    approved: ApprovedDivergences | None = None,
    emit_approval_list_to: str | Path | None = None,
    trace: dict[str, object] | None = None,
    no_radius_target: bool = False,
    acknowledge_divergence_sha256: str | None = None,
) -> SwitchOutcome:
    """Turn the single rate owner on (dry-run unless ``apply``).

    An apply holds the populate lock across the flip, projections and read-back,
    and refuses (``populate_busy``) if it is taken.

    ``no_radius_target`` is the mode for an environment with no user target at
    all (never in production): the projection, baseline and read-back are
    skipped, and the per-entry match is replaced by a digest acknowledgement of
    the live divergence set (``acknowledge_divergence_sha256``), serialised
    exactly as the emitted record. An ``approved`` list, if supplied, is still
    fully validated.

    ``approved`` is the reviewed list of expected rate changes. The preflight
    passes only when the live divergence set equals it exactly; without a list
    any divergence refuses.
    """
    if apply:
        with _populate_lock():
            return _switch_on(
                db,
                context=context,
                apply=True,
                populate=populate,
                approved=approved,
                emit_approval_list_to=emit_approval_list_to,
                trace=trace,
                no_radius_target=no_radius_target,
                acknowledge_divergence_sha256=acknowledge_divergence_sha256,
            )
    return _switch_on(
        db,
        context=context,
        apply=False,
        populate=populate,
        approved=approved,
        emit_approval_list_to=emit_approval_list_to,
        trace=trace,
        no_radius_target=no_radius_target,
        acknowledge_divergence_sha256=acknowledge_divergence_sha256,
    )


def _switch_on(
    db: Session,
    *,
    context: CommandContext,
    apply: bool,
    populate: Callable[..., object] | None,
    approved: ApprovedDivergences | None,
    emit_approval_list_to: str | Path | None,
    trace: dict[str, object] | None,
    no_radius_target: bool,
    acknowledge_divergence_sha256: str | None,
) -> SwitchOutcome:
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
        refusal = no_target_refusal(db)
        # With no user target there is nothing to project or read back, so the
        # baseline is skipped (it could only fail for want of a target).
        no_target_dry_run = refusal is None
        base = None if no_target_dry_run else baseline_read_back(db, run_populate)
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
            unverified_moves=len(unverified_moves(db, approved)),
            projection_target="none" if no_target_dry_run else "present",
            divergence_set_sha256=divergence_set_sha256(rows),
            no_target_eligible=no_target_dry_run,
            no_target_refusal=refusal,
        )

    if no_radius_target:
        refusal = no_target_refusal(db)
        if refusal is not None:
            raise _refuse_no_target_mode(refusal)
        base = None
    else:
        if not active_external_radius_targets(db, capability="users"):
            raise _error(
                "no_target",
                "No RADIUS projection target is configured. In a non-production "
                "environment with no user target at all, use --no-radius-target.",
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
        bad_moves = unverified_moves(db, approved)
        if bad_moves:
            raise _error(
                "moved_offer_unverified",
                "A moved_offer entry is not backed by an applied plan change.",
                count=len(bad_moves),
                subscription_ids=bad_moves[:_MISMATCH_SAMPLE],
            )
        rows = access_rate_profile.preflight(db)
        extra: dict[str, object] = {}
        if no_radius_target:
            # Re-checked under the lock: a target added since the dry run, or a
            # production environment, ends the mode.
            late = no_target_refusal(db)
            if late is not None:
                raise _refuse_no_target_mode(late)
            live_digest = divergence_set_sha256(rows)
            if (
                not acknowledge_divergence_sha256
                or live_digest != acknowledge_divergence_sha256.strip().lower()
            ):
                raise _error(
                    "divergence_ack_mismatch",
                    "The live divergence set does not match the acknowledged "
                    "digest; nothing was changed. Run the dry run again.",
                    divergences=len(rows),
                    by_class=_count(rows),
                    divergence_set_sha256=live_digest,
                )
            extra = {
                "projection_target": "none",
                "app_env_declared": settings.app_env_declared,
                "by_class": _count(rows),
                "divergence_set_sha256": live_digest,
            }
        else:
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
            **extra,
        )
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=len(rows),
            by_class=_count(rows),
            approved_list_sha256=approved.sha256 if approved else None,
            switch_digest=approved.digest if approved else None,
            record_schema=RECORD_SCHEMA if approved else None,
            apply_derived=len(approved.entries) if approved else 0,
            moved_offer=approved.audit_only if approved else 0,
            projection_target="none" if no_radius_target else None,
            divergence_set_sha256=extra.get("divergence_set_sha256"),  # type: ignore[arg-type]
        )

    outcome = execute_owner_command(
        db, definition=_SWITCH_ON, context=context, operation=flip
    )

    if no_radius_target:
        # Nothing to project: confirm the committed flag from the stored row and
        # the cache, and report the projection as skipped.
        _invalidate_switch_cache()
        visible = _await_flag(db)
        stored = flag_from_row(db)
        trace["flag"] = stored
        if not (visible and stored is True):
            raise _error(
                "owner_not_active",
                "The switch was committed but the flag is not visible; check the "
                "final state.",
                flag=stored,
            )
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=outcome.divergences,
            by_class=outcome.by_class,
            approved_list_sha256=outcome.approved_list_sha256,
            switch_digest=outcome.switch_digest,
            record_schema=outcome.record_schema,
            apply_derived=outcome.apply_derived,
            moved_offer=outcome.moved_offer,
            projection="skipped_no_target",
            projection_target="none",
            divergence_set_sha256=outcome.divergence_set_sha256,
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

    # This release has no legacy precedence to return to, so there is no
    # automatic switch-off: the flag stays on (turning it off would stop a
    # production process from starting) and the failure is reported with the
    # final state. Rolling back means redeploying the previous release.
    final_flag = flag_from_row(db)
    trace["flag"] = final_flag
    raise _error(
        failure,
        "The switch ran but its verification failed; investigate before relying "
        "on it, or redeploy the previous release.",
        flag=final_flag,
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
    """Refused: this release derives every rate through the owner.

    The flag records that the governed switch ran and a production process will
    not start while it is off, so turning it off would only stop the service.
    Roll back by redeploying the previous release.
    """
    raise _error(
        "switch_off_unavailable",
        "The rate owner cannot be switched off in this release; redeploy the "
        "previous release to roll back.",
    )


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
        "--no-radius-target",
        action="store_true",
        help=(
            "environment with no active RADIUS user target (never production): "
            "skip projection and read-back"
        ),
    )
    parser.add_argument(
        "--acknowledge-divergence-sha256",
        help=(
            "with --no-radius-target on 'on': the sha256 of the live divergence "
            "set, as reported by the dry run"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="make the change; without it the command only reports",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.no_radius_target and args.action != "on":
        parser.error("--no-radius-target applies to 'on' only")
    if args.acknowledge_divergence_sha256 and not (
        args.no_radius_target and args.action == "on"
    ):
        parser.error("--acknowledge-divergence-sha256 needs 'on --no-radius-target'")
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
                    no_radius_target=args.no_radius_target,
                    acknowledge_divergence_sha256=args.acknowledge_divergence_sha256,
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
