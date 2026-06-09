"""Zabbix's default action template sends `tags` as a list of
{"tag": ..., "value": ...} objects. The payload model must accept that (and the
older flat-dict form) instead of 422-ing a real alert on an unused field.
"""

from app.api.zabbix_webhook import ZabbixAlertPayload

# Verbatim body that returned 422 in production (event zabbix_webhook_invalid_payload).
_REAL_ZABBIX_BODY = {
    "triggerId": "30479",
    "triggerName": "Interface 0/2(D-EAGLE4): Link down",
    "triggerStatus": "OK",
    "triggerSeverity": "Average",
    "triggerUrl": "",
    "hostId": "11066",
    "hostName": "Eagle FM Switch",
    "hostIp": "172.16.150.2",
    "eventId": "3994272",
    "eventTime": "11:42:44",
    "eventDate": "2026.06.09",
    "eventValue": "0",
    "itemId": "63778",
    "itemName": "Interface 0/2(D-EAGLE4): Operational status",
    "itemValue": "up (1)",
    "itemKey": "net.if.status[ifOperStatus.2]",
    "tags": [
        {"tag": "scope", "value": "availability"},
        {"tag": "component", "value": "network"},
        {"tag": "interface", "value": "0/2"},
        {"tag": "description", "value": "D-EAGLE4"},
    ],
}


def test_real_zabbix_list_tags_payload_parses():
    payload = ZabbixAlertPayload.model_validate(_REAL_ZABBIX_BODY)
    assert payload.trigger_id == "30479"
    assert payload.host_name == "Eagle FM Switch"
    # List of {tag,value} flattened to a {tag: value} dict.
    assert payload.tags == {
        "scope": "availability",
        "component": "network",
        "interface": "0/2",
        "description": "D-EAGLE4",
    }


def test_flat_dict_tags_still_accepted():
    payload = ZabbixAlertPayload.model_validate(
        {**_REAL_ZABBIX_BODY, "tags": {"scope": "availability"}}
    )
    assert payload.tags == {"scope": "availability"}


def test_tags_optional():
    body = {k: v for k, v in _REAL_ZABBIX_BODY.items() if k != "tags"}
    payload = ZabbixAlertPayload.model_validate(body)
    assert payload.tags is None


def test_duplicate_tag_keys_keep_last_value():
    payload = ZabbixAlertPayload.model_validate(
        {
            **_REAL_ZABBIX_BODY,
            "tags": [
                {"tag": "interface", "value": "0/2"},
                {"tag": "interface", "value": "0/19"},
            ],
        }
    )
    assert payload.tags == {"interface": "0/19"}
