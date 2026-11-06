from pathlib import Path

from app.models.team_inbox import InboxMessage


def test_latest_visible_message_index_matches_the_correlated_lookup() -> None:
    index = next(
        item
        for item in InboxMessage.__table__.indexes
        if item.name == "ix_inbox_messages_latest_visible"
    )
    expressions = " ".join(str(item) for item in index.expressions).lower()

    assert "conversation_id" in expressions
    assert "coalesce(received_at, sent_at, created_at) desc" in expressions
    assert "created_at desc" in expressions
    assert "id desc" in expressions
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "direction <> 'internal'"
    )
    assert tuple(index.dialect_options["postgresql"]["include"]) == ("direction",)


def test_latest_visible_message_index_migration_is_online_and_bounded() -> None:
    migration = Path(
        "alembic/versions/709_inbox_latest_visible_message_index.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "708_vendor_supplier_flow"' in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "SET lock_timeout = '5s'" in migration
    assert "SET statement_timeout = '15min'" in migration
    assert "COALESCE(received_at, sent_at, created_at) DESC" in migration
    assert "INCLUDE (direction) WHERE direction <> 'internal'" in migration
