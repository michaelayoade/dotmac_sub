"""Phase 1 step 3 — provision dotmac-captive pool + firewall rules on Mikrotiks.

See docs/radius_state_refactor/phase1_groups_and_pools.md.

Pushes (idempotently) per Mikrotik NAS:
  * /ip pool             name=dotmac-captive-pool
  * /ip firewall address-list  list=dotmac-captive  address=<cidr>
  * /ip firewall filter  4 rules tagged dotmac-captive-*
  * /ip firewall nat     1 rule  dotmac-captive-redirect-http

All commands wrap in `:if ([:len [find ...]] = 0) do={ add ... }` so
re-running is a no-op. Read the portal IP from DomainSetting
`captive_portal_ip`.

Usage (via the app container so PYTHONPATH + DB env are right):

    # Dry-run, all NASes
    docker exec dotmac_sub_app python scripts/migration/phase1_provision_captive_pool.py --all --dry-run

    # Target one NAS by name substring
    docker exec dotmac_sub_app python scripts/migration/phase1_provision_captive_pool.py --nas-name 'test router'

    # Fleet (after canary)
    docker exec dotmac_sub_app python scripts/migration/phase1_provision_captive_pool.py --all
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import NasDevice, NasVendor
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusClient, RadiusServer
from app.services.nas import DeviceProvisioner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("phase1_captive")

CAPTIVE_LIST = "dotmac-captive"
CAPTIVE_POOL = "dotmac-captive-pool"
OSS_PORTS = "80,443,8101,8102,8103,8104"

# Each command uses a stable comment so the conditional find can detect
# the prior install. Re-runs are no-ops.
_RULES = [
    {
        "comment": "dotmac-captive-allow-dns",
        "find_path": "/ip firewall filter",
        "add": (
            'add chain=forward src-address-list="{LIST}" '
            "protocol=udp dst-port=53 action=accept "
            'comment="dotmac-captive-allow-dns"'
        ),
    },
    {
        "comment": "dotmac-captive-allow-oss",
        "find_path": "/ip firewall filter",
        "add": (
            'add chain=forward src-address-list="{LIST}" '
            'dst-address="{PORTAL}" '
            f"protocol=tcp dst-port={OSS_PORTS} action=accept "
            'comment="dotmac-captive-allow-oss"'
        ),
    },
    {
        "comment": "dotmac-captive-redirect-http",
        "find_path": "/ip firewall nat",
        "add": (
            'add chain=dstnat src-address-list="{LIST}" '
            "protocol=tcp dst-port=80 action=dst-nat "
            'to-addresses="{PORTAL}" to-ports=80 '
            'comment="dotmac-captive-redirect-http"'
        ),
    },
    {
        # Drop everything else from the captive list. Must be last so
        # the allow-dns / allow-oss rules above match first.
        "comment": "dotmac-captive-drop-other",
        "find_path": "/ip firewall filter",
        "add": (
            'add chain=forward src-address-list="{LIST}" action=drop '
            'comment="dotmac-captive-drop-other"'
        ),
    },
]


def _pool_range_from_cidr(cidr: str) -> str:
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts())
    if len(hosts) < 2:
        raise ValueError(f"CIDR {cidr} has too few host addresses")
    return f"{hosts[0]}-{hosts[-1]}"


def _portal_ip(db) -> str:
    setting = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.radius)
        .where(DomainSetting.key == "captive_portal_ip")
    ).first()
    if not setting or not setting.value_text:
        raise RuntimeError(
            "DomainSetting radius.captive_portal_ip is not set; configure "
            "it in admin > settings before running this script."
        )
    return setting.value_text.strip()


def _active_mikrotiks(db) -> list[NasDevice]:
    devices = db.scalars(
        select(NasDevice)
        .join(RadiusClient, RadiusClient.nas_device_id == NasDevice.id)
        .join(RadiusServer, RadiusServer.id == RadiusClient.server_id)
        .where(NasDevice.is_active.is_(True))
        .where(NasDevice.vendor == NasVendor.mikrotik)
        .where(RadiusClient.is_active.is_(True))
        .where(RadiusServer.is_active.is_(True))
    ).all()
    by_id = {str(d.id): d for d in devices}
    return sorted(by_id.values(), key=lambda d: d.name or "")


def build_commands(pool_cidr: str, portal_ip: str) -> list[str]:
    pool_range = _pool_range_from_cidr(pool_cidr)
    cmds: list[str] = []

    # 1. IP pool
    cmds.append(
        f':if ([:len [/ip pool find name="{CAPTIVE_POOL}"]] = 0) '
        f'do={{/ip pool add name="{CAPTIVE_POOL}" '
        f'ranges="{pool_range}" '
        f'comment="dotmac access-state captive pool"}}'
    )

    # 2. Address-list
    cmds.append(
        f":if ([:len [/ip firewall address-list find "
        f'list="{CAPTIVE_LIST}" address="{pool_cidr}"]] = 0) '
        f"do={{/ip firewall address-list add "
        f'list="{CAPTIVE_LIST}" address="{pool_cidr}" '
        f'comment="dotmac access-state captive pool"}}'
    )

    # 3-6. Filter + NAT rules
    for rule in _RULES:
        add_clause = rule["add"].format(LIST=CAPTIVE_LIST, PORTAL=portal_ip)
        cmds.append(
            f":if ([:len [{rule['find_path']} find "
            f'comment="{rule["comment"]}"]] = 0) '
            f"do={{{rule['find_path']} {add_clause}}}"
        )

    return cmds


def provision_nas(device: NasDevice, commands: list[str], *, dry_run: bool) -> bool:
    label = f"{device.name} ({device.management_ip or device.ip_address})"
    if dry_run:
        logger.info("DRY-RUN %s — would push %d commands:", label, len(commands))
        for c in commands:
            logger.info("  %s", c)
        return True
    try:
        with DeviceProvisioner.ssh_session(device) as ssh:
            for c in commands:
                ssh.execute(c)
        logger.info("OK    %s — %d commands applied", label, len(commands))
        return True
    except Exception as exc:
        logger.error("FAIL  %s — %s", label, exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--nas-name",
        help="Substring of NAS name to target (canary single-NAS push).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Push to every active Mikrotik with an active RADIUS client.",
    )
    parser.add_argument(
        "--cidr",
        default="10.255.0.0/16",
        help="Captive pool CIDR (default: 10.255.0.0/16)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands per NAS without executing.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        portal_ip = _portal_ip(db)
        commands = build_commands(args.cidr, portal_ip)

        all_devices = _active_mikrotiks(db)
        if args.nas_name:
            substr = args.nas_name.lower()
            targets = [d for d in all_devices if substr in (d.name or "").lower()]
            if not targets:
                logger.error("No Mikrotik NAS matched name substring %r", args.nas_name)
                return 1
        else:
            targets = all_devices

        logger.info(
            "captive pool CIDR=%s portal=%s targets=%d %s",
            args.cidr,
            portal_ip,
            len(targets),
            "(dry-run)" if args.dry_run else "",
        )

        ok = 0
        fail = 0
        for device in targets:
            if provision_nas(device, commands, dry_run=args.dry_run):
                ok += 1
            else:
                fail += 1

        logger.info("done — ok=%d fail=%d", ok, fail)
        return 0 if fail == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
