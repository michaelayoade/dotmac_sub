"""CRM client: technician live-location + rating portal proxies (F1 sub side)."""

from app.services.crm_client import CRMClient


def _client():
    return CRMClient("https://crm.example", "user", "pass")


def test_get_portal_technician_location_calls_scoped_endpoint(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_portal_token",
        lambda cid, scopes, actor="subscriber": f"tok:{','.join(scopes)}:{actor}",
    )
    captured: dict = {}

    def _req(method, path, **kw):
        captured.update(method=method, path=path, headers=kw.get("headers"))
        return {"available": True, "latitude": 6.5, "longitude": 3.3}

    monkeypatch.setattr(c, "_request", _req)

    out = c.get_portal_technician_location("sub-1", "wo-9")
    assert out["available"] is True
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/portal/work-orders/wo-9/technician-location"
    assert (
        captured["headers"]["Authorization"] == "Bearer tok:work_orders:read:subscriber"
    )


def test_get_portal_technician_location_reseller_actor(monkeypatch):
    c = _client()
    tokens: list = []
    monkeypatch.setattr(
        c,
        "_portal_token",
        lambda cid, scopes, actor="subscriber": tokens.append(actor) or "tok",
    )
    monkeypatch.setattr(c, "_request", lambda *a, **k: {"available": False})
    # actor threads through to the token mint.
    c.get_portal_technician_location("res-1", "wo-9", actor="reseller")
    assert tokens == ["reseller"]


def test_submit_portal_technician_rating_posts_payload(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c, "_portal_token", lambda cid, scopes, actor="subscriber": "tok"
    )
    captured: dict = {}

    def _req(method, path, **kw):
        captured.update(method=method, path=path, json=kw.get("json_data"))
        return {"ok": True, "already_rated": False, "rating": 5}

    monkeypatch.setattr(c, "_request", _req)

    out = c.submit_portal_technician_rating("sub-1", "wo-9", rating=5, comment="great")
    assert out["ok"] is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/portal/work-orders/wo-9/rate-technician"
    assert captured["json"] == {"rating": 5, "comment": "great"}


def test_submit_rating_omits_empty_comment(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c, "_portal_token", lambda cid, scopes, actor="subscriber": "tok"
    )
    captured: dict = {}

    def _req(method, path, **kw):
        captured.update(json=kw.get("json_data"))
        return {"ok": True}

    monkeypatch.setattr(c, "_request", _req)

    c.submit_portal_technician_rating("sub-1", "wo-9", rating=4)
    assert captured["json"] == {"rating": 4}
