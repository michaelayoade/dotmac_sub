"""Governed switch for the single rate owner (``access.rate_profile_switch``).

``radius.rate_owner_enabled`` is owner-command-only: it can be turned on solely
by :func:`switch_on`, which

1. takes a transaction advisory lock (one switch at a time);
2. re-runs the read-only preflight in the SAME transaction and refuses on any
   divergence;
3. flips the setting and stages an audit record, then commits;
4. runs a full ``radius_population.populate`` (the only FreeRADIUS writer);
5. reads every target's ``radreply`` ``Mikrotik-Rate-Limit`` back and compares
   it with the derived rate, requiring zero mismatches;
6. on a projection failure or any mismatch, switches the owner OFF again and
   re-projects, then reports the failure.

:func:`switch_off` is the matching rollback: it flips the setting off, audits
it and re-projects. Both are dry-run by default; ``apply=True`` is explicit.
No customer data is written outside the existing projection.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain, SettingValueType
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

_SWITCH_ON = OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="switch_on")
_SWITCH_OFF = OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="switch_off")


@dataclass(frozen=True)
class ReadBack:
    """Derived rate versus the rate written to every target's radreply."""

    targets: int
    logins_checked: int
    skipped_rejected: int
    mismatches: int
    sample: tuple[str, ...] = ()


@dataclass(frozen=True)
class SwitchOutcome:
    action: str
    applied: bool
    enabled: bool
    divergences: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    read_back: ReadBack | None = None
    auto_switched_off: bool = False
    unexpected: int = 0
    missing: int = 0
    approved_list_sha256: str | None = None
    switch_digest: str | None = None

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
            "read_back": None
            if self.read_back is None
            else {
                "targets": self.read_back.targets,
                "logins_checked": self.read_back.logins_checked,
                "skipped_rejected": self.read_back.skipped_rejected,
                "mismatches": self.read_back.mismatches,
                "sample_subscription_ids": list(self.read_back.sample),
            },
        }


ApprovedKey = tuple[str, str | None, str | None]


