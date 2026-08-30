"""Fail-closed policy for concurrent deploys and orphaned `pg_dump` processes.

Extracted from `scripts/deploy.sh` so the knowledge survives the move to the
deployment controller. **This module changes nothing today** — `deploy.sh` is
frozen ahead of the first controller deployment and still carries its own bash
copy. This is the typed behaviour the Foundation preconditions and effects bind
to at adoption, ported with its parity tests rather than reimplemented from the
comments.

Three separate pieces of knowledge, each with an incident or a near-miss behind
it. They are separate on purpose; collapsing them loses the reason each exists.

**1. One deploy at a time.** On 2026-07-12 two deploys ran concurrently, each
started a full `pg_dump` of production, load hit 52 on 16 cores, the app was
starved out and the site served 502s for about ten minutes. Had both reached
`alembic upgrade heads` they would have raced the migration chain against one
database.

**2. A missing `flock` binary must fail closed, and say which thing failed.**
In bash, `! flock` succeeds when the binary is absent, so the script would
report that another deploy holds the lock and send an operator hunting a
process that does not exist. A wrong diagnosis costs more than a refusal.

**3. The lock does not catch an orphaned dump.** A deploy killed mid-backup —
a dropped SSH session is enough — releases the lock when it dies, but the
`pg_dump` it started keeps running and keeps hammering the database. So the
orphan check is a SEPARATE precondition, not a consequence of the lock.

The cleanup effect carries its own detail: `pg_dump` runs inside the database
container via `docker exec` and survives the exec client, so terminating the
local child is not enough — it has to be signalled inside the container too.

## Why the process list is injected

The bash calls `pgrep` directly, which is why none of this has ever been
tested. `DumpGuardEvidence` takes the process listing as data so the refusal
can be exercised against a constructed process table. A precondition that has
never been observed refusing is indistinguishable from one that cannot refuse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

#: Default database name, matching `${DB_NAME:-dotmac_sub}` in deploy.sh.
DEFAULT_DATABASE_NAME = "dotmac_sub"


class DumpGuardIssue(str, Enum):
    """Stable reasons a deploy must not start."""

    FLOCK_UNAVAILABLE = "flock_unavailable"
    CONCURRENT_DEPLOY_HOLDS_LOCK = "concurrent_deploy_holds_lock"
    ORPHANED_DUMP_RUNNING = "orphaned_dump_running"


@dataclass(frozen=True, slots=True)
class DumpGuardEvidence:
    """What the host looks like at the moment a deploy is about to start."""

    flock_available: bool
    lock_acquired: bool
    database_name: str = DEFAULT_DATABASE_NAME
    #: Full command lines, as `pgrep -af` would report them.
    running_processes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DumpGuardDecision:
    """Whether the deploy may proceed, and why not."""

    may_deploy: bool
    issues: tuple[DumpGuardIssue, ...]
    #: The offending command lines, so the refusal names what to go and kill.
    blocking_processes: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return not self.may_deploy


@dataclass(frozen=True, slots=True)
class DumpCleanupStep:
    """One action taken when a deploy is interrupted mid-backup."""

    target: str
    reason: str


def dumps_against(database_name: str, processes: Sequence[str]) -> tuple[str, ...]:
    """Command lines that are dumping `database_name`.

    Mirrors deploy.sh's `pgrep -f "pg_dump .*-d ${DB_NAME}"`. The match is
    deliberately as loose as the original: `-d dotmac_sub` also matches
    `-d dotmac_sub_test`, so a dump of a differently named database can refuse
    a deploy. That errs toward refusing, which is the safe direction here, and
    `test_a_dump_of_a_similarly_named_database_also_refuses` pins the behaviour
    so a future tightening is a deliberate change rather than a silent one.
    """

    pattern = re.compile(rf"pg_dump .*-d {re.escape(database_name)}")
    return tuple(line for line in processes if pattern.search(line))


def resolve_dump_guard(evidence: DumpGuardEvidence) -> DumpGuardDecision:
    """Decide whether a deploy may start. Fail closed on every unknown."""

    issues: list[DumpGuardIssue] = []

    if not evidence.flock_available:
        # Reported on its own, never as "another deploy holds the lock" — see
        # the module docstring.
        issues.append(DumpGuardIssue.FLOCK_UNAVAILABLE)
    elif not evidence.lock_acquired:
        issues.append(DumpGuardIssue.CONCURRENT_DEPLOY_HOLDS_LOCK)

    blocking = dumps_against(evidence.database_name, evidence.running_processes)
    if blocking:
        # Checked even when the lock was acquired: the deploy that started this
        # dump is dead, and released the lock when it died.
        issues.append(DumpGuardIssue.ORPHANED_DUMP_RUNNING)

    return DumpGuardDecision(
        may_deploy=not issues,
        issues=tuple(issues),
        blocking_processes=blocking,
    )


def plan_dump_cleanup(
    *, backup_pid: int | None, database_container: str
) -> tuple[DumpCleanupStep, ...]:
    """The effect an interrupted deploy must run before it exits.

    Terminating the local child is not sufficient. `pg_dump` runs inside the
    database container via `docker exec`; it outlives the exec client, so it
    has to be signalled inside the container as well.
    """

    steps: list[DumpCleanupStep] = []
    if backup_pid is not None:
        steps.append(
            DumpCleanupStep(
                target=f"pid:{backup_pid}",
                reason="terminate the backup child started by this deploy",
            )
        )
    steps.append(
        DumpCleanupStep(
            target=f"container:{database_container}",
            reason=(
                "pg_dump runs inside the database container via docker exec and "
                "survives the exec client, so it must be signalled there too"
            ),
        )
    )
    return tuple(steps)
