from __future__ import annotations

import importlib.util
from pathlib import Path


def test_capability_sync_migration_is_linear_and_contains_no_secret_material() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/377_integration_capability_sync.py"
    )
    spec = importlib.util.spec_from_file_location("migration_377", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "377_integration_capability_sync"
    assert module.down_revision == "376_integration_platform_foundation"
    source = path.read_text(encoding="utf-8")
    assert "integration_checkpoints" in source
    assert "capability_binding_id" in source
    assert "manifest_digest" in source
    assert "service_token" not in source


def test_sync_dispatcher_has_no_hard_coded_crm_action_branch() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app/services/integration_sync.py"
    ).read_text(encoding="utf-8")

    assert 'if adapter_key == "crm"' not in source
    assert "_SYNC_CAPABILITY_HANDLERS" in source
    assert "_LEGACY_CAPABILITY_MIGRATORS" not in source
    assert "CRMClient" not in source
    assert "shadow_parity" not in source


def test_run_sync_job_fails_closed_without_a_capability_binding() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    import pytest

    from app.services import integration_sync

    job = SimpleNamespace(capability_binding=None)
    with pytest.raises(integration_sync.SyncAdapterError, match="no capability"):
        integration_sync.run_sync_job(None, job, uuid4())  # type: ignore[arg-type]


def test_run_sync_job_fails_closed_for_the_retired_crm_ticket_capability() -> None:
    """A job still bound to the retired capability (production has one) must
    raise rather than silently succeed; the caller records the run as failed."""
    from types import SimpleNamespace
    from uuid import uuid4

    import pytest

    from app.services import integration_sync

    job = SimpleNamespace(
        capability_binding=SimpleNamespace(capability_id="crm.ticket_observation.v1")
    )
    with pytest.raises(integration_sync.SyncAdapterError, match="No sync handler"):
        integration_sync.run_sync_job(None, job, uuid4())  # type: ignore[arg-type]
