"""Parity and sensitivity for the extracted deploy dump guard.

`scripts/deploy.sh` has carried these refusals since 2026-07-12 and **none of
them has ever been exercised** — the bash calls `pgrep` directly, so there was
nothing a test could drive. That is the gap this closes before the behaviour
moves to the deployment controller: a precondition nobody has watched refuse is
indistinguishable from one that cannot refuse.

Every test here constructs the failing host state and asserts the refusal.
"""

from __future__ import annotations

from scripts.deploy_dump_guard import (
    DEFAULT_DATABASE_NAME,
    DumpGuardEvidence,
    DumpGuardIssue,
    dumps_against,
    plan_dump_cleanup,
    resolve_dump_guard,
)

ORPHANED_DUMP = "31337 pg_dump -U dotmac -d dotmac_sub --no-owner --no-privileges"
UNRELATED = "1024 postgres: autovacuum launcher"


def _evidence(**overrides: object) -> DumpGuardEvidence:
    base: dict[str, object] = {"flock_available": True, "lock_acquired": True}
    base.update(overrides)
    return DumpGuardEvidence(**base)  # type: ignore[arg-type]


# ── the happy path, so the refusals below mean something ─────────────────────


def test_a_clean_host_may_deploy() -> None:
    decision = resolve_dump_guard(_evidence(running_processes=(UNRELATED,)))
    assert decision.may_deploy
    assert decision.issues == ()
    assert decision.blocking_processes == ()


# ── sensitivity: each refusal, driven ────────────────────────────────────────


def test_an_orphaned_dump_refuses_the_deploy() -> None:
    """The 2026-07-12 shape: a dump from a dead deploy still hammering the DB."""

    decision = resolve_dump_guard(
        _evidence(running_processes=(UNRELATED, ORPHANED_DUMP))
    )
    assert decision.refused
    assert DumpGuardIssue.ORPHANED_DUMP_RUNNING in decision.issues
    assert decision.blocking_processes == (ORPHANED_DUMP,), (
        "the refusal must name the offending command line — an operator has to "
        "go and kill it, and a refusal that does not say which process is a "
        "hunt rather than a remedy"
    )


def test_an_orphaned_dump_refuses_even_when_the_lock_was_acquired() -> None:
    """The whole reason this is a separate precondition.

    The deploy that started the dump is dead, and released the lock when it
    died. A guard that only checked the lock would wave this through.
    """

    decision = resolve_dump_guard(
        _evidence(lock_acquired=True, running_processes=(ORPHANED_DUMP,))
    )
    assert decision.refused
    assert decision.issues == (DumpGuardIssue.ORPHANED_DUMP_RUNNING,)


def test_a_missing_flock_refuses_on_its_own_terms() -> None:
    """Never reported as "another deploy holds the lock".

    In bash `! flock` succeeds when the binary is absent, so the original
    failure mode was a correct refusal with a wrong reason — sending an
    operator to hunt a deploy process that does not exist.
    """

    decision = resolve_dump_guard(_evidence(flock_available=False, lock_acquired=False))
    assert decision.refused
    assert decision.issues == (DumpGuardIssue.FLOCK_UNAVAILABLE,)
    assert DumpGuardIssue.CONCURRENT_DEPLOY_HOLDS_LOCK not in decision.issues


def test_a_held_lock_refuses_as_a_concurrent_deploy() -> None:
    decision = resolve_dump_guard(_evidence(lock_acquired=False))
    assert decision.refused
    assert decision.issues == (DumpGuardIssue.CONCURRENT_DEPLOY_HOLDS_LOCK,)


def test_both_conditions_are_reported_together() -> None:
    """A refusal should not hide the second reason behind the first."""

    decision = resolve_dump_guard(
        _evidence(lock_acquired=False, running_processes=(ORPHANED_DUMP,))
    )
    assert set(decision.issues) == {
        DumpGuardIssue.CONCURRENT_DEPLOY_HOLDS_LOCK,
        DumpGuardIssue.ORPHANED_DUMP_RUNNING,
    }


# ── specificity: what must NOT refuse ────────────────────────────────────────


def test_a_dump_of_another_database_does_not_refuse() -> None:
    other = "31338 pg_dump -U dotmac -d dotmac_radius"
    assert dumps_against(DEFAULT_DATABASE_NAME, (other,)) == ()
    assert resolve_dump_guard(_evidence(running_processes=(other,))).may_deploy


def test_a_process_merely_mentioning_the_database_does_not_refuse() -> None:
    mention = "31339 psql -d dotmac_sub -c 'select 1'"
    assert dumps_against(DEFAULT_DATABASE_NAME, (mention,)) == ()


# ── parity: the loose match is inherited deliberately ────────────────────────


def test_a_dump_of_a_similarly_named_database_also_refuses() -> None:
    """Pinned as inherited behaviour, not endorsed as correct.

    deploy.sh matches `pg_dump .*-d dotmac_sub`, which also matches
    `dotmac_sub_test`. It errs toward refusing, which is the safe direction, so
    the port keeps it — but pinned here so tightening it later is a deliberate
    change with a failing test, rather than a silent behaviour drift during the
    controller migration.
    """

    lookalike = "31340 pg_dump -U dotmac -d dotmac_sub_test"
    assert dumps_against(DEFAULT_DATABASE_NAME, (lookalike,)) == (lookalike,)


# ── the cleanup effect ───────────────────────────────────────────────────────


def test_cleanup_always_signals_inside_the_database_container() -> None:
    """The detail most likely to be lost in a reimplementation.

    `pg_dump` runs via `docker exec` and outlives the exec client, so killing
    the local child is not enough.
    """

    steps = plan_dump_cleanup(backup_pid=4242, database_container="dotmac_pg_local")
    targets = [step.target for step in steps]
    assert "pid:4242" in targets
    assert "container:dotmac_pg_local" in targets


def test_cleanup_signals_the_container_even_with_no_local_child() -> None:
    """An interrupt before the child was recorded still leaves a dump running."""

    steps = plan_dump_cleanup(backup_pid=None, database_container="dotmac_pg_local")
    assert [step.target for step in steps] == ["container:dotmac_pg_local"]
