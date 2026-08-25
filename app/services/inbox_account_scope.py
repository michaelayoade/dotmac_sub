"""Which connected account a conversation arrived at.

`account_scope` is part of the module's thread key on every channel — two
mailboxes talking to one customer are two conversations, and merging them
exposes one team's thread to another. Sub has never stored it on a
conversation, so it has to be resolved, and the resolution has to be the SAME
rule at backfill time and at write time or the two will thread history and new
traffic differently.

That is the whole reason this module exists rather than the ladder living
inside `inbox_backfill`. A historical row and a live inbound message get the
same answer from the same function.

The ladder (ADR-0013 § 5):

1. What the provider told us.
2. The single active channel route for this team and channel.
3. The declared internal literal, for a channel with no external transport.
4. Nothing — the caller decides whether that is fatal.

There is deliberately no "first active route" fallback. Two candidate routes
means we do not know which mailbox this arrived at, and picking one merges two
teams' threads in a way nothing downstream can detect.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import TeamInboxChannelRoute
from app.services.inbox_module.references import internal_account_scope

__all__ = [
    "AccountScopeUnresolved",
    "active_route_scopes",
    "resolve_account_scope",
]


class AccountScopeUnresolved(LookupError):
    """No rung of the ladder produced an account scope."""

    def __init__(self, *, channel: str, service_team_id: uuid.UUID | None) -> None:
        super().__init__(
            f"no account scope for channel {channel!r} team {service_team_id}: no "
            "provider scope, no single active channel route, and the channel has "
            "an external transport. See ADR-0013 § 5 — this is refused rather "
            "than defaulted."
        )
        self.channel = channel
        self.service_team_id = service_team_id


def active_route_scopes(db: Session) -> dict[tuple[uuid.UUID, str], list[str]]:
    """Active channel routes grouped by `(service_team_id, channel_type)`.

    Returned as a mapping so a caller resolving many conversations loads the
    routes once. The backfill does exactly that over ~24k rows.
    """
    rows = db.execute(
        select(
            TeamInboxChannelRoute.service_team_id,
            TeamInboxChannelRoute.channel_type,
            TeamInboxChannelRoute.account_scope,
        ).where(TeamInboxChannelRoute.is_active.is_(True))
    ).all()
    grouped: dict[tuple[uuid.UUID, str], list[str]] = defaultdict(list)
    for team_id, channel_type, scope in rows:
        grouped[(team_id, channel_type)].append(scope)
    return grouped


def resolve_account_scope(
    *,
    channel: str,
    service_team_id: uuid.UUID | None,
    provider_account_scope: str | None = None,
    route_scopes: dict[tuple[uuid.UUID, str], list[str]] | None = None,
    db: Session | None = None,
) -> str | None:
    """Resolve the connected account, or `None` for the caller to refuse.

    Either `route_scopes` (preloaded) or `db` must be supplied. Preloading is
    the point for bulk callers; the `db` form is the convenience for a single
    live message.
    """
    if provider_account_scope:
        return provider_account_scope

    if service_team_id is not None:
        if route_scopes is None:
            if db is None:
                raise TypeError("resolve_account_scope needs route_scopes or db")
            route_scopes = active_route_scopes(db)
        candidates = route_scopes.get((service_team_id, channel), [])
        if len(candidates) == 1:
            return candidates[0]

    return internal_account_scope(channel)


def require_account_scope(
    *,
    channel: str,
    service_team_id: uuid.UUID | None,
    provider_account_scope: str | None = None,
    route_scopes: dict[tuple[uuid.UUID, str], list[str]] | None = None,
    db: Session | None = None,
) -> str:
    """`resolve_account_scope`, raising instead of returning `None`."""
    scope = resolve_account_scope(
        channel=channel,
        service_team_id=service_team_id,
        provider_account_scope=provider_account_scope,
        route_scopes=route_scopes,
        db=db,
    )
    if scope is None:
        raise AccountScopeUnresolved(channel=channel, service_team_id=service_team_id)
    return scope
