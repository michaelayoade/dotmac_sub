#!/usr/bin/env python3
"""Check ONT auto-discovery on all active OLTs."""

import sys
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.web_network_ont_autofind import refresh_autofind_from_olt


def main() -> None:
    db = SessionLocal()
    try:
        # Get all active OLTs
        stmt = select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
        olts = list(db.scalars(stmt).all())

        if not olts:
            print("No active OLTs found.")
            return

        print(f"Checking autofind on {len(olts)} OLT(s)...\n")
        print("-" * 80)

        total_created = 0
        total_updated = 0
        total_disappeared = 0
        successful = 0
        failed = 0

        for olt in olts:
            print(f"\n{olt.name} ({olt.mgmt_ip})")
            ok, message, stats = refresh_autofind_from_olt(db, olt_id=str(olt.id))

            if ok:
                successful += 1
                created = stats.get("created", 0)
                updated = stats.get("updated", 0)
                disappeared = stats.get("disappeared", 0)
                total_created += created
                total_updated += updated
                total_disappeared += disappeared

                if created or updated or disappeared:
                    print(f"  ✓ {message}")
                    print(f"    New: {created}, Updated: {updated}, Disappeared: {disappeared}")
                else:
                    print(f"  ✓ No undiscovered ONTs")
            else:
                failed += 1
                print(f"  ✗ {message}")

        db.commit()

        print("\n" + "-" * 80)
        print(f"\nSummary:")
        print(f"  OLTs checked: {len(olts)} ({successful} successful, {failed} failed)")
        print(f"  New candidates: {total_created}")
        print(f"  Updated candidates: {total_updated}")
        print(f"  Disappeared: {total_disappeared}")

    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
