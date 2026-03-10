from __future__ import annotations

import csv
import io
from collections import Counter

from app.db import SessionLocal
from app.models.network import IpBlock, IpPool, IPv4Address
from app.services import web_network_ip


CSV_TEXT = """ID,Network,BM,RootNet,Used,Title,Location,Network type,Network category,Actions
1,160.119.124.0,24,None,,Range 1,All,EndNet,Dev,
2,160.119.125.0,24,None,,Range 2,All,EndNet,Dev,
3,160.119.126.0,24,None,,Range 3,All,EndNet,Dev,
4,172.16.100.0,24,None,,karsana Fallback IP 2,All,EndNet,Dev,
74,160.119.127.0,24,None,,management IP Block,All,EndNet,Dev,
76,102.220.188.0,24,None,,Range 4,Abuja,EndNet,Dev,
77,102.220.189.0,24,None,,Range 5,Lagos,EndNet,Dev,
78,102.220.190.0,24,None,,Range 6,Lagos,EndNet,Dev,
79,102.220.191.0,24,None,,Range 7,Lagos,EndNet,Dev,
80,10.10.10.0,24,None,,CSS IP Range,Lagos,EndNet,Dev,
81,10.10.11.0,24,None,,Allen IP Range,Lagos,EndNet,Dev,
83,172.16.115.0,24,None,,Point To Point IPs,All,EndNet,Dev,
84,172.16.8.0,24,None,,Private IP Block (Core Router),All,EndNet,Dev,
85,172.16.101.0,24,None,,CBD IP Range 2,Abuja,EndNet,Dev,
86,172.16.102.0,24,None,,Eagle FM Range,All,EndNet,Dev,
87,172.16.103.0,24,None,,Unlimited 4,All,EndNet,Dev,
88,172.16.104.0,24,None,,Unlimited 5,All,EndNet,Dev,
89,172.16.105.0,24,None,,Unlimited 6,All,EndNet,Dev,
90,172.16.106.0,24,None,,Unlimited 7,All,EndNet,Dev,
91,172.16.107.0,24,None,,Jabi IP Range,All,EndNet,Dev,
92,172.16.108.0,24,None,,Kubwa IP Range,Abuja,EndNet,Dev,
93,172.16.109.0,24,None,,Lokogoma IP Range,Abuja,EndNet,Dev,
96,172.16.129.0,24,None,,IDU IP Range,Abuja,EndNet,Dev,
98,172.16.90.0,24,None,,Lugbe IP RANGE,Abuja,EndNet,Dev,
99,172.16.99.0,24,None,,Garki IP Range,Abuja,EndNet,Dev,
100,172.16.98.0,24,None,,Karsana IP Range,Abuja,EndNet,Dev,
101,172.16.130.0,24,None,,CBD IP Range,All,EndNet,Dev,
102,172.16.131.0,24,None,,Gwarimpa IP Block,All,EndNet,Dev,
103,172.16.132.0,24,None,,SPDC IP Range,All,EndNet,Dev,
104,172.16.133.0,24,None,,Maitama IP Range,All,EndNet,Dev,
105,172.16.134.0,24,None,,BOI IP Range,All,EndNet,Dev,
106,172.16.135.0,24,None,,Gudu IP Range,All,EndNet,Dev,
107,172.16.136.0,24,None,,Gwarimpa IP Block 2,Abuja,EndNet,Dev,
108,172.16.137.0,24,None,,AFR IP RANGE,All,EndNet,Dev,
109,172.16.138.0,24,None,,Jabi IP Range 2,Abuja,EndNet,Dev,
110,102.216.193.0,24,None,,Megamore Block  (Reserved),Abuja,EndNet,Dev,
111,172.16.139.0,24,None,,Gudu IP Range 2,All,EndNet,Dev,
112,172.16.140.0,24,None,,Airport IP Range,Abuja,EndNet,Dev,
113,172.16.141.0,24,None,,BOI IP Range 2,All,EndNet,Dev,
114,172.16.142.0,24,None,,SPDC IP Range 2,All,EndNet,Dev,
115,172.16.143.0,24,None,,Karsana IP Range 2,Abuja,EndNet,Dev,
116,172.16.116.0,24,None,,CBD Fallback IP,Abuja,EndNet,Dev,
117,172.16.117.0,24,None,,Karsana Fallback IP,Abuja,EndNet,Dev,
118,172.16.120.0,24,None,,Kubwa Fallback IP,Abuja,EndNet,Dev,
119,172.16.118.0,24,None,,Gudu Fallback IP,Abuja,EndNet,Dev,
120,172.16.121.0,24,None,,Lokogoma Fallback IP,Abuja,EndNet,Dev,
121,172.16.111.0,24,None,,Maitama Fallback IP,Abuja,EndNet,Dev,
122,172.16.114.0,24,None,,Gwarimpa Fallback IP,Abuja,EndNet,Dev,
123,172.16.128.0,24,None,,Karu Fallback IP,Abuja,EndNet,Dev,
124,172.16.119.0,24,None,,Idu Fallback IP,Abuja,EndNet,Dev,
125,172.16.112.0,24,None,,AFR Fallback IP,Abuja,EndNet,Dev,
126,172.16.151.0,24,None,,Airport Fallback IP,Abuja,EndNet,Dev,
127,172.16.152.0,24,None,,Mpape Fallback IP,Abuja,EndNet,Dev,
128,172.16.127.0,24,None,,Jabi Fallback IP,Abuja,EndNet,Dev,
129,172.16.126.0,24,None,,Lugbe Fallback IP,Abuja,EndNet,Dev,
130,172.16.150.0,24,None,,Eagle FM Fallback IP,Abuja,EndNet,Dev,
131,172.20.0.0,24,None,,CSS Fallback IP,Lagos,EndNet,Dev,
132,172.21.0.0,24,None,,Allen Fallback IP,Lagos,EndNet,Dev,
133,172.21.3.0,24,None,,Ilupeju Fallback IP,Lagos,EndNet,Dev,
134,172.21.1.0,24,None,,Abule Egba Fallback IP,Lagos,EndNet,Dev,
135,172.21.2.0,24,None,,Apapa Fallback IP,Lagos,EndNet,Dev,
136,172.16.153.0,24,None,,Garki Fallback IP,Abuja,EndNet,Dev,
138,172.16.123.0,24,None,,SPDC Fallback IP,Abuja,EndNet,Dev,
144,10.10.14.0,24,None,,Apapa IP Range,Lagos,EndNet,Dev,
145,10.10.15.0,24,None,,Surulere IP Range,Lagos,EndNet,Dev,
146,10.10.16.0,24,None,,Ilupeju IP Range,Lagos,EndNet,Dev,
147,172.21.20.0,24,None,,Point To Point IPs,Lagos,EndNet,Dev,
148,172.21.4.0,24,None,,Surulere Fallback IP,Lagos,EndNet,Dev,
149,172.21.5.0,24,None,,Dopemu Fallback Ip,Lagos,EndNet,Dev,
150,10.10.17.0,24,None,,Dopemu IP range,Lagos,EndNet,Dev,
152,172.16.250.0,24,None,,AirFiber IP Management Range,Abuja,EndNet,Dev,
153,102.216.192.0,24,None,,Megamore Block-2  (Reserved),Abuja,EndNet,Dev,
154,172.16.200.0,24,None,,AirFiber IP Management Range,Lagos,EndNet,Dev,
156,172.16.144.0,24,None,,GWARIMPA IP RANGE-3,Abuja,EndNet,Dev,
157,172.16.145.0,24,None,,Jabi IP Range 3,Abuja,EndNet,Dev,
158,172.16.154.0,24,None,,Aso Fallback IP,Abuja,EndNet,Dev,
159,10.120.120.0,24,None,,Dell Server IP Block,Abuja,EndNet,Dev,
160,10.120.121.0,24,None,,HP Server IP Block,Abuja,EndNet,Dev,
161,172.16.201.0,24,None,,fallback ip,Abuja,EndNet,Dev,
162,172.30.100.0,24,None,,Test,All,EndNet,Dev,
163,10.10.20.0,24,None,,Lagos Medallion FIRS IPs Private,Lagos,EndNet,Dev,
"""


