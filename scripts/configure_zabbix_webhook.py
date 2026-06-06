#!/usr/bin/env python3
"""Idempotently configure the Zabbix side of the inbound alert webhook.

Creates (or updates) a Zabbix *webhook media type* that POSTs trigger events to
this app's ``/api/v1/zabbix/webhook/alert`` endpoint with the shared
``X-Zabbix-Token`` header that the endpoint now requires (fail-closed). With
``--with-action`` it also creates a trigger action that uses the media type.

Run it where the Zabbix API hostname resolves (the app's Docker network), e.g.:

    docker compose exec app python scripts/configure_zabbix_webhook.py
    docker compose exec app python scripts/configure_zabbix_webhook.py --with-action

Reads configuration from the same sources the app uses:
  ZABBIX_API_URL / ZABBIX_API_TOKEN  (admin/super-admin token; create perms)
  ZABBIX_WEBHOOK_TOKEN               (shared secret the endpoint checks)
  APP_URL                            (public base URL of this app)

Nothing here hardcodes secrets — everything comes from the environment.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

# Allow running as a plain script (python scripts/configure_zabbix_webhook.py)
# from anywhere — ensure the repo root is importable for the ``app`` package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.zabbix import (  # noqa: E402  (after sys.path bootstrap)
    get_zabbix_api_token,
    get_zabbix_api_url,
    get_zabbix_webhook_token,
)

MEDIA_TYPE_NAME = "DotMac Sub Webhook"
ACTION_NAME = "DotMac Sub - forward problems"

# Zabbix macro -> our endpoint field. Sent to the webhook script as `value`.
_PARAM_MACROS: dict[str, str] = {
    "triggerId": "{TRIGGER.ID}",
    "triggerName": "{TRIGGER.NAME}",
    "triggerStatus": "{TRIGGER.STATUS}",
    "triggerSeverity": "{TRIGGER.SEVERITY}",
    "triggerUrl": "{TRIGGER.URL}",
    "hostId": "{HOST.ID}",
    "hostName": "{HOST.NAME}",
    "hostIp": "{HOST.IP}",
    "eventId": "{EVENT.ID}",
    "eventTime": "{EVENT.TIME}",
    "eventDate": "{EVENT.DATE}",
    "eventValue": "{EVENT.VALUE}",
    "itemId": "{ITEM.ID}",
    "itemName": "{ITEM.NAME}",
    "itemValue": "{ITEM.VALUE}",
    "itemKey": "{ITEM.KEY}",
    "tagsJson": "{EVENT.TAGSJSON}",
}

# JS executed by Zabbix: forwards the params to our endpoint with the token.
_WEBHOOK_SCRIPT = r"""
var p = JSON.parse(value);
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('X-Zabbix-Token: ' + p.Token);
var tags = null;
try { if (p.tagsJson) { tags = JSON.parse(p.tagsJson); } } catch (e) { tags = null; }
var body = {
    triggerId: p.triggerId, triggerName: p.triggerName,
    triggerStatus: p.triggerStatus, triggerSeverity: p.triggerSeverity,
    triggerUrl: p.triggerUrl, hostId: p.hostId, hostName: p.hostName,
    hostIp: p.hostIp, eventId: p.eventId, eventTime: p.eventTime,
    eventDate: p.eventDate, eventValue: p.eventValue, itemId: p.itemId,
    itemName: p.itemName, itemValue: p.itemValue, itemKey: p.itemKey,
    tags: tags
};
var resp = req.post(p.URL, JSON.stringify(body));
var status = req.getStatus();
if (status < 200 || status >= 300) {
    throw 'DotMac webhook failed: HTTP ' + status + ' ' + resp;
}
return 'OK';
""".strip()


class ZabbixAdminError(RuntimeError):
    pass


class _Api:
    """Minimal Zabbix JSON-RPC client with create/update perms.

    Deliberately separate from app.services.zabbix.ZabbixClient, whose method
    allowlist (read + host mgmt) intentionally excludes mediatype/action.create.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._id = 0
        self._use_bearer = self._detect_bearer_support()

    def _detect_bearer_support(self) -> bool:
        # apiinfo.version needs no auth; >= 6.4 supports the Bearer header.
        version = self._raw_call("apiinfo.version", {}, auth=False)
        try:
            major, minor = (int(x) for x in str(version).split(".")[:2])
        except ValueError:
            return False
        return (major, minor) >= (6, 4)

    def _raw_call(self, method: str, params: dict, *, auth: bool = True) -> object:
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        headers = {"Content-Type": "application/json-rpc"}
        if auth and getattr(self, "_use_bearer", False):
            headers["Authorization"] = f"Bearer {self._token}"
        elif auth:
            payload["auth"] = self._token
        resp = httpx.post(self._url, json=payload, headers=headers, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ZabbixAdminError(f"{method}: {data['error']}")
        return data["result"]

    def call(self, method: str, params: dict) -> object:
        return self._raw_call(method, params, auth=True)


def _upsert_media_type(api: _Api, *, app_url: str, webhook_token: str) -> str:
    parameters = [{"name": "URL", "value": f"{app_url.rstrip('/')}/api/v1/zabbix/webhook/alert"}]
    parameters.append({"name": "Token", "value": webhook_token})
    parameters.extend({"name": k, "value": v} for k, v in _PARAM_MACROS.items())

    fields = {
        "type": "4",  # webhook
        "name": MEDIA_TYPE_NAME,
        "parameters": parameters,
        "script": _WEBHOOK_SCRIPT,
        "status": "0",  # enabled
        "message_templates": [
            {  # trigger problem
                "eventsource": "0",
                "recovery": "0",
                "subject": "{EVENT.NAME}",
                "message": "{EVENT.NAME}",
            },
            {  # trigger recovery
                "eventsource": "0",
                "recovery": "1",
                "subject": "RESOLVED: {EVENT.NAME}",
                "message": "{EVENT.NAME}",
            },
        ],
    }

    existing = api.call("mediatype.get", {"filter": {"name": [MEDIA_TYPE_NAME]}, "output": ["mediatypeid"]})
    if existing:
        media_id = str(existing[0]["mediatypeid"])
        api.call("mediatype.update", {"mediatypeid": media_id, **fields})
        print(f"updated media type '{MEDIA_TYPE_NAME}' (id={media_id})")
        return media_id
    result = api.call("mediatype.create", fields)
    media_id = str(result["mediatypeids"][0])
    print(f"created media type '{MEDIA_TYPE_NAME}' (id={media_id})")
    return media_id


def _upsert_trigger_action(api: _Api, media_id: str) -> None:
    existing = api.call("action.get", {"filter": {"name": [ACTION_NAME]}, "output": ["actionid"]})
    operations = [
        {
            "operationtype": "0",  # send message
            "opmessage": {"default_msg": "1", "mediatypeid": media_id},
            "opmessage_grp": [],  # see note printed below
        }
    ]
    fields = {
        "name": ACTION_NAME,
        "eventsource": "0",  # triggers
        "status": "0",
        "esc_period": "1h",
        "operations": operations,
    }
    if existing:
        action_id = str(existing[0]["actionid"])
        api.call("action.update", {"actionid": action_id, **fields})
        print(f"updated action '{ACTION_NAME}' (id={action_id})")
    else:
        result = api.call("action.create", fields)
        print(f"created action '{ACTION_NAME}' (id={result['actionids'][0]})")
    print(
        "NOTE: the action sends via the media type, but Zabbix only delivers to "
        "users who have this media type configured on their profile. Assign "
        f"'{MEDIA_TYPE_NAME}' media (any sendto value) to the recipient user(s) "
        "in Users > Media, and set the action's recipient user group as needed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-action",
        action="store_true",
        help="also create/update a trigger action that uses the media type",
    )
    args = parser.parse_args()

    api_url = get_zabbix_api_url()
    api_token = get_zabbix_api_token()
    webhook_token = get_zabbix_webhook_token()
    app_url = os.getenv("APP_URL", "").strip()

    missing = [
        name
        for name, val in (
            ("ZABBIX_API_TOKEN", api_token),
            ("ZABBIX_WEBHOOK_TOKEN", webhook_token),
            ("APP_URL", app_url),
        )
        if not val
    ]
    if missing:
        print(f"error: missing required config: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"Zabbix API: {api_url}")
    print(f"Target endpoint: {app_url.rstrip('/')}/api/v1/zabbix/webhook/alert")

    try:
        api = _Api(api_url, api_token)
        media_id = _upsert_media_type(api, app_url=app_url, webhook_token=webhook_token)
        if args.with_action:
            _upsert_trigger_action(api, media_id)
    except (httpx.HTTPError, ZabbixAdminError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
