"""Nextcloud Talk OCS API client."""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.services.common import coerce_uuid
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0  # fallback when settings unavailable


def get_nextcloud_talk_timeout(db: Session | None = None) -> float:
    """Get the Nextcloud Talk API timeout from settings."""
    timeout = resolve_value(db, SettingDomain.comms, "nextcloud_talk_timeout_seconds") if db else None
    if timeout is None:
        return _DEFAULT_TIMEOUT
    if isinstance(timeout, (int, float)):
        return float(timeout)
    if isinstance(timeout, str):
        try:
            return float(timeout)
        except ValueError:
            return _DEFAULT_TIMEOUT
    return _DEFAULT_TIMEOUT


def resolve_talk_client(
    db: Session,
    *,
    base_url: str | None,
    username: str | None,
    app_password: str | None,
    timeout_sec: float | None,
    connector_config_id: str | None,
) -> NextcloudTalkClient:
    """Resolve credentials from payload and optional ConnectorConfig, return a client."""
    if connector_config_id:
        config = db.get(ConnectorConfig, coerce_uuid(connector_config_id))
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        auth_config = dict(config.auth_config or {})
        base_url = base_url or config.base_url
        username = username or auth_config.get("username")
        app_password = (
            app_password
            or auth_config.get("app_password")
            or auth_config.get("password")
        )
        timeout_sec = timeout_sec or config.timeout_sec or auth_config.get("timeout_sec")

    if not base_url or not username or not app_password:
        raise HTTPException(
            status_code=400,
            detail="Nextcloud Talk credentials are incomplete.",
        )

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https":
        raise ValueError("Nextcloud base_url must use https://")
    if not parsed.hostname:
        raise ValueError("Nextcloud base_url must include a hostname")

    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError("Nextcloud base_url hostname could not be resolved") from exc

    for addr in addrs:
        ip = ipaddress.ip_address(addr[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(
                "SSRF blocked: Nextcloud base_url resolves to internal address"
            )

    return NextcloudTalkClient(
        base_url=base_url,
        username=username,
        app_password=app_password,
        timeout=float(timeout_sec or 30.0),
    )


class NextcloudTalkError(Exception):
    """Base exception for Nextcloud Talk client errors."""

    pass


class NextcloudTalkClient:
    """HTTP client for Nextcloud Talk OCS API (spreed)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        timeout: float | None = None,
        db: Session | None = None,
    ) -> None:
        # Use configurable timeout if not explicitly provided
        if timeout is None:
            timeout = get_nextcloud_talk_timeout(db)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth = httpx.BasicAuth(username, app_password)
        self.headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        self.ocs_base_path = "/ocs/v2.php/apps/spreed/api/v4"

    def _parse_ocs(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise NextcloudTalkError("Invalid JSON response from Nextcloud Talk") from exc

        if not isinstance(payload, dict) or "ocs" not in payload:
            raise NextcloudTalkError("Invalid OCS response structure")

        meta = payload.get("ocs", {}).get("meta", {})
        statuscode = meta.get("statuscode")
        if statuscode != 100:
            message = meta.get("message") or meta.get("status") or "Unknown error"
            raise NextcloudTalkError(f"OCS error {statuscode}: {message}")

        return payload.get("ocs", {}).get("data")

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        data: dict | None = None,
    ) -> Any:
        url = f"{self.base_url}{self.ocs_base_path}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=self.headers,
                    auth=self.auth,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Nextcloud Talk HTTP error: %s - %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise NextcloudTalkError(
                f"HTTP error: {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("Nextcloud Talk request error: %s", exc)
            raise NextcloudTalkError(f"Request error: {exc}") from exc

        return self._parse_ocs(response)

    def list_rooms(self) -> list[dict]:
        data = self._request("GET", "/room")
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return [data]

    def create_room(
        self,
        room_name: str,
        room_type: str | int = "public",
        options: dict | None = None,
    ) -> dict:
        payload = {"roomName": room_name, "roomType": room_type}
        if options:
            payload.update(options)
        data = self._request("POST", "/room", data=payload)
        if isinstance(data, dict):
            return data
        return {"data": data}

    def post_message(
        self,
        room_token: str,
        message: str,
        options: dict | None = None,
    ) -> dict:
        payload = {"message": message}
        if options:
            payload.update(options)
        data = self._request("POST", f"/room/{room_token}/message", data=payload)
        if isinstance(data, dict):
            return data
        return {"data": data}
