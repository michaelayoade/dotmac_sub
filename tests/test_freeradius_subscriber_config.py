"""Pin the FreeRADIUS side of subscriber concurrency enforcement."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_subscriber_virtual_server_runs_sql_session_checks() -> None:
    site = (PROJECT_ROOT / "config/freeradius/sites-enabled/default").read_text(
        encoding="utf-8"
    )

    session_block = site.split("session {", maxsplit=1)[1].split("}", maxsplit=1)[0]
    assert "sql" in session_block.split()


def test_subscriber_sql_counts_only_open_accounting_sessions() -> None:
    sql_module = (PROJECT_ROOT / "config/freeradius/mods-enabled/sql").read_text(
        encoding="utf-8"
    )

    assert "simul_count_query" in sql_module
    assert "acctstoptime IS NULL" in sql_module
