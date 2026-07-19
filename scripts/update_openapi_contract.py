#!/usr/bin/env python3
"""Regenerate tests/architecture/openapi_contract_surface.json.

Run after an intentional /api/v1 contract change, then review the manifest
diff in your commit — the diff IS the contract-change review artifact.

Usage: python scripts/update_openapi_contract.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _set_test_env() -> None:
    """Mirror tests/conftest.py's import-time env so app.main imports cleanly
    without touching the deployment .env, a real database, or Redis."""
    os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"
    os.environ["SESSION_REDIS_URL"] = "redis://127.0.0.1:9/0"
    os.environ["CELERY_BROKER_URL"] = "memory://"
    os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "false"
    os.environ["GLITCHTIP_ENABLED"] = "false"
    os.environ["GLITCHTIP_DSN"] = ""
    os.environ["OTEL_ENABLED"] = "false"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
    os.environ["OPENBAO_ADDR"] = ""
    os.environ["OPENBAO_TOKEN"] = ""
    os.environ["VAULT_ADDR"] = ""
    os.environ["VAULT_TOKEN"] = ""
    os.environ["DATABASE_URL"] = (
        "postgresql+psycopg://postgres:postgres@127.0.0.1:9/dotmac_sub_test"
        "?connect_timeout=1"
    )
    os.environ["RADIUS_DB_DSN"] = ""
    os.environ["RADIUS_DB_HOST"] = ""
    os.environ["RADIUS_SYNC_DB_URL"] = ""


def main() -> None:
    _set_test_env()
    from tests.architecture import openapi_contract_lib as lib

    surface = lib.compute_surface(lib.build_full_app())
    lib.write_manifest(surface)
    print(
        f"wrote {lib.MANIFEST_PATH.relative_to(REPO_ROOT)}: "
        f"{len(surface['routes'])} routes, {len(surface['schemas'])} schemas"
    )


if __name__ == "__main__":
    main()
