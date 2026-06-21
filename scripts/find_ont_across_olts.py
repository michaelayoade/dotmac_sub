from __future__ import annotations

import sys
import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont.status import find_ont_by_serial

SERIALS = (
    "HWTC7D4518C3",
    "485754437D4518C3",
)


def main() -> int:
    started = time.time()
    with SessionLocal() as db:
        olts = db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()

        for olt in olts:
            print(
                {
                    "step": "olt_start",
                    "elapsed_s": round(time.time() - started, 2),
                    "olt": olt.name,
                    "host": olt.mgmt_ip or olt.hostname,
                    "user": olt.ssh_username,
                },
                flush=True,
            )
            for serial in SERIALS:
                try:
                    ok, msg, entry = find_ont_by_serial(olt, serial)
                    print(
                        {
                            "step": "serial_result",
                            "elapsed_s": round(time.time() - started, 2),
                            "olt": olt.name,
                            "serial": serial,
                            "ok": ok,
                            "msg": msg,
                            "entry": None
                            if entry is None
                            else {
                                "fsp": entry.fsp,
                                "ont_id": entry.onu_id,
                                "run_state": entry.run_state,
                                "serial": entry.real_serial,
                            },
                        },
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        {
                            "step": "serial_exception",
                            "elapsed_s": round(time.time() - started, 2),
                            "olt": olt.name,
                            "serial": serial,
                            "type": type(exc).__name__,
                            "detail": str(exc),
                        },
                        flush=True,
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
