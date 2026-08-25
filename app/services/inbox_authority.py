"""Which system owns inbox writes right now, and what it takes to change that.

ADR-0013 P4/P5. Three stages, and every inbox write path reads the stage from
here rather than deciding for itself:

- **LOCAL** — Sub's `public.inbox_*` tables are the authority. The composed
  modules are installed and empty. This is the state until someone acts.
- **SHADOW** — Sub is still the authority; the modules are written too, so the
  drift comparator has something to compare. Module writes here are BEST
  EFFORT: an exception is recorded and swallowed, because a contact centre
  losing an inbound WhatsApp message to a shadow bug is a worse outcome than
  the shadow window taking another week.
- **MODULE** — the composed modules are the authority. Module writes now
  propagate their exceptions, and `public.inbox_*` is a projection.

The stage is DERIVED, never stored as a mode string: the cutover row's presence
means MODULE, the setting means SHADOW, and neither means LOCAL. There is no
value anyone can set to "MODULE" without the row, and no row that means
anything other than MODULE.

## Why the switch is a row and not a setting

A setting can be turned back. After the modules have accepted writes Sub never
saw, turning it back does not restore the previous world — it creates a second
authority holding a subset of the truth, silently. So the forward step is a
durable, uniquely-keyed, delete-free row gated on evidence, and the way back is
the reverse reconciler run in ADR-0013's rollback section, performed knowingly.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import InboxAuthorityCutover
from app.services import settings_spec
from app.services.settings_spec import SettingDomain

logger = logging.getLogger(__name__)

__all__ = [
    "ActivateInboxAuthority",
    "InboxAuthorityActivationRefused",
    "InboxAuthorityStage",
    "activate",
    "cutover_record",
    "drift_fingerprint",
    "resolve_stage",
    "shadow_writes_enabled",
]

_SHADOW_SETTING: Final = "inbox_module_shadow_writes_enabled"


class InboxAuthorityStage(enum.StrEnum):
    """Who owns inbox writes. Ordered: each stage is a superset of the last."""

    LOCAL = "local"
    SHADOW = "shadow"
    MODULE = "module"

    @property
    def writes_modules(self) -> bool:
        """Whether module writes happen at all."""
        return self is not InboxAuthorityStage.LOCAL

    @property
    def modules_are_authoritative(self) -> bool:
        """Whether a module write failing must fail the request."""
        return self is InboxAuthorityStage.MODULE

    @property
    def writes_sub_tables(self) -> bool:
        """Whether Sub's own tables are still written as the authority.

        False at MODULE: the projection is then maintained by the reconciler,
        which is a different writer with a different reason.
        """
        return self is not InboxAuthorityStage.MODULE


def cutover_record(db: Session) -> InboxAuthorityCutover | None:
    """The single activation row, or `None` while authority has not moved."""
    return db.scalar(select(InboxAuthorityCutover).limit(1))


def shadow_writes_enabled(db: Session) -> bool:
    """Whether the shadow window is open.

    Fails CLOSED. A settings backend that cannot answer must not be read as
    "yes, start writing another schema" — the safe reading of an unavailable
    switch is that it is off.
    """
    try:
        return settings_spec.resolve_boolean(db, SettingDomain.comms, _SHADOW_SETTING)
    except Exception:  # pragma: no cover - defensive, settings backend down
        logger.warning("inbox shadow-write setting unreadable; assuming disabled")
        return False


def resolve_stage(db: Session) -> InboxAuthorityStage:
    """The current stage. One place, so no call site invents a fourth answer."""
    if cutover_record(db) is not None:
        return InboxAuthorityStage.MODULE
    if shadow_writes_enabled(db):
        return InboxAuthorityStage.SHADOW
    return InboxAuthorityStage.LOCAL


class InboxAuthorityActivationRefused(RuntimeError):
    """The cutover gate was not satisfied, so nothing was written."""


@dataclass(frozen=True, slots=True)
class ActivateInboxAuthority:
    """The command that moves authority. Every field is evidence.

    `review_reference` is not decoration. ADR-0013 § 6 records that queue
    ordering stops honouring `inbox_conversations.priority` at this moment —
    a behaviour change for the contact centre that somebody has to accept by
    name. A cutover with no reference is a cutover nobody agreed to.
    """

    activated_by: str
    review_reference: str
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    activated_at: datetime | None = None


def drift_fingerprint(report) -> str:
    """A stable SHA-256 over the comparator's verdict.

    Only the counts and the emptiness of the three problem lists are hashed —
    not the row ids, which change as the inbox lives. What is being fingerprinted
    is "this many rows were compared and none disagreed", which is exactly the
    claim the activation rests on, and it can be recomputed later from a re-run.
    """
    payload = {
        "conversations_compared": report.conversations_compared,
        "messages_compared": report.messages_compared,
        "missing_projection": len(report.missing_projection),
        "orphan_projection": len(report.orphan_projection),
        "drift": len(report.drift),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def activate(db: Session, command: ActivateInboxAuthority) -> InboxAuthorityCutover:
    """Move write authority to the composed modules, once, on evidence.

    Refuses unless every one of these holds:

    1. Authority has not already moved. Re-activating is not idempotent in any
       meaningful sense — it would claim a second, different set of evidence for
       a transition that already happened.
    2. A named review reference is supplied (see `ActivateInboxAuthority`).
    3. The drift comparator, run HERE and now rather than quoted from a report
       someone pasted, is clean.
    4. Something was actually compared. A clean comparison over zero rows is
       what an unrun backfill looks like, and it is the single most likely way
       to activate against an empty module schema.

    The caller owns the transaction. This function flushes and does not commit,
    so an activation is only real once the caller's commit succeeds.
    """
    from app.services.inbox_projection_reconciler import compare

    if cutover_record(db) is not None:
        raise InboxAuthorityActivationRefused(
            "inbox authority has already moved to the composed modules; there is "
            "no second cutover. See ADR-0013 'Rollback or forward-fix' for the "
            "way back."
        )

    if not command.review_reference.strip():
        raise InboxAuthorityActivationRefused(
            "activation needs a named review reference: queue ordering stops "
            "honouring inbox_conversations.priority at this moment (ADR-0013 § 6) "
            "and that is a behaviour change somebody must accept by name"
        )

    report = compare(db)
    if not report.clean:
        raise InboxAuthorityActivationRefused(
            f"the inbox drift comparator is not clean: {report.summary()}. "
            "Authority does not move over known drift — reconcile, re-run the "
            "backfill if rows are missing, and try again."
        )

    if report.conversations_compared == 0 and report.messages_compared == 0:
        raise InboxAuthorityActivationRefused(
            "the drift comparator found nothing to compare. A clean verdict over "
            "zero rows is what an unrun backfill looks like, not proof of parity — "
            "run app.services.inbox_backfill.apply first."
        )

    row = InboxAuthorityCutover(
        singleton_key="inbox",
        drift_fingerprint=drift_fingerprint(report),
        conversations_verified=report.conversations_compared,
        messages_verified=report.messages_compared,
        review_reference=command.review_reference.strip(),
        activated_by=command.activated_by,
        command_id=command.command_id,
        correlation_id=command.correlation_id,
        cutover_at=command.activated_at or datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    logger.warning(
        "inbox write authority moved to the composed modules: "
        "conversations=%s messages=%s by=%s",
        row.conversations_verified,
        row.messages_verified,
        row.activated_by,
    )
    return row
