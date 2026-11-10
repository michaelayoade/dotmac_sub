"""Saved connector header values are hidden and preserved on edit."""

from __future__ import annotations

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.services import web_integrations as svc


def test_unmask_restores_sentinel_keeps_edits():
    stored = {"Authorization": "Bearer xyz", "Content-Type": "json"}
    submitted = {"Authorization": svc.SECRET_VALUE_SENTINEL, "Content-Type": "text"}
    assert svc._unmask_secret_values(submitted, stored) == {
        "Authorization": "Bearer xyz",  # restored from stored
        "Content-Type": "text",  # operator edit kept
    }


def test_update_preserves_masked_secret_header(db_session):
    cfg = ConnectorConfig(
        name="mask-test",
        connector_type=ConnectorType.http,
        auth_type=ConnectorAuthType.none,
        headers={"Authorization": "Bearer xyz", "Content-Type": "application/json"},
    )
    db_session.add(cfg)
    db_session.commit()

    # Every saved header value is masked, regardless of its name.
    rows = svc.connector_edit_form(cfg)["header_rows"]
    masked = {row["name"]: row["value"] for row in rows}
    assert set(masked.values()) == {svc.SECRET_VALUE_SENTINEL}
    masked["Content-Type"] = "text/plain"

    updated = svc.update_connector_config(
        db_session,
        str(cfg.id),
        svc.ConnectorFormCommand(
            auth_type=ConnectorAuthType.none,
            base_url=None,
            timeout_sec=None,
            username=None,
            password=None,
            token=None,
            api_key=None,
            secret=None,
            client_id=None,
            client_secret=None,
            header_names=tuple(masked),
            header_values=tuple(masked.values()),
            retry_max_attempts=None,
            retry_backoff_sec=None,
            notes=None,
            is_active=True,
        ),
    )

    assert updated.headers["Authorization"] == "Bearer xyz"  # secret preserved
    assert updated.headers["Content-Type"] == "text/plain"  # edit applied
