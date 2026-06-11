"""Import GenieACS devices unknown to the app as OntUnits + assignments.

The GenieACS fleet (~314 devices) contains ~142 devices with no ont_units
row — online TR-069 devices invisible to self-service. This links them by
matching the device's PPPoE username to an active AccessCredential.

Step 1 — extract (read-only, on the host) a CSV of device_id,serial,username:

  docker exec dotmac_sub_genieacs_mongodb mongo --quiet genieacs \\
    -u genieacs -p "$GENIEACS_MONGODB_PASSWORD" --authenticationDatabase admin \\
    --eval '
      function findUser(o){ if(!o||typeof o!=="object")return null;
        if(o.Username&&o.Username._value)return o.Username._value;
        for(var k in o){var r=findUser(o[k]);if(r)return r;} return null;}
      db.devices.find().forEach(function(d){
        var u=findUser(d.InternetGatewayDevice)||findUser(d.Device)||
              (d.VirtualParameters&&d.VirtualParameters.pppoeUsername&&
               d.VirtualParameters.pppoeUsername._value)||"";
        print(d._id+","+(d._id.split("-").pop())+","+u);})' > /tmp/acs_pppoe.csv

Step 2 — import (dry-run default):

  docker cp /tmp/acs_pppoe.csv dotmac_sub_app:/tmp/acs_pppoe.csv
  docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
    python -m scripts.migration.import_genieacs_onts /tmp/acs_pppoe.csv --execute

Rules: skip devices whose serial already has an ont_units row; skip blank or
ambiguous usernames (no credential match, or username matched by multiple
subscribers); never touches existing assignments.
"""

from __future__ import annotations

import csv
import logging
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import AccessCredential
from app.models.network import OntAssignment, OntUnit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run(csv_path: str, dry_run: bool = True) -> dict[str, int]:
    stats = {
        "rows": 0,
        "skipped_existing_serial": 0,
        "skipped_no_username": 0,
        "skipped_no_credential_match": 0,
        "created": 0,
    }
    db = SessionLocal()
    try:
        known_serials = {
            s
            for s in db.scalars(
                select(OntUnit.serial_number).where(OntUnit.serial_number.isnot(None))
            )
        }
        creds_by_username = {}
        for cred in db.scalars(
            select(AccessCredential).where(AccessCredential.is_active.is_(True))
        ):
            # last-wins is unsafe for linking hardware — mark dupes ambiguous.
            if cred.username in creds_by_username:
                creds_by_username[cred.username] = None
            else:
                creds_by_username[cred.username] = cred

        with open(csv_path) as fh:
            for device_id, serial, username in csv.reader(fh):
                stats["rows"] += 1
                serial = (serial or "").strip()
                username = (username or "").strip()
                if (
                    not serial
                    or serial.upper() in known_serials
                    or serial in known_serials
                ):
                    stats["skipped_existing_serial"] += 1
                    continue
                if not username:
                    stats["skipped_no_username"] += 1
                    logger.info("no PPPoE username on %s — skipped", device_id)
                    continue
                cred = creds_by_username.get(username)
                if cred is None:
                    stats["skipped_no_credential_match"] += 1
                    logger.info(
                        "no/ambiguous credential for %s (user=%s) — skipped",
                        device_id,
                        username,
                    )
                    continue

                logger.info(
                    "%s link %s (serial=%s) -> subscriber %s (user=%s)",
                    "DRY-RUN would" if dry_run else "creating",
                    device_id,
                    serial,
                    cred.subscriber_id,
                    username,
                )
                stats["created"] += 1
                if dry_run:
                    continue
                unit = OntUnit(serial_number=serial, is_active=True)
                db.add(unit)
                db.flush()
                db.add(
                    OntAssignment(
                        ont_unit_id=unit.id,
                        subscriber_id=cred.subscriber_id,
                        active=True,
                    )
                )
                known_serials.add(serial)
        if not dry_run:
            db.commit()
    finally:
        db.close()
    logger.info("done: %s", stats)
    return stats


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--execute"]
    if not args:
        print(__doc__)
        sys.exit(1)
    run(args[0], dry_run="--execute" not in sys.argv)
