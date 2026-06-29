"""Connector header/metadata secrets are masked on display, kept on save."""

from __future__ import annotations

import json

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.services import web_integrations as svc


def test_mask_secret_values():
    masked = svc.mask_secret_values(
        {"Authorization": "Bearer xyz", "X-Api-Key": "k", "Content-Type": "json"}
    )
    assert masked["Authorization"] == svc.SECRET_VALUE_SENTINEL
    assert masked["X-Api-Key"] == svc.SECRET_VALUE_SENTINEL
    assert masked["Content-Type"] == "json"  # non-secret untouched
    assert svc.mask_secret_values(None) == {}


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

    # Simulate the edit form: secret masked, a non-secret edited, then saved.
    masked = svc.mask_secret_values(cfg.headers)
    masked["Content-Type"] = "text/plain"

    updated = svc.update_connector_config(
        db_session,
        str(cfg.id),
        base_url=None,
        auth_type="none",
        timeout_sec=None,
        auth_config=None,
        headers=json.dumps(masked),
        retry_policy=None,
        metadata=None,
        notes=None,
        is_active=True,
    )

    assert updated.headers["Authorization"] == "Bearer xyz"  # secret preserved
    assert updated.headers["Content-Type"] == "text/plain"  # edit applied
