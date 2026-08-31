"""Naming and safety properties of the template-database mechanism.

These run in the unit lane because they are pure: no database is touched. The
behaviour that needs a real server -- sealing, cloning, sweeping -- is proven in
`tests/integration/test_template_database_contract.py`.
"""

from __future__ import annotations

import pytest

from scripts.ci.migrated_test_database import _DISPOSABLE_DATABASE_TOKEN
from scripts.ci.template_database import (
    _RUN_TOKEN,
    CLONE_PREFIX,
    TEMPLATE_PREFIX,
    clone_database_name,
    template_database_name,
)

REVISION = "467_sla_policy_versions"


def test_a_template_name_is_stable_for_one_revision_within_a_run() -> None:
    """Two tests asking for the same target must share one template.

    If this drifted, every test would build its own template and the mechanism
    would cost MORE than the chain replay it replaces.
    """

    assert template_database_name(REVISION) == template_database_name(REVISION)


def test_distinct_revisions_get_distinct_templates() -> None:
    assert template_database_name("heads") != template_database_name(REVISION)


def test_clone_names_are_unique() -> None:
    names = {clone_database_name() for _ in range(500)}
    assert len(names) == 500


# Parametrise over a FACTORY KEY, never over a generated name. `_RUN_TOKEN` and
# the clone suffix are process-random, so calling the factories at collection
# time gives every pytest-xdist worker a different test id -- and xdist aborts
# the whole shard with "Different tests were collected between workers" rather
# than failing one test. Generate inside the test body instead.
NAME_FACTORIES = {
    "template": lambda: template_database_name(REVISION),
    "clone": clone_database_name,
}


@pytest.mark.parametrize("factory", sorted(NAME_FACTORIES))
def test_generated_names_carry_the_run_token(factory: str) -> None:
    """A concurrent run's databases must be distinguishable from this one's.

    The maintenance command relies on this to leave a live run alone.
    """

    assert f"_{_RUN_TOKEN}_" in NAME_FACTORIES[factory]()


@pytest.mark.parametrize("factory", sorted(NAME_FACTORIES))
def test_generated_names_are_recognisably_disposable(factory: str) -> None:
    """The repository already has a rule for what a throwaway database is.

    `migrated_test_database.parse_test_database_target` refuses a
    `TEST_DATABASE_URL` whose database name lacks a disposable token. Names
    produced here satisfy that same rule, so nothing this module creates could
    be mistaken for -- or accepted as -- a real target.
    """

    assert _DISPOSABLE_DATABASE_TOKEN.search(NAME_FACTORIES[factory]())


def test_prefixes_do_not_shadow_each_other() -> None:
    """The sweep matches on prefix; one must not be a prefix of the other."""

    assert not TEMPLATE_PREFIX.startswith(CLONE_PREFIX)
    assert not CLONE_PREFIX.startswith(TEMPLATE_PREFIX)


def test_a_revision_name_cannot_escape_into_the_identifier() -> None:
    """Revision strings reach an identifier; only `[a-z0-9_]` may survive."""

    hostile = 'heads"; DROP DATABASE postgres; --'
    name = template_database_name(hostile)
    assert name.startswith(TEMPLATE_PREFIX)
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789_")


def test_a_long_revision_name_stays_within_the_identifier_limit() -> None:
    """PostgreSQL truncates identifiers at 63 bytes; a truncated name that
    collided with another template would silently share ONE schema between two
    revision targets while each appeared to have its own."""

    name = template_database_name("x" * 200)
    assert len(name.encode("utf-8")) <= 63


def test_long_revision_names_that_share_a_prefix_get_distinct_templates() -> None:
    """Truncation alone would collide these; the digest suffix is what saves it."""

    shared = "a_very_long_revision_identifier_that_will_not_fit_in_one_identifier"
    first = template_database_name(f"{shared}_alpha")
    second = template_database_name(f"{shared}_beta")
    assert first != second
    assert len(first.encode("utf-8")) <= 63
    assert len(second.encode("utf-8")) <= 63
