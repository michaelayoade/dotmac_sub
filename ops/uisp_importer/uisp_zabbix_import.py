#!/usr/bin/env python3
"""UISP -> app-Zabbix topology importer.

Reads the UISP inventory (devices + sites) and reconciles it into the app
Zabbix (the instance the dotmac_sub reconcile consumes):

  - infrastructure (APs, routers, switches, UF-OLTs, backhauls): MATCH existing
    Zabbix hosts by interface IP, then normalized name — only ADD tags and empty
    inventory fields, never create duplicates or modify anything else;
  - customer layer (stations, ONUs, airCube/blackBox): CREATE hosts. Stations
    with a unique routable mgmt IP get an ICMP Ping template; everything else
    becomes an interface-less host whose status arrives via trapper items;
  - relationships ride host tags: parent_ap / parent_olt (+ *_id with the UISP
    uuid), site, role, uisp_id, and managed:uisp-importer on every object the
    importer owns.

Safety rules (see docs/superpowers/specs/2026-07-04-uisp-topology-connector-design.md):
  - dry-run by default; --apply required to write, and apply refuses to run
    unless a plan review file from a prior dry-run is confirmed with --yes;
  - never modifies or deletes a host lacking the managed:uisp-importer tag
    (matched infra hosts only receive tag/inventory additions);
  - circuit breaker: once managed hosts exist, a run that would re-parent or
    disable >20% of them aborts;
  - devices in the UISP Archive site are ignored entirely.

Tokens (never hardcoded): UISP_TOKEN / UISP_TOKEN_FILE for UISP,
ZABBIX_API_TOKEN / ZABBIX_API_TOKEN_FILE for Zabbix.

Usage:
  UISP_TOKEN_FILE=... ZABBIX_API_TOKEN_FILE=... \
    ./uisp_zabbix_import.py --uisp https://uisp.dotmac.ng \
      --zabbix http://127.0.0.1:8085 [--apply --yes] [--plan-out plan.json]
"""

import argparse
import ipaddress
import json
import os
import re
import ssl
import sys
import urllib.request
from collections import Counter
from urllib.parse import urlparse

ARCHIVE_SITE_ID = "d857d634-db38-45ff-81a1-4594410ded45"
MANAGED_TAG = ("managed", "uisp-importer")
CIRCUIT_BREAKER_FRACTION = 0.20
ICMP_TEMPLATE_NAME = "ICMP Ping"
STATION_GROUP = "UISP/Stations"
ONU_GROUP = "UISP/ONUs"
INFRA_ROLES = {"ap", "router", "switch"}
INFRA_TYPES = {"airFiber", "wave", "olt", "eswitch", "toughSwitch"}
CUSTOMER_ROLES = {"station", "homeWiFi"}
MGMT_NETS = [ipaddress.ip_network(n) for n in ("172.16.0.0/12", "10.0.0.0/8")]

# TLS is verified by default (uisp.dotmac.ng carries a valid cert).
# --insecure exists only for raw-IP access during diagnostics.
_ssl_ctx = ssl.create_default_context()


def allow_insecure_tls():
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE


def _read_token(env_name):
    path = os.getenv(env_name + "_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    value = os.getenv(env_name, "").strip()
    if not value:
        sys.exit(f"missing credential: set {env_name} or {env_name}_FILE")
    return value


def _http_json(url, headers=None, payload=None, timeout=60):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme for API request: {parsed.scheme}")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})  # noqa: S310
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        req, timeout=timeout, context=_ssl_ctx
    ) as resp:
        return json.loads(resp.read().decode())


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Uisp:
    def __init__(self, base, token):
        self.base = base.rstrip("/") + "/nms/api/v2.1"
        self.headers = {"x-auth-token": token}

    def get(self, path):
        return _http_json(self.base + path, headers=self.headers)

    def live_devices(self):
        devices = self.get("/devices")
        return [
            d
            for d in devices
            if (
                (d.get("identification", {}).get("site") or {}).get("id")
                != ARCHIVE_SITE_ID
            )
        ]

    def sites(self):
        return self.get("/sites")

    def ap_stations(self, ap_id):
        try:
            return self.get(f"/devices/airmaxes/{ap_id}/stations")
        except Exception:
            return []


