"""Guards for the canonical admin customer-search read path."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = PROJECT_ROOT / "alembic/versions/504_customer_search_trigram_indexes.py"


def test_customer_search_indexes_every_previously_unindexed_or_branch() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    # The migration's PARENT is deliberately not asserted. It pinned
    # `502_open_setting_domain_vocabulary`, which made this test fail the moment
    # the revision was renumbered off a duplicate prefix — a chain fact this
    # test does not claim and does not need. Chain integrity is owned by
    # test_migration_chain_assertions and the single-head checks; see that
    # module's docstring on asserting what a test actually means.
    assert '("ix_trgm_subscribers_display_name", "display_name")' in migration
    assert '("ix_trgm_subscribers_phone", "phone")' in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "gin_trgm_ops" in migration
