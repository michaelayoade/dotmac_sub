"""PostgreSQL canaries for the deployed position-observation schema."""

from __future__ import annotations

from sqlalchemy import inspect


def test_migrated_position_observation_schema_is_replay_safe(engine) -> None:
    inspector = inspect(engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("field_tech_location_pings")
    }

    assert "crm_work_order_id" not in columns
    assert "work_order_id" in columns
    assert columns["client_observation_id"]["nullable"] is False
    assert columns["payload_fingerprint"]["nullable"] is False

    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("field_tech_location_pings")
    }
    identity = indexes["ux_field_tech_location_pings_observation_identity"]
    assert identity["unique"] is True
    assert identity["column_names"] == [
        "technician_id",
        "source",
        "client_observation_id",
    ]


def test_migrated_position_collection_grant_is_bounded(engine) -> None:
    inspector = inspect(engine)
    columns = {
        column["name"] for column in inspector.get_columns("field_tech_presence")
    }
    assert {
        "collection_purpose",
        "collection_granted_at",
        "collection_expires_at",
    } <= columns

    checks = {
        constraint["name"]: str(constraint["sqltext"])
        for constraint in inspector.get_check_constraints("field_tech_presence")
    }
    grant_check = checks["ck_field_tech_presence_active_collection_grant"]
    assert "collection_purpose IS NOT NULL" in grant_check
    assert "collection_expires_at > collection_granted_at" in grant_check
    assert "on_break" in checks["ck_field_tech_presence_status"]