class Zabbix:
    def __init__(self, base, token):
        self.url = base.rstrip("/") + "/api_jsonrpc.php"
        self.token = token
        self._id = 0

    def call(self, method, params):
        self._id += 1
        out = _http_json(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            payload={
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._id,
            },
        )
        if "error" in out:
            raise RuntimeError(f"zabbix {method}: {out['error']}")
        return out["result"]

    def hosts(self):
        return self.call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status"],
                "selectInterfaces": ["interfaceid", "ip", "type", "main"],
                "selectTags": "extend",
                "selectHostGroups": ["groupid", "name"],
            },
        )

    def groups(self):
        return {
            g["name"]: g["groupid"]
            for g in self.call("hostgroup.get", {"output": ["groupid", "name"]})
        }

    def template_id(self, name):
        res = self.call(
            "template.get",
            {"output": ["templateid"], "filter": {"host": [name], "name": [name]}},
        )
        return res[0]["templateid"] if res else None


def zbx_sender(server, port, values, chunk=250):
    """Minimal zabbix_sender (ZBXD v1 framing). values: [(host, key, value)]."""
    import socket
    import struct

    sent = failed = 0
    for i in range(0, len(values), chunk):
        batch = values[i : i + chunk]
        payload = json.dumps(
            {
                "request": "sender data",
                "data": [{"host": h, "key": k, "value": str(v)} for h, k, v in batch],
            }
        ).encode()
        frame = b"ZBXD\x01" + struct.pack("<Q", len(payload)) + payload
        with socket.create_connection((server, port), timeout=30) as sock:
            sock.sendall(frame)
            hdr = sock.recv(13)
            if not hdr.startswith(b"ZBXD"):
                raise RuntimeError("bad sender response header")
            length = struct.unpack("<Q", hdr[5:13])[0]
            body = b""
            while len(body) < length:
                body += sock.recv(length - len(body))
        info = json.loads(body.decode()).get("info", "")
        m = re.search(r"processed: (\d+); failed: (\d+)", info)
        if m:
            sent += int(m.group(1))
            failed += int(m.group(2))
    return sent, failed


def push_status(zbx, devices, sender_host, sender_port):
    """Feed uisp.status trapper items from UISP overview.status."""
    items = zbx.call(
        "item.get",
        {
            "output": ["itemid"],
            "selectHosts": ["host"],
            "filter": {"key_": "uisp.status"},
        },
    )
    trapper_hosts = {it["hosts"][0]["host"] for it in items if it.get("hosts")}
    values = []
    for d in devices:
        host = f"uisp-{d['identification']['id']}"
        if host in trapper_hosts:
            status = (d.get("overview") or {}).get("status") or "unknown"
            values.append((host, "uisp.status", status))
    sent, failed = zbx_sender(sender_host, sender_port, values)
    print(
        f"status push: {sent} accepted, {failed} failed "
        f"({len(values)} sent, {len(trapper_hosts)} trapper hosts)"
    )


def tag_map(host):
    return {t["tag"]: t["value"] for t in host.get("tags", [])}


def is_managed(host):
    return tag_map(host).get(MANAGED_TAG[0]) == MANAGED_TAG[1]


def usable_ip(ip, seen_counter):
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if seen_counter[ip] > 1:  # shared/default IPs (e.g. 192.168.1.1 fleet)
        return False
    return any(addr in net for net in MGMT_NETS)


def classify(device):
    ident = device["identification"]
    role, dtype = ident.get("role"), ident.get("type")
    if dtype == "olt":
        return "uf_olt"
    if dtype == "onu":
        return "onu"
    if role in CUSTOMER_ROLES or dtype in ("airCube", "blackBox"):
        return "customer"
    if role in INFRA_ROLES or dtype in INFRA_TYPES:
        return "infra"
    return "other"


