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
