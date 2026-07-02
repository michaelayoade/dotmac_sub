"""Router config export uses POST /rest/export and normalises the response.

Regression for the keystone snapshot bug: `GET /rest/export` returns
`400 "no such command"` — `/export` is a RouterOS command and must be POSTed.
"""

from __future__ import annotations

from unittest.mock import patch

from app.tasks import router_sync


def test_export_to_text_normalises_shapes():
    assert router_sync._export_to_text("/interface\n/ip") == "/interface\n/ip"
    # POST /export commonly returns a JSON array of config lines.
    assert (
        router_sync._export_to_text(["/interface", "/ip address"])
        == "/interface\n/ip address"
    )
    # list of dicts (some builds) -> JSON per line, never a Python repr
    out = router_sync._export_to_text([{"a": 1}])
    assert out == '{"a": 1}'
    assert router_sync._export_to_text({"ret": "x"}) == '{"ret": "x"}'
    assert router_sync._export_to_text([]) == ""


def test_fetch_config_export_uses_post(monkeypatch):
    # Config export now defaults to SSH; pin this REST-path test to the fallback.
    import types

    monkeypatch.setattr(
        router_sync,
        "settings",
        types.SimpleNamespace(router_config_export_via_ssh=False),
    )
    captured = {}

    def _fake_execute(router, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        return ["/system identity set name=r1"]

    with patch.object(
        router_sync.RouterConnectionService, "execute", staticmethod(_fake_execute)
    ):
        text = router_sync._fetch_config_export(types.SimpleNamespace(name="r1"))

    assert (
        captured["method"] == "POST"
    )  # not GET — GET /rest/export is "no such command"
    assert captured["path"] == "/export"
    assert text == "/system identity set name=r1"