def _rows() -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(CSV_TEXT)))


def _build_pool_payloads() -> list[dict[str, object]]:
    rows = _rows()
    title_counts = Counter((row.get("Title") or "").strip() for row in rows)
    payloads: list[dict[str, object]] = []
    for row in rows:
        title = (row.get("Title") or "").strip()
        network = (row.get("Network") or "").strip()
        bm = (row.get("BM") or "").strip()
        cidr = f"{network}/{bm}" if network and bm else network
        unique_name = title or cidr
        if title_counts.get(title, 0) > 1:
            unique_name = f"{title} ({cidr})"
        payloads.append(
            {
                "name": unique_name,
                "ip_version": "ipv4",
                "cidr": cidr,
                "gateway": None,
                "dns_primary": None,
                "dns_secondary": None,
                "notes": f"Imported range ID {row.get('ID')}".strip(),
                "location": (row.get("Location") or "").strip() or None,
                "category": (row.get("Network category") or "").strip() or None,
                "network_type": (row.get("Network type") or "").strip() or None,
                "router": None,
                "usage_type": "static",
                "allow_network_broadcast": False,
                "is_active": True,
                "is_fallback": "fallback" in title.lower(),
                "block_notes": title or None,
            }
        )
    return payloads


def main() -> None:
    db = SessionLocal()
    try:
        existing_pools = db.query(IpPool).all()
        existing_blocks = db.query(IpBlock).all()
        linked_addresses = (
            db.query(IPv4Address)
            .filter(IPv4Address.pool_id.isnot(None))
            .count()
        )
        if linked_addresses:
            raise RuntimeError(
                f"Refusing destructive backfill: {linked_addresses} IPv4 addresses are already linked to pools."
            )

        for block in existing_blocks:
            db.delete(block)
        for pool in existing_pools:
            db.delete(pool)
        db.commit()

        created_pools = []
        created_blocks = []
        for payload in _build_pool_payloads():
            block_notes = payload.pop("block_notes")
            pool, error = web_network_ip.create_ip_pool(db, payload)
            if error or pool is None:
                raise RuntimeError(f"Failed to create pool {payload['name']} ({payload['cidr']}): {error}")
            created_pools.append(pool)
            block, error = web_network_ip.create_ip_block(
                db,
                {
                    "pool_id": str(pool.id),
                    "cidr": str(pool.cidr),
                    "is_active": True,
                    "notes": block_notes,
                },
            )
            if error or block is None:
                raise RuntimeError(f"Failed to create block for {pool.name} ({pool.cidr}): {error}")
            created_blocks.append(block)

        reconcile_result = web_network_ip.reconcile_ipv4_pool_memberships(db)
        print(
            {
                "deleted_pools": len(existing_pools),
                "deleted_blocks": len(existing_blocks),
                "created_pools": len(created_pools),
                "created_blocks": len(created_blocks),
                "reconcile": reconcile_result,
            }
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
