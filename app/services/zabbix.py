from __future__ import annotations

import logging
import os
from itertools import count
from typing import Any

import httpx

from app.services.zabbix_mapper import map_alerts, map_hosts

logger = logging.getLogger(__name__)

ALLOWED_METHODS = ["host.get", "item.get", "history.get", "trend.get", "trigger.get"]


class ZabbixClientError(Exception):
    pass


class ZabbixMethodNotAllowedError(ZabbixClientError):
    pass


class ZabbixConfigurationError(ZabbixClientError):
    pass


class ZabbixClient:
    def __init__(self, api_url: str, api_token: str, timeout: float = 10.0) -> None:
        if not api_url:
            raise ZabbixConfigurationError("Zabbix API URL is not configured")
        if not api_token:
            raise ZabbixConfigurationError("Zabbix API token is not configured")
        self.api_url = api_url
        self.api_token = api_token
        self.timeout = timeout
        self._request_ids = count(1)

    @classmethod
    def from_env(cls) -> ZabbixClient:
        timeout = float(os.getenv("ZABBIX_TIMEOUT_SECONDS", "10"))
        return cls(
            api_url=os.getenv(
                "ZABBIX_API_URL",
                "http://160.119.127.193/zabbix/api_jsonrpc.php",
            ),
            api_token=os.getenv("ZABBIX_API_TOKEN", ""),
            timeout=timeout,
        )

    def _ensure_allowed(self, payload: dict[str, Any], expected: str) -> None:
        method = str(payload.get("method") or "")
        if method != expected or method not in ALLOWED_METHODS:
            raise ZabbixMethodNotAllowedError("Zabbix API method is not allowed")

    def _submit_read_payload(
        self,
        payload: dict[str, Any],
        expected: str,
    ) -> list[dict[str, Any]]:
        self._ensure_allowed(payload, expected)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json-rpc",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.info(
                "zabbix_request_failure",
                extra={
                    "event": "zabbix_request_failure",
                    "method": expected,
                    "status_code": exc.response.status_code,
                },
            )
            raise ZabbixClientError("Zabbix API request failed") from exc
        except (httpx.RequestError, ValueError) as exc:
            logger.info(
                "zabbix_request_failure",
                extra={"event": "zabbix_request_failure", "method": expected},
            )
            raise ZabbixClientError("Zabbix API request failed") from exc

        if not isinstance(data, dict):
            raise ZabbixClientError("Invalid Zabbix API response")
        if data.get("error"):
            logger.info(
                "zabbix_request_failure",
                extra={"event": "zabbix_request_failure", "method": expected},
            )
            raise ZabbixClientError("Zabbix API returned an error")
        result = data.get("result")
        if not isinstance(result, list):
            raise ZabbixClientError("Invalid Zabbix API result")

        logger.info(
            "zabbix_request_success",
            extra={"event": "zabbix_request_success", "method": expected},
        )
        return [item for item in result if isinstance(item, dict)]

    def get_hosts(
        self,
        host_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        method = "host.get"
        params: dict[str, Any] = {
            "output": ["hostid", "host", "name", "status", "available"],
            "selectGroups": ["groupid", "name"],
            "selectInterfaces": ["ip", "dns", "port", "type", "main", "useip"],
            "selectTags": ["tag", "value"],
            "selectInventory": "extend",
            "sortfield": "name",
            "limit": limit,
        }
        if host_id:
            params["hostids"] = [host_id]
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def get_items(
        self,
        host_ids: list[str] | None = None,
        metric: str | None = None,
        limit: int = 100000,
    ) -> list[dict[str, Any]]:
        method = "item.get"
        params: dict[str, Any] = {
            "output": [
                "itemid",
                "hostid",
                "name",
                "key_",
                "value_type",
                "units",
                "lastvalue",
                "lastclock",
            ],
            "monitored": True,
            "search": {"key_": metric or "net.if"},
            "sortfield": "name",
            "limit": limit,
        }
        if host_ids:
            params["hostids"] = host_ids
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def get_history(
        self,
        item_ids: list[str],
        history_type: int,
        time_from: int,
        time_till: int,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        method = "history.get"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "output": ["itemid", "clock", "value"],
                "history": history_type,
                "itemids": item_ids,
                "time_from": time_from,
                "time_till": time_till,
                "sortfield": ["itemid", "clock"],
                "sortorder": "ASC",
                "limit": limit,
            },
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def get_trends(
        self,
        item_ids: list[str],
        time_from: int,
        time_till: int,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        method = "trend.get"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "output": [
                    "itemid",
                    "clock",
                    "num",
                    "value_min",
                    "value_avg",
                    "value_max",
                ],
                "itemids": item_ids,
                "time_from": time_from,
                "time_till": time_till,
                "sortfield": ["itemid", "clock"],
                "sortorder": "ASC",
                "limit": limit,
            },
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def get_triggers(
        self,
        host_id: str | None = None,
        active_only: bool = True,
        min_priority: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        method = "trigger.get"
        params: dict[str, Any] = {
            "output": [
                "triggerid",
                "description",
                "priority",
                "value",
                "status",
                "lastchange",
            ],
            "selectHosts": ["hostid", "host", "name"],
            "sortfield": "lastchange",
            "sortorder": "DESC",
            "limit": limit,
        }
        if host_id:
            params["hostids"] = [host_id]
        if active_only:
            params["filter"] = {"value": 1}
        if min_priority is not None:
            params["min_severity"] = min_priority
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)


def get_hosts(host_id: str | None = None, limit: int = 1000):
    client = ZabbixClient.from_env()
    return map_hosts(client.get_hosts(host_id=host_id, limit=limit))


def get_metrics(
    host_id: str | None = None,
    metric: str | None = None,
    time_from=None,
    time_till=None,
    limit: int = 100,
):
    from app.services.zabbix_engine import get_zabbix_engine

    host_ids = [host_id] if host_id else None
    return get_zabbix_engine().get_normalized_metrics(
        host_ids=host_ids,
        metric=metric,
        time_from=time_from,
        time_till=time_till,
        limit=limit,
    )


def get_alerts(
    host_id: str | None = None,
    active_only: bool = True,
    min_priority: int | None = None,
    limit: int = 100,
):
    client = ZabbixClient.from_env()
    return map_alerts(
        client.get_triggers(
            host_id=host_id,
            active_only=active_only,
            min_priority=min_priority,
            limit=limit,
        )
    )
