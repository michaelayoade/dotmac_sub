from __future__ import annotations

import httpx

from scripts.setup_genieacs import GenieACSSetup


def test_setup_genieacs_deploy_config_reports_404_as_error(monkeypatch) -> None:
    setup = GenieACSSetup("http://genieacs.example", dry_run=False)

    def fake_request(method, path, **kwargs):
        request = httpx.Request(method, f"http://genieacs.example{path}")
        response = httpx.Response(404, request=request, text="not found")
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(setup.client, "request", fake_request)

    results = setup.deploy_config()
    setup.close()

    assert results["cwmp.auth"].startswith("error:")
    assert results["cwmp.connectionRequestAuth"].startswith("error:")


def test_setup_genieacs_prunes_legacy_objects(monkeypatch) -> None:
    setup = GenieACSSetup("http://genieacs.example", dry_run=False)
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        request = httpx.Request(method, f"http://genieacs.example{path}")
        return httpx.Response(200, request=request)

    monkeypatch.setattr(setup.client, "request", fake_request)

    results = setup.prune_legacy_objects()
    setup.close()

    assert results == {
        "preset:dotmac-inform-webhook": "deleted",
        "preset:dotmac-runtime-collect": "deleted",
        "provision:dotmac-inform-webhook": "deleted",
        "provision:dotmac-runtime-collect": "deleted",
        "provision:full-refresh": "deleted",
    }
    assert calls == [
        ("DELETE", "/presets/dotmac-inform-webhook"),
        ("DELETE", "/presets/dotmac-runtime-collect"),
        ("DELETE", "/provisions/dotmac-inform-webhook"),
        ("DELETE", "/provisions/dotmac-runtime-collect"),
        ("DELETE", "/provisions/full-refresh"),
    ]
