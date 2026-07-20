from __future__ import annotations

from pathlib import Path

import httpx

from app.services.genieacs_config import GENIEACS_CONFIG_ENTRIES
from app.tasks.tr069 import setup_genieacs
from scripts.network.setup_genieacs import CONFIG_ENTRIES


def test_setup_script_exports_authoritative_config_mapping() -> None:
    assert CONFIG_ENTRIES is GENIEACS_CONFIG_ENTRIES


def test_setup_task_deploys_authoritative_config_mapping(monkeypatch) -> None:
    deployed: dict[str, str] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, _path: str) -> FakeResponse:
            return FakeResponse()

        def put(self, path: str, *, json: dict[str, str]) -> FakeResponse:
            deployed[path.removeprefix("/config/")] = json["value"]
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = setup_genieacs(
        base_url="http://genieacs.example",
        provisions=False,
        virtual_params=False,
        presets=False,
        config=True,
    )

    assert result["config"] == dict.fromkeys(GENIEACS_CONFIG_ENTRIES, "deployed")
    assert deployed == dict(GENIEACS_CONFIG_ENTRIES)


def test_compose_does_not_override_managed_genieacs_auth_config() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()

    assert "GENIEACS_CWMP_CONNECTION_REQUEST_AUTH" not in compose