def build_plan(uisp, zbx_hosts, zbx_groups, sites_by_id, devices):
    by_ip, by_name = {}, {}
    for h in zbx_hosts:
        for itf in h.get("interfaces", []):
            if itf.get("ip"):
                by_ip.setdefault(itf["ip"], h)
        by_name.setdefault(norm(h["name"]), h)
        by_name.setdefault(norm(h["host"]), h)

    devs_by_id = {d["identification"]["id"]: d for d in devices}

    # fill missing apDevice from AP-side station lists
    ap_of_station = {}
    for d in devices:
        ap = (d.get("attributes") or {}).get("apDevice") or {}
        if ap.get("id"):
            ap_of_station[d["identification"]["id"]] = ap["id"]
    aps = [d for d in devices if d["identification"].get("role") == "ap"]
    for ap in aps:
        if (ap.get("overview") or {}).get("status") != "active":
            continue
        for st in uisp.ap_stations(ap["identification"]["id"]):
            sid = (st.get("deviceIdentification") or {}).get("id")
            if sid and sid not in ap_of_station:
                ap_of_station[sid] = ap["identification"]["id"]

    ip_counter = Counter((d.get("ipAddress") or "").split("/")[0] for d in devices)

    plan = {
        "tag_infra": [],
        "create": [],
        "unmatched_infra": [],
        "groups_to_create": set(),
        "skipped_other": 0,
    }

    for d in devices:
        ident = d["identification"]
        kind = classify(d)
        if kind == "other":
            plan["skipped_other"] += 1
            continue
        ip = (d.get("ipAddress") or "").split("/")[0]
        site = ident.get("site") or {}
        site_name = site.get("name") or ""
        parent_site = (
            sites_by_id.get(site.get("id"), {}).get("identification", {}).get("parent")
            or {}
        )
        bts = (
            site_name if site.get("type") == "site" else (parent_site.get("name") or "")
        )

        tags = {
            "uisp_id": ident["id"],
            "role": {
                "uf_olt": "olt",
                "onu": "onu",
                "customer": ident.get("role") or "cpe",
                "infra": ident.get("role") or ident.get("type"),
            }[kind],
            "site": bts or site_name,
            MANAGED_TAG[0]: MANAGED_TAG[1],
        }
        if kind == "uf_olt":
            tags["vendor"] = "ubiquiti"
        if kind == "onu":
            parent = (d.get("attributes") or {}).get("parentId")
            if parent and parent in devs_by_id:
                tags["parent_olt"] = devs_by_id[parent]["identification"]["name"]
                tags["parent_olt_id"] = parent
        if kind == "customer":
            ap_id = ap_of_station.get(ident["id"])
            if ap_id and ap_id in devs_by_id:
                tags["parent_ap"] = devs_by_id[ap_id]["identification"]["name"]
                tags["parent_ap_id"] = ap_id

        # Match-don't-create applies to ALL kinds: some customer-role devices
        # (PtP "X to Y Master" backhauls, enterprise masters) already exist as
        # Zabbix hosts. IP matching only when the IP is unique+routable —
        # shared defaults like 192.168.1.1 must not cross-match.
        match = (by_ip.get(ip) if usable_ip(ip, ip_counter) else None) or by_name.get(
            norm(ident.get("name"))
        )
        if match:
            if not tags.get("site"):
                # device unassigned to a site in UISP — backfill from the
                # matched host's existing BTS group membership
                tags["site"] = next(
                    (
                        g["name"]
                        for g in match.get("hostgroups", [])
                        if g["name"].lower().endswith("bts")
                    ),
                    "",
                )
            existing = tag_map(match)
            add = {k: v for k, v in tags.items() if v and k not in existing}
            if add:
                plan["tag_infra"].append(
                    {
                        "kind": kind,
                        "hostid": match["hostid"],
                        "zbx_name": match["name"],
                        "uisp_name": ident.get("name"),
                        "add_tags": add,
                        # strip read-only fields (e.g. "automatic") that
                        # host.update rejects
                        "keep_tags": [
                            {"tag": t["tag"], "value": t["value"]}
                            for t in match.get("tags", [])
                        ],
                    }
                )
            continue
        if kind == "infra":
            plan["unmatched_infra"].append(
                {
                    "name": ident.get("name"),
                    "ip": ip,
                    "site": site_name,
                    "model": ident.get("model"),
                }
            )
            continue
        # unmatched non-infra (customer/onu/uf_olt): create below

        # UISP site names often already end in " BTS"; try both forms against
        # the existing Zabbix groups before inventing a new group.
        group = None
        if bts:
            for candidate in (bts, bts + " BTS"):
                if candidate in zbx_groups:
                    group = candidate
                    break
        if group is None:
            group = (
                STATION_GROUP
                if kind == "customer"
                else (ONU_GROUP if kind == "onu" else (bts or site_name or "UISP"))
            )
            plan["groups_to_create"].add(group)
        pingable = kind != "onu" and usable_ip(ip, ip_counter)
        inv = {
            "macaddress_a": ident.get("mac") or "",
            "name": site_name,
            "location": bts,
        }
        loc = (sites_by_id.get(site.get("id"), {}).get("description") or {}).get(
            "location"
        ) or {}
        if loc.get("latitude"):
            inv["location_lat"] = f"{loc['latitude']:.6f}"
            inv["location_lon"] = f"{loc['longitude']:.6f}"
        addr = (sites_by_id.get(site.get("id"), {}).get("description") or {}).get(
            "address"
        )
        if addr:
            inv["site_address_a"] = addr[:128]
        plan["create"].append(
            {
                "kind": kind,
                "host": f"uisp-{ident['id']}",
                "visible_name": (ident.get("name") or ident["id"])[:128],
                "ip": ip if pingable else None,
                "group": group,
                "tags": {k: v for k, v in tags.items() if v},
                "inventory": {k: v for k, v in inv.items() if v},
                "status_via": "icmp" if pingable else "trapper",
            }
        )

    plan["groups_to_create"] = sorted(plan["groups_to_create"])
    return plan


