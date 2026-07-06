"""P4: the four mirror reconcilers share one portal token per subscriber.

Each get_portal_* read used to mint its own single-scope token, so a subscriber
stale in all four mirrors cost four mints (~8 HTTP calls/cycle). Now the reads
share one cached read-union token per subscriber — one mint, four GETs.
"""

from __future__ import annotations

import time

from app.services.crm_client import CRMClient


def _client() -> CRMClient:
    return CRMClient(base_url="http://crm", username="u", password="p")


def test_four_reads_share_one_minted_token(monkeypatch):
    client = _client()
    mints = {"n": 0}
    gets = {"tokens": []}

    def fake_mint(*, crm_subscriber_id, actor, scopes):
        mints["n"] += 1
        # one token carrying the union of read scopes
        assert set(scopes) == set(CRMClient._PORTAL_READ_SCOPES)
        return {"portal_token": "TKN", "expires_at": time.time() + 300}

    def fake_request(method, path, headers=None, **_kw):
        gets["tokens"].append(headers.get("Authorization") if headers else None)
        return {}

    monkeypatch.setattr(client, "create_portal_session", fake_mint)
    monkeypatch.setattr(client, "_request", fake_request)

    client.get_portal_referrals("sub-1")
    client.get_portal_projects("sub-1")
    client.get_portal_work_orders("sub-1")
    client.get_portal_quotes("sub-1")

    assert mints["n"] == 1  # one mint, reused across all four reads
    assert gets["tokens"] == ["Bearer TKN"] * 4


def test_token_re_minted_after_expiry(monkeypatch):
    client = _client()
    mints = {"n": 0}

    def fake_mint(**_kw):
        mints["n"] += 1
        return {"portal_token": f"TKN{mints['n']}", "expires_at": time.time() + 300}

    monkeypatch.setattr(client, "create_portal_session", fake_mint)
    monkeypatch.setattr(client, "_request", lambda *a, **k: {})

    client.get_portal_referrals("sub-1")
    assert mints["n"] == 1
    # force the cached token past expiry
    client._portal_read_tokens[("sub-1", "subscriber")] = ("stale", time.time() - 1)
    client.get_portal_projects("sub-1")
    assert mints["n"] == 2


def test_distinct_subscribers_do_not_share(monkeypatch):
    client = _client()
    mints = {"n": 0}

    def fake_mint(**_kw):
        mints["n"] += 1
        return {"portal_token": f"T{mints['n']}", "expires_at": time.time() + 300}

    monkeypatch.setattr(client, "create_portal_session", fake_mint)
    monkeypatch.setattr(client, "_request", lambda *a, **k: {})

    client.get_portal_referrals("sub-1")
    client.get_portal_referrals("sub-2")
    assert mints["n"] == 2
