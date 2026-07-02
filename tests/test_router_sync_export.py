"""Router config export uses POST /rest/export and normalises the response.

Regression for the keystone snapshot bug: `GET /rest/export` returns
`400 "no such command"` — `/export` is a RouterOS command and must be POSTed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def test_fetch_config_export_uses_post():
    captured = {}

    def _fake_execute(router, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        return ["/system identity set name=r1"]

    with patch.object(
        router_sync.RouterConnectionService, "execute", staticmethod(_fake_execute)
    ):
        text = router_sync._fetch_config_export(object())

    assert (
        captured["method"] == "POST"
    )  # not GET — GET /rest/export is "no such command"
    assert captured["path"] == "/export"
    assert text == "/system identity set name=r1"


def test_fetch_config_export_rejects_empty():
    # RouterOS returns [] / "" for a REST user without the 'sensitive' policy.
    # A zero-length snapshot must be a capture failure, never a stored "backup".
    for empty in ([], "", "   ", None):
        with patch.object(
            router_sync.RouterConnectionService,
            "execute",
            staticmethod(lambda *a, _e=empty, **k: _e),
        ):
            with pytest.raises(RuntimeError, match="empty config export"):
                router_sync._fetch_config_export(SimpleNamespace(name="r1"))