@dataclass(frozen=True)
class ApprovedDivergences:
    """A reviewed list of the exact rate changes the switch is allowed to make.

    Each entry is ``(subscription_id, current_rate, derived_rate)``. The list is
    bound to the sha256 of its file bytes, which the operator states up front.
    """

    entries: frozenset[ApprovedKey]
    sha256: str

    @property
    def digest(self) -> str:
        """Digest recorded with the switch: list hash plus its size."""
        material = json.dumps(
            {"approved_list_sha256": self.sha256, "entries": len(self.entries)},
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_approved(path: str | Path, expected_sha256: str) -> ApprovedDivergences:
    """Read the approved list, refusing unless its bytes match the stated digest."""
    raw = Path(path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.strip().lower():
        raise _error(
            "approved_list_digest_mismatch",
            "The approved list does not match the stated sha256.",
        )
    try:
        entries = json.loads(raw)["entries"]
        keys = frozenset(
            (
                str(item["subscription_id"]),
                item["current_rate"],
                item["derived_rate"],
            )
            for item in entries
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise _error(
            "approved_list_invalid", "The approved list is not valid."
        ) from exc
    if len(keys) != len(entries):
        raise _error(
            "approved_list_invalid", "The approved list repeats a subscription."
        )
    return ApprovedDivergences(entries=keys, sha256=actual)


def _live_keys(rows: Sequence[access_rate_profile.Divergence]) -> set[ApprovedKey]:
    return {(r.subscription_id, r.projected_rate, r.derived_rate) for r in rows}


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


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return DomainError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


def _take_lock(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))


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


def _count(rows: Sequence[access_rate_profile.Divergence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.klass] = counts.get(row.klass, 0) + 1
    return counts


def _release_reads(db: Session) -> None:
    """An owner command needs a transaction-free session at entry."""
    if db.in_transaction():
        db.rollback()


def read_back(db: Session) -> ReadBack:
    """Compare the derived rate with every target's written radreply rate.

    A login the projection hard-rejects has no radreply by design and is
    skipped. Everything else must carry exactly the derived rate, or no rate
    when none is derived.
    """
    subscriptions, credentials, ctx = access_rate_profile._provisioned_state(db)
    expected: dict[str, tuple[str, str | None]] = {}
    for subscription in subscriptions:
        if not subscription.login:
            continue
        derived = access_rate_profile.derive(
            ctx, subscription, credentials.get(subscription.login)
        )
        expected[subscription.login] = (str(subscription.id), derived.rate_limit)

    targets = active_external_radius_targets(db, capability="users")
    if not targets:
        raise _error("no_target", "No RADIUS projection target is configured.")
    mismatched: set[str] = set()
    skipped = 0
    for target in targets:
        rates, rejected = radius_population.observed_rate_limits(target, expected)
        skipped = max(skipped, len(rejected & set(expected)))
        for login, (subscription_id, rate) in expected.items():
            if login in rejected:
                continue
            observed = rates.get(login, [])
            wanted = [rate] if rate else []
            if observed != wanted:
                mismatched.add(subscription_id)
    return ReadBack(
        targets=len(targets),
        logins_checked=len(expected) - skipped,
        skipped_rejected=skipped,
        mismatches=len(mismatched),
        sample=tuple(sorted(mismatched)[:_MISMATCH_SAMPLE]),
    )


def _project(populate: Callable[..., object]) -> None:
    """Full projection; any incomplete target raises."""
    result = populate(dry_run=False)
    if isinstance(result, dict):
        radius_population.require_complete_projection(result)


def switch_on(
    db: Session,
    *,
    context: CommandContext,
    apply: bool = False,
    populate: Callable[..., object] | None = None,
    approved: ApprovedDivergences | None = None,
) -> SwitchOutcome:
    """Turn the single rate owner on (dry-run unless ``apply``).

    ``approved`` is the reviewed list of expected rate changes. The preflight
    passes only when the live divergence set equals it exactly; without a list
    any divergence refuses.
    """
    run_populate = populate or radius_population.populate
    if not apply:
        rows = access_rate_profile.preflight(db)
        db.rollback()
        live = _live_keys(rows)
        wanted = set(approved.entries) if approved is not None else set()
        return SwitchOutcome(
            action="switch_on",
            applied=False,
            enabled=access_rate_profile.rate_owner_enabled(db),
            divergences=len(rows),
            by_class=_count(rows),
            unexpected=len(live - wanted),
            missing=len(wanted - live),
            approved_list_sha256=approved.sha256 if approved else None,
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
        )
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=len(rows),
            approved_list_sha256=approved.sha256 if approved else None,
            switch_digest=approved.digest if approved else None,
        )

    outcome = execute_owner_command(
        db, definition=_SWITCH_ON, context=context, operation=flip
    )

    failure: str | None = None
    report: ReadBack | None = None
    try:
        _project(run_populate)
        report = read_back(db)
        db.rollback()
        if report.mismatches:
            failure = "read_back_mismatch"
    except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
        db.rollback()
        failure = "projection_failed"
        logger.error("rate owner switch-on projection failed: %s", type(exc).__name__)
    if failure is None:
        return SwitchOutcome(
            action="switch_on",
            applied=True,
            enabled=True,
            divergences=outcome.divergences,
            approved_list_sha256=outcome.approved_list_sha256,
            switch_digest=outcome.switch_digest,
            read_back=report,
        )

    # Automatic switch-off: restore the previous behaviour and re-project.
    off_context = CommandContext.system(
        actor=context.actor,
        scope=SCOPE,
        reason=f"automatic switch-off after {failure}",
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
    )
    off = switch_off(db, context=off_context, apply=True, populate=run_populate)
    raise _error(
        failure,
        "The rate owner was switched off again: "
        + (
            "the written rates do not match the derived rates."
            if failure == "read_back_mismatch"
            else "the full projection did not complete."
        ),
        auto_switched_off=True,
        reprojected=off.applied,
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
    _project(run_populate)
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
        "--apply",
        action="store_true",
        help="make the change; without it the command only reports",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    from app.db import SessionLocal

    db = SessionLocal()
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
                    db, context=context, apply=args.apply, approved=approved
                )
            else:
                outcome = switch_off(db, context=context, apply=args.apply)
        except DomainError as exc:
            print(
                json.dumps(
                    {
                        "refused": exc.code,
                        "message": exc.message,
                        "details": exc.details,
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
