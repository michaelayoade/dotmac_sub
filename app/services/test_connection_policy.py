"""Pure, time-bounded evidence shared by network readers and transports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TestConnectionAccess:
    grant_id: UUID
    subscription_id: UUID
    activated_at: datetime
    expires_at: datetime

    def valid_at(self, now: datetime) -> bool:
        return self.activated_at <= now < self.expires_at

    def remaining_seconds(self, now: datetime) -> int:
        return max(0, int((self.expires_at - now).total_seconds()))


def utc_datetime(value: datetime) -> datetime:
    """SQLite's unit lane drops tzinfo; deployed PostgreSQL retains UTC offsets."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


TEST_RADIUS_PREFIX = "Dotmac-Test-"
TEST_RADIUS_UNTIL = f"{TEST_RADIUS_PREFIX}Until"
TEST_RADIUS_GRANT = f"{TEST_RADIUS_PREFIX}Grant"


@dataclass(frozen=True, slots=True)
class ObservedRadiusAttribute:
    username: str
    attribute: str
    value: str


@dataclass(frozen=True, slots=True)
class EffectiveRadiusObservation:
    rejected: frozenset[str]
    captive: frozenset[str]
    concurrency_check: frozenset[str]
    concurrency_reply: frozenset[str]


def effective_radius_observation(
    *,
    checks: tuple[ObservedRadiusAttribute, ...],
    replies: tuple[ObservedRadiusAttribute, ...],
    evaluated_at: datetime,
    suspended_list: str,
) -> EffectiveRadiusObservation:
    live: set[str] = set()
    for row in checks:
        if (
            row.attribute == TEST_RADIUS_UNTIL
            and row.value.isdigit()
            and int(row.value) > evaluated_at.timestamp()
        ):
            live.add(row.username)

    def selected(
        rows: tuple[ObservedRadiusAttribute, ...],
    ) -> tuple[ObservedRadiusAttribute, ...]:
        return tuple(
            ObservedRadiusAttribute(
                username=row.username,
                attribute=row.attribute.removeprefix(TEST_RADIUS_PREFIX),
                value=row.value,
            )
            for row in rows
            if row.attribute not in {TEST_RADIUS_UNTIL, TEST_RADIUS_GRANT}
            and row.attribute.startswith(TEST_RADIUS_PREFIX) == (row.username in live)
        )

    active_checks, active_replies = selected(checks), selected(replies)
    return EffectiveRadiusObservation(
        rejected=frozenset(
            row.username
            for row in active_checks
            if row.attribute.lower() == "auth-type" and row.value.lower() == "reject"
        ),
        captive=frozenset(
            row.username
            for row in active_replies
            if row.attribute == "Mikrotik-Address-List" and row.value == suspended_list
        ),
        concurrency_check=frozenset(
            row.username
            for row in active_checks
            if row.attribute.lower() == "simultaneous-use"
        ),
        concurrency_reply=frozenset(
            row.username
            for row in active_replies
            if row.attribute.lower() == "simultaneous-use"
        ),
    )
