"""Keep every Inbox message substring predicate supported by an index."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READ_OWNER = ROOT / "app/services/team_inbox_read.py"
MIGRATION = ROOT / "alembic/versions/712_inbox_message_search_trigram_indexes.py"


def test_inbox_search_indexes_cover_every_message_predicate() -> None:
    owner = READ_OWNER.read_text(encoding="utf-8")
    search = owner.split("matching_message_conversation_ids = [", 1)[1].split(
        "matching_comment_conversation_ids = [", 1
    )[0]
    columns = set(re.findall(r"InboxMessage\.(\w+)\.ilike\(like\)", search))
    metadata_keys = set(
        re.findall(
            r'InboxMessage\.metadata_\["(\w+)"\]\.as_string\(\)\.ilike\(like\)',
            search,
        )
    )
    assert columns == {"subject", "body", "from_address"}
    assert metadata_keys == {
        "from_name",
        "contact_name",
        "sender_name",
        "profile_name",
    }

    migration = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(migration)
    assignments = (
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_INDEXES"
    )
    index_nodes = next(assignments).value
    assert isinstance(index_nodes, ast.Tuple)
    indexed_columns: set[str] = set()
    indexed_metadata: set[str] = set()
    for node in index_nodes.elts:
        assert isinstance(node, ast.Call)
        options = {
            item.arg: ast.literal_eval(item.value)
            for item in node.keywords
            if item.arg is not None
        }
        if column := options.get("column"):
            indexed_columns.add(column)
        if key := options.get("metadata_key"):
            indexed_metadata.add(key)
            assert f"CAST((metadata ->> '{key}') AS VARCHAR)" in ast.literal_eval(
                node.args[1]
            )
    assert indexed_columns == columns
    assert indexed_metadata == metadata_keys
    assert "CREATE INDEX CONCURRENTLY" in migration
    assert "gin_trgm_ops" in migration
    assert "_index_state(bind, index)" in migration
