"""Sub vocabulary in, module vocabulary out. Declared once, formatted nowhere else.

The two composed inbox modules speak in opaque strings — `conversation_reference`,
`agent_reference`, `account_scope` — precisely so they carry no product model. That
makes the FORMAT of those strings Sub's decision, and a decision made at forty call
sites is not a decision. Everything that turns a Sub identifier into a module
reference lives here.

`str(uuid)` looks too trivial to wrap. It is not: the module's columns are
`String(160)`/`String(180)`, so any format at all fits, and the day one caller
writes `f"agent:{person_id}"` the round-robin cursor and the assignment rows stop
agreeing about who an agent is — silently, because both are valid strings. The
functions below exist to make that impossible rather than unlikely.

See `docs/adr/0013-inbox-authority-cutover.md` § 7.
"""

from __future__ import annotations

import uuid
from typing import Final

from dotmac_inbox.channels import Transport, channel_spec
from dotmac_inbox.threading import InboundIdentity
from dotmac_inbox_operations.contracts import PresenceState

from app.models.team_inbox import InboxAgentAwayReason, InboxAgentPresenceStatus
from app.services.inbox_channels import INTERNAL_ACCOUNT_SCOPE

__all__ = [
    "PRESENCE_STATE_BY_SUB_STATUS",
    "PRESENCE_AWAY_REASON_BY_SUB_STATUS",
    "agent_reference",
    "conversation_reference",
    "inbound_identity",
    "internal_account_scope",
    "person_id_from_agent_reference",
    "presence_state",
    "presence_away_reason",
    "sub_presence_status",
]


def conversation_reference(conversation_id: uuid.UUID) -> str:
    """The opaque handle inbox-operations holds for one conversation.

    It is the conversation's own uuid because ADR-0013 § 2 keeps the module row
    and the Sub row on one id. That makes the reference resolvable in both
    directions with no mapping table, which is what lets the drift comparator
    join by primary key.
    """
    return str(conversation_id)


def agent_reference(person_id: uuid.UUID) -> str:
    """The opaque handle inbox-operations holds for one operator."""
    return str(person_id)


def person_id_from_agent_reference(reference: str) -> uuid.UUID:
    """The inverse, for reading module rows back into Sub's vocabulary.

    Raises `ValueError` on anything this module did not write, which is the
    point: a reference in another format means something wrote the module's
    tables without going through here.
    """
    return uuid.UUID(reference)


#: Sub declares four presence states; the module declares three. `on_break` and
#: `away` both mean "present, not assignable", which is the only distinction the
#: module's dispatch reads — so collapsing them changes no behaviour, and Sub
#: keeps the roster-display difference in its own row. Exhaustive over
#: `InboxAgentPresenceStatus` on purpose; a new Sub state must be mapped here or
#: `presence_state` raises. ADR-0013 § 6 records this as a knowing narrowing.
PRESENCE_STATE_BY_SUB_STATUS: Final[dict[InboxAgentPresenceStatus, PresenceState]] = {
    InboxAgentPresenceStatus.online: PresenceState.AVAILABLE,
    InboxAgentPresenceStatus.away: PresenceState.AWAY,
    InboxAgentPresenceStatus.on_break: PresenceState.AWAY,
    InboxAgentPresenceStatus.offline: PresenceState.OFFLINE,
}

#: The product-owned half of the same mapping. Dispatch deliberately sees only
#: `AWAY`; the roster retains whether that meant an ordinary away interval or a
#: break. Non-away states carry no away reason.
PRESENCE_AWAY_REASON_BY_SUB_STATUS: Final[
    dict[InboxAgentPresenceStatus, InboxAgentAwayReason | None]
] = {
    InboxAgentPresenceStatus.online: None,
    InboxAgentPresenceStatus.away: InboxAgentAwayReason.away,
    InboxAgentPresenceStatus.on_break: InboxAgentAwayReason.break_,
    InboxAgentPresenceStatus.offline: None,
}


def presence_state(status: InboxAgentPresenceStatus | str) -> PresenceState:
    """Map one Sub presence status onto the module's three-state vocabulary."""
    resolved = (
        status
        if isinstance(status, InboxAgentPresenceStatus)
        else InboxAgentPresenceStatus(status)
    )
    try:
        return PRESENCE_STATE_BY_SUB_STATUS[resolved]
    except KeyError:  # pragma: no cover - unreachable while the map is total
        raise ValueError(
            f"presence status {resolved.value!r} has no module state. Add it to "
            "PRESENCE_STATE_BY_SUB_STATUS and say in ADR-0013 § 6 what the "
            "narrowing costs."
        ) from None


def presence_away_reason(
    status: InboxAgentPresenceStatus | str,
) -> InboxAgentAwayReason | None:
    """Preserve Sub's roster detail independently of dispatch availability."""
    resolved = (
        status
        if isinstance(status, InboxAgentPresenceStatus)
        else InboxAgentPresenceStatus(status)
    )
    return PRESENCE_AWAY_REASON_BY_SUB_STATUS[resolved]


def sub_presence_status(
    state: PresenceState, away_reason: InboxAgentAwayReason | str | None
) -> InboxAgentPresenceStatus:
    """Reconstruct the roster state from its two authoritative inputs."""
    if state is PresenceState.AVAILABLE:
        if away_reason is not None:
            raise ValueError("AVAILABLE presence cannot carry an away reason")
        return InboxAgentPresenceStatus.online
    if state is PresenceState.OFFLINE:
        if away_reason is not None:
            raise ValueError("OFFLINE presence cannot carry an away reason")
        return InboxAgentPresenceStatus.offline
    reason = (
        away_reason
        if isinstance(away_reason, InboxAgentAwayReason)
        else InboxAgentAwayReason(away_reason or InboxAgentAwayReason.away.value)
    )
    return (
        InboxAgentPresenceStatus.on_break
        if reason is InboxAgentAwayReason.break_
        else InboxAgentPresenceStatus.away
    )


def internal_account_scope(channel: str) -> str | None:
    """`sub:internal` for a channel Sub declared with no external transport.

    Read from the declaration rather than a second list of channel names, so
    changing a channel's transport in `app/services/inbox_channels.py` changes
    this answer too.
    """
    if channel_spec(channel).transport is Transport.INTERNAL:
        return INTERNAL_ACCOUNT_SCOPE
    return None


def inbound_identity(
    *,
    channel: str,
    account_scope: str,
    contact: str,
    external_thread_id: str | None = None,
    external_message_id: str | None = None,
    subject: str | None = None,
    body: str | None = None,
) -> InboundIdentity:
    """Build the identity both threading rules read.

    A thin constructor on purpose. It exists so that every Sub caller passes the
    same field to the same slot — `contact` is the EXTERNAL party and
    `account_scope` is OUR connected account, and swapping them produces a
    perfectly valid thread key for the wrong thread.
    """
    return InboundIdentity(
        channel=channel,
        account_scope=account_scope,
        contact=contact,
        external_thread_id=external_thread_id,
        external_message_id=external_message_id,
        subject=subject,
        body=body,
    )
