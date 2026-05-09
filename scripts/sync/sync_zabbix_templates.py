#!/usr/bin/env python3
"""Sync templates from external Zabbix to Docker Zabbix.

This script reads templates from the external (source) Zabbix server
and imports them into the Docker (target) Zabbix server.

Usage:
    python scripts/sync_zabbix_templates.py [--dry-run]

Environment variables:
    ZABBIX_EXTERNAL_URL - External Zabbix API URL
    ZABBIX_EXTERNAL_TOKEN - External Zabbix API token
    ZABBIX_API_URL - Docker Zabbix API URL (target)
    ZABBIX_API_TOKEN - Docker Zabbix API token (target)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from itertools import count
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class ZabbixAPI:
    """Simple Zabbix API client."""

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self._request_ids = count(1)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make a Zabbix API call."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": next(self._request_ids),
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json-rpc",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("error"):
            raise Exception(f"Zabbix API error: {data['error']}")
        return data.get("result")


def export_templates(api: ZabbixAPI, template_ids: list[str]) -> dict:
    """Export templates in Zabbix format."""
    return api.call(
        "configuration.export",
        {
            "format": "json",
            "options": {
                "templates": template_ids,
            },
        },
    )


def get_templates(api: ZabbixAPI) -> list[dict]:
    """Get all templates."""
    return api.call(
        "template.get",
        {
            "output": ["templateid", "host", "name", "description"],
            "sortfield": "name",
        },
    )


def get_host_groups(api: ZabbixAPI) -> list[dict]:
    """Get all host groups."""
    return api.call(
        "hostgroup.get",
        {
            "output": ["groupid", "name"],
            "sortfield": "name",
        },
    )


def import_configuration(api: ZabbixAPI, config_json: str) -> dict:
    """Import configuration into Zabbix."""
    return api.call(
        "configuration.import",
        {
            "format": "json",
            "source": config_json,
            "rules": {
                "templates": {"createMissing": True, "updateExisting": True},
                "template_groups": {"createMissing": True, "updateExisting": True},
                "host_groups": {"createMissing": True, "updateExisting": True},
                "items": {"createMissing": True, "updateExisting": True},
                "triggers": {"createMissing": True, "updateExisting": True},
                "discoveryRules": {"createMissing": True, "updateExisting": True},
                "graphs": {"createMissing": True, "updateExisting": True},
                "valueMaps": {"createMissing": True, "updateExisting": True},
            },
        },
    )


def create_host_group(api: ZabbixAPI, name: str) -> str:
    """Create a host group."""
    result = api.call("hostgroup.create", {"name": name})
    return result["groupids"][0]


def main():
    parser = argparse.ArgumentParser(description="Sync Zabbix templates")
    parser.add_argument("--dry-run", action="store_true", help="Don't make changes")
    parser.add_argument("--templates", nargs="*", help="Specific template names to sync")
    parser.add_argument("--list-templates", action="store_true", help="List available templates")
    parser.add_argument("--sync-groups", action="store_true", help="Sync host groups only")
    args = parser.parse_args()

    # Source (external) Zabbix
    source_url = os.getenv(
        "ZABBIX_EXTERNAL_URL",
        "http://160.119.127.193/zabbix/api_jsonrpc.php"
    )
    source_token = os.getenv(
        "ZABBIX_EXTERNAL_TOKEN",
        "622dbc396ffe31415b0c10dc59466edc069bbb6819fa1e574c49ff79b651b1a4"
    )

    # Target (Docker) Zabbix
    target_url = os.getenv("ZABBIX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
    target_token = os.getenv("ZABBIX_API_TOKEN", "")

    if not target_token:
        logger.error("ZABBIX_API_TOKEN not set")
        sys.exit(1)

    source = ZabbixAPI(source_url, source_token)
    target = ZabbixAPI(target_url, target_token)

    # Test connections
    try:
        logger.info(f"Connecting to source Zabbix: {source_url}")
        source_version = source.call("apiinfo.version")
        logger.info(f"Source Zabbix version: {source_version}")
    except Exception as e:
        logger.error(f"Failed to connect to source Zabbix: {e}")
        sys.exit(1)

    try:
        logger.info(f"Connecting to target Zabbix: {target_url}")
        target_version = target.call("apiinfo.version")
        logger.info(f"Target Zabbix version: {target_version}")
    except Exception as e:
        logger.error(f"Failed to connect to target Zabbix: {e}")
        sys.exit(1)

    # List templates
    if args.list_templates:
        templates = get_templates(source)
        logger.info(f"\nFound {len(templates)} templates on source:")
        for t in templates:
            print(f"  - {t['name']} (host: {t['host']})")
        return

    # Sync host groups
    if args.sync_groups:
        source_groups = get_host_groups(source)
        target_groups = get_host_groups(target)
        target_names = {g["name"] for g in target_groups}

        logger.info(f"Source has {len(source_groups)} host groups")
        logger.info(f"Target has {len(target_groups)} host groups")

        for group in source_groups:
            if group["name"] not in target_names:
                if args.dry_run:
                    logger.info(f"Would create host group: {group['name']}")
                else:
                    logger.info(f"Creating host group: {group['name']}")
                    try:
                        create_host_group(target, group["name"])
                    except Exception as e:
                        logger.warning(f"Failed to create group {group['name']}: {e}")
        return

    # Sync templates
    templates = get_templates(source)

    if args.templates:
        # Filter to specific templates
        templates = [t for t in templates if t["name"] in args.templates or t["host"] in args.templates]
        if not templates:
            logger.error(f"No matching templates found for: {args.templates}")
            sys.exit(1)

    logger.info(f"Syncing {len(templates)} templates...")

    # Export in batches to avoid timeout
    batch_size = 10
    for i in range(0, len(templates), batch_size):
        batch = templates[i : i + batch_size]
        template_ids = [t["templateid"] for t in batch]
        template_names = [t["name"] for t in batch]

        logger.info(f"Exporting batch {i // batch_size + 1}: {template_names}")

        try:
            config_json = export_templates(source, template_ids)
        except Exception as e:
            logger.error(f"Failed to export templates: {e}")
            continue

        if args.dry_run:
            logger.info(f"Would import {len(batch)} templates")
            # Parse and show summary
            config = json.loads(config_json)
            if "zabbix_export" in config:
                export_data = config["zabbix_export"]
                if "templates" in export_data:
                    for tmpl in export_data["templates"]:
                        items = len(tmpl.get("items", []))
                        triggers = len(tmpl.get("triggers", []))
                        discovery = len(tmpl.get("discovery_rules", []))
                        logger.info(
                            f"  Template '{tmpl.get('name')}': "
                            f"{items} items, {triggers} triggers, {discovery} discovery rules"
                        )
        else:
            logger.info(f"Importing {len(batch)} templates to target...")
            try:
                import_configuration(target, config_json)
                logger.info("Import successful")
            except Exception as e:
                logger.error(f"Failed to import: {e}")

    logger.info("Template sync complete")


if __name__ == "__main__":
    main()
