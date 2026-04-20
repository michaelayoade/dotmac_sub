from __future__ import annotations

import logging
import os
from itertools import count
from typing import Any

import httpx

from app.services.zabbix_mapper import map_alerts, map_hosts

logger = logging.getLogger(__name__)

ALLOWED_METHODS = [
    # Read methods
    "host.get",
    "item.get",
    "history.get",
    "trend.get",
    "trigger.get",
    "discoveryrule.get",
    "itemprototype.get",
    # Write methods for host management
    "host.create",
    "host.update",
    "host.delete",
    # Host group methods
    "hostgroup.get",
    "hostgroup.create",
    # Template methods
    "template.get",
    # Maintenance window methods
    "maintenance.get",
    "maintenance.create",
    "maintenance.update",
    "maintenance.delete",
]


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
                "http://zabbix-web:8080/api_jsonrpc.php",
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

    def _submit_write_payload(
        self,
        payload: dict[str, Any],
        expected: str,
    ) -> dict[str, Any]:
        """Submit a write payload and return the result (dict or scalar)."""
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
            error_info = data.get("error", {})
            logger.warning(
                "zabbix_request_error",
                extra={
                    "event": "zabbix_request_error",
                    "method": expected,
                    "error": error_info,
                },
            )
            raise ZabbixClientError(
                f"Zabbix API error: {error_info.get('data', error_info.get('message', 'Unknown'))}"
            )

        logger.info(
            "zabbix_request_success",
            extra={"event": "zabbix_request_success", "method": expected},
        )
        result = data.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, bool):
            return {"success": result}
        return {"result": result}

    # ========== Host Group Methods ==========

    def get_host_groups(
        self,
        name: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Get host groups, optionally filtered by name."""
        method = "hostgroup.get"
        params: dict[str, Any] = {
            "output": ["groupid", "name"],
            "sortfield": "name",
            "limit": limit,
        }
        if name:
            params["filter"] = {"name": name}
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def create_host_group(self, name: str) -> str:
        """Create a host group and return its ID."""
        method = "hostgroup.create"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {"name": name},
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        group_ids = result.get("groupids", [])
        if not group_ids:
            raise ZabbixClientError("Failed to create host group")
        return group_ids[0]

    def get_or_create_host_group(self, name: str) -> str:
        """Get or create a host group by name, return its ID."""
        existing = self.get_host_groups(name=name)
        if existing:
            return existing[0]["groupid"]
        return self.create_host_group(name)

    # ========== Template Methods ==========

    def get_templates(
        self,
        name: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Get templates, optionally filtered by name."""
        method = "template.get"
        params: dict[str, Any] = {
            "output": ["templateid", "host", "name"],
            "sortfield": "name",
            "limit": limit,
        }
        if name:
            params["filter"] = {"host": name}
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    # ========== Host Write Methods ==========

    def create_host(
        self,
        host: str,
        name: str,
        group_ids: list[str],
        template_ids: list[str] | None = None,
        interface_ip: str | None = None,
        interface_type: int = 2,  # 1=agent, 2=SNMP, 3=IPMI, 4=JMX
        snmp_community: str = "{$SNMP_COMMUNITY}",
        snmp_version: int = 2,  # 1=SNMPv1, 2=SNMPv2c, 3=SNMPv3
        tags: list[dict[str, str]] | None = None,
        inventory: dict[str, str] | None = None,
    ) -> str:
        """Create a Zabbix host and return its ID.

        Args:
            host: Technical hostname (unique identifier)
            name: Visible display name
            group_ids: List of host group IDs
            template_ids: List of template IDs to link
            interface_ip: IP address for monitoring (required for SNMP)
            interface_type: 1=agent, 2=SNMP, 3=IPMI, 4=JMX
            snmp_community: SNMP community string (for SNMPv1/v2c)
            snmp_version: 1=SNMPv1, 2=SNMPv2c, 3=SNMPv3
            tags: List of {"tag": "key", "value": "val"} dicts
            inventory: Host inventory fields
        """
        method = "host.create"
        params: dict[str, Any] = {
            "host": host,
            "name": name,
            "groups": [{"groupid": gid} for gid in group_ids],
        }

        if template_ids:
            params["templates"] = [{"templateid": tid} for tid in template_ids]

        if interface_ip:
            interface: dict[str, Any] = {
                "type": interface_type,
                "main": 1,
                "useip": 1,
                "ip": interface_ip,
                "dns": "",
                "port": "161" if interface_type == 2 else "10050",
            }
            # Zabbix 7.0 requires details for SNMP interfaces
            if interface_type == 2:
                interface["details"] = {
                    "version": snmp_version,
                    "bulk": 1,
                    "community": snmp_community,
                }
            params["interfaces"] = [interface]

        if tags:
            params["tags"] = tags

        if inventory:
            params["inventory_mode"] = 1  # Manual mode
            params["inventory"] = inventory

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        host_ids = result.get("hostids", [])
        if not host_ids:
            raise ZabbixClientError("Failed to create host")
        return host_ids[0]

    def update_host(
        self,
        host_id: str,
        name: str | None = None,
        group_ids: list[str] | None = None,
        template_ids: list[str] | None = None,
        tags: list[dict[str, str]] | None = None,
        inventory: dict[str, str] | None = None,
        status: int | None = None,  # 0=enabled, 1=disabled
    ) -> bool:
        """Update an existing Zabbix host."""
        method = "host.update"
        params: dict[str, Any] = {"hostid": host_id}

        if name is not None:
            params["name"] = name
        if group_ids is not None:
            params["groups"] = [{"groupid": gid} for gid in group_ids]
        if template_ids is not None:
            params["templates"] = [{"templateid": tid} for tid in template_ids]
        if tags is not None:
            params["tags"] = tags
        if inventory is not None:
            params["inventory_mode"] = 1
            params["inventory"] = inventory
        if status is not None:
            params["status"] = status

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        return bool(result.get("hostids"))

    def delete_host(self, host_id: str) -> bool:
        """Delete a Zabbix host by ID."""
        method = "host.delete"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": [host_id],
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        return bool(result.get("hostids"))

    # ========== Maintenance Methods ==========

    def get_maintenance_windows(
        self,
        host_ids: list[str] | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get maintenance windows, optionally filtered by host IDs."""
        method = "maintenance.get"
        params: dict[str, Any] = {
            "output": [
                "maintenanceid",
                "name",
                "active_since",
                "active_till",
                "description",
            ],
            "selectHosts": ["hostid", "host", "name"],
            "selectTimeperiods": "extend",
            "sortfield": "active_since",
            "sortorder": "DESC",
            "limit": limit,
        }
        if host_ids:
            params["hostids"] = host_ids
        if active_only:
            import time

            now = int(time.time())
            params["filter"] = {}
            # Filter for active maintenance windows handled by time range
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        return self._submit_read_payload(payload, method)

    def create_maintenance(
        self,
        name: str,
        host_ids: list[str],
        active_since: int,
        active_till: int,
        description: str | None = None,
    ) -> str:
        """Create a maintenance window and return its ID.

        Args:
            name: Maintenance window name
            host_ids: List of host IDs to put in maintenance
            active_since: Unix timestamp for start
            active_till: Unix timestamp for end
            description: Optional description
        """
        method = "maintenance.create"
        params: dict[str, Any] = {
            "name": name,
            "active_since": active_since,
            "active_till": active_till,
            "hostids": host_ids,
            "timeperiods": [
                {
                    "timeperiod_type": 0,  # One-time
                    "start_date": active_since,
                    "period": active_till - active_since,
                }
            ],
        }
        if description:
            params["description"] = description

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        maint_ids = result.get("maintenanceids", [])
        if not maint_ids:
            raise ZabbixClientError("Failed to create maintenance window")
        return maint_ids[0]

    def update_maintenance(
        self,
        maintenance_id: str,
        name: str | None = None,
        host_ids: list[str] | None = None,
        active_since: int | None = None,
        active_till: int | None = None,
        description: str | None = None,
    ) -> bool:
        """Update an existing maintenance window."""
        method = "maintenance.update"
        params: dict[str, Any] = {"maintenanceid": maintenance_id}

        if name is not None:
            params["name"] = name
        if host_ids is not None:
            params["hostids"] = host_ids
        if active_since is not None:
            params["active_since"] = active_since
        if active_till is not None:
            params["active_till"] = active_till
        if description is not None:
            params["description"] = description

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        return bool(result.get("maintenanceids"))

    def delete_maintenance(self, maintenance_id: str) -> bool:
        """Delete a maintenance window by ID."""
        method = "maintenance.delete"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": [maintenance_id],
            "id": next(self._request_ids),
        }
        result = self._submit_write_payload(payload, method)
        return bool(result.get("maintenanceids"))

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
