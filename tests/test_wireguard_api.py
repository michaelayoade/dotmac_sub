from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from app.api import wireguard as wireguard_api


def _peer(subscriber_id: str) -> SimpleNamespace:
    return SimpleNamespace(subscriber_id=subscriber_id)


def test_download_peer_config_allows_admin_role(monkeypatch):
    peer = _peer("peer-owner")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)
    monkeypatch.setattr(
        wireguard_api.wg_service.wg_peers,
        "generate_peer_config",
        lambda _db, _peer_id: SimpleNamespace(
            config_content="[Interface]\nPrivateKey = secret\n",
            filename="wg-peer.conf",
        ),
    )

    response = wireguard_api.download_peer_config(
        uuid4(),
        db=object(),
        current_user={"roles": ["admin"], "subscriber_id": "someone-else"},
    )

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 200
    assert "attachment;" in response.headers["content-disposition"]


def test_download_peer_config_allows_owner_subscriber(monkeypatch):
    peer = _peer("owner-1")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)
    monkeypatch.setattr(
        wireguard_api.wg_service.wg_peers,
        "generate_peer_config",
        lambda _db, _peer_id: SimpleNamespace(
            config_content="[Interface]\nPrivateKey = secret\n",
            filename="wg-peer.conf",
        ),
    )

    response = wireguard_api.download_peer_config(
        uuid4(),
        db=object(),
        current_user={"roles": [], "subscriber_id": "owner-1", "principal_id": "another-id"},
    )

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 200


def test_download_peer_config_denies_non_owner(monkeypatch):
    peer = _peer("owner-1")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)

    def _should_not_run(_db, _peer_id):
        raise AssertionError("config generation should be blocked for non-owners")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "generate_peer_config", _should_not_run)

    with pytest.raises(HTTPException) as exc:
        wireguard_api.download_peer_config(
            uuid4(),
            db=object(),
            current_user={"roles": [], "subscriber_id": "not-owner", "principal_id": "also-not-owner"},
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied"


def test_download_mikrotik_script_allows_operator_role(monkeypatch):
    peer = _peer("peer-owner")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)
    monkeypatch.setattr(
        wireguard_api.wg_service.wg_mikrotik,
        "generate_script",
        lambda _db, _peer_id: SimpleNamespace(
            script_content="/interface/wireguard/add name=wg-test",
            filename="wg-peer.rsc",
        ),
    )

    response = wireguard_api.download_mikrotik_script(
        uuid4(),
        db=object(),
        current_user={"roles": ["operator"], "subscriber_id": "not-owner"},
    )

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 200
    assert "attachment;" in response.headers["content-disposition"]


def test_download_mikrotik_script_allows_owner_principal(monkeypatch):
    peer = _peer("owner-principal")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)
    monkeypatch.setattr(
        wireguard_api.wg_service.wg_mikrotik,
        "generate_script",
        lambda _db, _peer_id: SimpleNamespace(
            script_content="/interface/wireguard/add name=wg-test",
            filename="wg-peer.rsc",
        ),
    )

    response = wireguard_api.download_mikrotik_script(
        uuid4(),
        db=object(),
        current_user={
            "roles": [],
            "subscriber_id": "different-subscriber",
            "principal_id": "owner-principal",
        },
    )

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 200


def test_download_mikrotik_script_denies_non_owner(monkeypatch):
    peer = _peer("owner-1")

    monkeypatch.setattr(wireguard_api.wg_service.wg_peers, "get", lambda _db, _peer_id: peer)

    def _should_not_run(_db, _peer_id):
        raise AssertionError("script generation should be blocked for non-owners")

    monkeypatch.setattr(wireguard_api.wg_service.wg_mikrotik, "generate_script", _should_not_run)

    with pytest.raises(HTTPException) as exc:
        wireguard_api.download_mikrotik_script(
            uuid4(),
            db=object(),
            current_user={
                "roles": [],
                "subscriber_id": "not-owner",
                "principal_id": "also-not-owner",
            },
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied"
