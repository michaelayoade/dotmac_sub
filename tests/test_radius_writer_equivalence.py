from __future__ import annotations

import sqlite3

from app.services import radius as radius_service
from app.services import radius_dsn
from app.services.radius_writer_equivalence import (
    assess_radius_writer_equivalence,
    database_identity,
)


def _radius_db(tmp_path, name: str, *, group_row: bool = False) -> str:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
        )
        if group_row:
            conn.execute(
                "INSERT INTO radusergroup VALUES ('subscriber-1', 'full-speed', 0)"
            )
        conn.commit()
    finally:
        conn.close()
    return f"sqlite+pysqlite:///{path}"


def _config(dsn: str, *, use_group: bool = False) -> dict:
    return {
        "db_url": dsn,
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "use_group": use_group,
    }


def test_database_identity_redacts_credentials():
    identity = database_identity(
        "postgresql+psycopg://radius-user:secret@radius-db:5432/radius"
    )

    assert identity is not None
    assert identity.label == "radius-db:5432/radius"
    assert "secret" not in identity.label
    assert "radius-user" not in identity.label


def test_matching_target_with_compatible_schema_is_move_ready(
    db_session, tmp_path, monkeypatch
):
    dsn = _radius_db(tmp_path, "radius-ready.db")
    monkeypatch.setattr(radius_dsn, "resolve_radius_dsn", lambda: dsn)
    monkeypatch.setattr(
        radius_service,
        "_active_external_sync_configs",
        lambda db: [_config(dsn)],
    )

    report = assess_radius_writer_equivalence(db_session)

    assert report.all_targets_match_canonical is True
    assert report.schema_contract_ok is True
    assert report.group_semantics_required is False
    assert report.ready_for_single_owner is True


def test_different_database_target_blocks_writer_move(
    db_session, tmp_path, monkeypatch
):
    canonical = _radius_db(tmp_path, "radius-canonical.db")
    configured = _radius_db(tmp_path, "radius-configured.db")
    monkeypatch.setattr(radius_dsn, "resolve_radius_dsn", lambda: canonical)
    monkeypatch.setattr(
        radius_service,
        "_active_external_sync_configs",
        lambda db: [_config(configured)],
    )

    report = assess_radius_writer_equivalence(db_session, probe_schema=False)

    assert report.unique_target_count == 1
    assert report.all_targets_match_canonical is False
    assert report.ready_for_single_owner is False


def test_existing_group_projection_blocks_writer_move(
    db_session, tmp_path, monkeypatch
):
    dsn = _radius_db(tmp_path, "radius-groups.db", group_row=True)
    monkeypatch.setattr(radius_dsn, "resolve_radius_dsn", lambda: dsn)
    monkeypatch.setattr(
        radius_service,
        "_active_external_sync_configs",
        lambda db: [_config(dsn)],
    )

    report = assess_radius_writer_equivalence(db_session)

    assert report.schema_contract_ok is True
    assert report.targets[0].group_rows_present is True
    assert report.group_semantics_required is True
    assert report.ready_for_single_owner is False