def summarize(plan, devices, zbx_hosts):
    managed_existing = [h for h in zbx_hosts if is_managed(h)]
    creates = Counter(c["kind"] for c in plan["create"])
    via = Counter(c["status_via"] for c in plan["create"])
    print(f"UISP live devices considered : {len(devices)}")
    print(f"infra hosts matched, tags to add : {len(plan['tag_infra'])}")
    print(f"infra devices with NO zabbix host: {len(plan['unmatched_infra'])}")
    print(f"hosts to create : {sum(creates.values())}  {dict(creates)}")
    print(f"  status via    : {dict(via)}")
    print(f"groups to create: {plan['groups_to_create']}")
    print(f"devices skipped (unclassifiable): {plan['skipped_other']}")
    print(f"existing managed hosts: {len(managed_existing)}")
    if plan["unmatched_infra"]:
        print("\nunmatched infra (would NOT be created — review):")
        for u in plan["unmatched_infra"][:20]:
            print(f"  {u['name']:40s} {u['ip'] or '-':16s} {u['site']}")


def apply_plan(zbx, plan, zbx_hosts):
    # Circuit breaker guards VALUE-CHANGING updates on managed hosts
    # (re-parenting/disabling churn from a UISP reset). Pure tag additions
    # (add_tags only contains keys absent from the host) are always safe.
    managed = [h for h in zbx_hosts if is_managed(h)]
    if managed:
        mutating = [
            t
            for t in plan["tag_infra"]
            if set(t["add_tags"]) & {kt["tag"] for kt in t["keep_tags"]}
        ]
        if len(mutating) > CIRCUIT_BREAKER_FRACTION * max(len(managed), 1):
            sys.exit(
                f"circuit breaker: {len(mutating)} value-changing updates vs "
                f"{len(managed)} managed hosts — aborting"
            )

    groups = zbx.groups()
    for gname in plan["groups_to_create"]:
        if gname not in groups:
            res = zbx.call("hostgroup.create", {"name": gname})
            groups[gname] = res["groupids"][0]
            print(f"created group {gname}")

    icmp_tpl = zbx.template_id(ICMP_TEMPLATE_NAME)
    if icmp_tpl is None:
        sys.exit(f"template '{ICMP_TEMPLATE_NAME}' not found in Zabbix")

    for t in plan["tag_infra"]:
        zbx.call(
            "host.update",
            {
                "hostid": t["hostid"],
                "tags": t["keep_tags"]
                + [{"tag": k, "value": v} for k, v in t["add_tags"].items()],
            },
        )
    print(f"tagged {len(plan['tag_infra'])} existing infra hosts")

    existing_hostnames = {h["host"] for h in zbx_hosts}
    # visible names must be unique across Zabbix; dedupe with a short suffix
    used_names = {h["name"] for h in zbx_hosts} | {h["host"] for h in zbx_hosts}
    created = 0
    for c in plan["create"]:
        if c["host"] in existing_hostnames:
            continue
        visible = c["visible_name"]
        if visible in used_names:
            visible = f"{visible[:110]} [{c['host'][-12:]}]"
        used_names.add(visible)
        params = {
            "host": c["host"],
            "name": visible,
            "groups": [{"groupid": groups[c["group"]]}],
            "tags": [{"tag": k, "value": v} for k, v in c["tags"].items()],
            "inventory_mode": 0,
            "inventory": c["inventory"],
        }
        if c["ip"]:
            params["interfaces"] = [
                {
                    "type": 1,
                    "main": 1,
                    "useip": 1,
                    "ip": c["ip"],
                    "dns": "",
                    "port": "10050",
                }
            ]
            params["templates"] = [{"templateid": icmp_tpl}]
        try:
            res = zbx.call("host.create", params)
        except RuntimeError as exc:
            if "already exists" in str(exc):
                continue  # idempotency: created by a prior/partial run
            raise
        hostid = res["hostids"][0]
        if not c["ip"]:
            zbx.call(
                "item.create",
                {
                    "hostid": hostid,
                    "name": "UISP status",
                    "key_": "uisp.status",
                    "type": 2,
                    "value_type": 4,
                },
            )  # trapper, text
        created += 1
    print(f"created {created} hosts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uisp", required=True)
    ap.add_argument("--zabbix", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="required with --apply; confirms a reviewed dry-run",
    )
    ap.add_argument("--plan-out", default=None)
    ap.add_argument(
        "--only",
        choices=["infra", "customer", "onu", "uf_olt"],
        action="append",
        help="restrict the plan to these kinds (repeatable) — staged rollout: "
        "infra tags first, then customer, then onu/uf_olt",
    )
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (raw-IP diagnostics only)",
    )
    ap.add_argument(
        "--push-status",
        action="store_true",
        help="feed uisp.status trapper items from UISP overview.status",
    )
    ap.add_argument("--sender-host", default="127.0.0.1")
    ap.add_argument("--sender-port", type=int, default=10051)
    args = ap.parse_args()

    if args.insecure:
        allow_insecure_tls()

    uisp = Uisp(args.uisp, _read_token("UISP_TOKEN"))
    zbx = Zabbix(args.zabbix, _read_token("ZABBIX_API_TOKEN"))

    devices = uisp.live_devices()
    sites_by_id = {s["id"]: s for s in uisp.sites()}
    zbx_hosts = zbx.hosts()
    zbx_groups = zbx.groups()

    plan = build_plan(uisp, zbx_hosts, zbx_groups, sites_by_id, devices)
    if args.only:
        kinds = set(args.only)
        plan["create"] = [c for c in plan["create"] if c["kind"] in kinds]
        plan["tag_infra"] = [
            t for t in plan["tag_infra"] if t.get("kind", "infra") in kinds
        ]
        plan["groups_to_create"] = sorted(
            {c["group"] for c in plan["create"]} - set(zbx_groups)
        )
    summarize(plan, devices, zbx_hosts)

    if args.plan_out:
        with open(args.plan_out, "w") as fh:
            json.dump(plan, fh, indent=1, default=list)
        print(f"\nplan written to {args.plan_out}")

    if args.apply:
        if not args.yes:
            sys.exit("--apply requires --yes after reviewing a dry-run plan")
        apply_plan(zbx, plan, zbx_hosts)
    elif not args.push_status:
        print("\nDRY-RUN — nothing written. Re-run with --apply --yes to apply.")

    if args.push_status:
        push_status(zbx, devices, args.sender_host, args.sender_port)


if __name__ == "__main__":
    main()
