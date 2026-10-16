"""Migration-source graph helpers; these do not prove deployed schema state."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.ci.migrated_test_database import install_migration_graph_environment


def composed_sub_graph(root: Path) -> tuple[ScriptDirectory, tuple[str, ...]]:
    """Load real module dependencies and identify only Sub's lineage heads."""
    install_migration_graph_environment()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    script = ScriptDirectory.from_config(config)
    versions = (root / "alembic" / "versions").resolve()
    heads = tuple(
        head
        for head in script.get_heads()
        if (revision := script.get_revision(head)) is not None
        and Path(revision.path).resolve().parent == versions
    )
    return script, heads
