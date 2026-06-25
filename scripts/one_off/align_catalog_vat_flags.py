"""Align catalog VAT checkbox with positive VAT percentages.

Imported offers can carry ``vat_percent=7.50`` while ``with_vat=false``. The
billing resolver treats a positive percent as taxable, but aligning the boolean
keeps the admin catalog readable: taxable services show VAT enabled, exempt
services remain ``with_vat=false`` with no positive percent.

Dry-run by default.
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.catalog import CatalogOffer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write updates.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = (
            db.query(CatalogOffer)
            .filter(CatalogOffer.vat_percent.isnot(None))
            .filter(CatalogOffer.vat_percent > 0)
            .filter(CatalogOffer.with_vat.is_(False))
            .order_by(CatalogOffer.name.asc())
            .all()
        )
        if args.apply:
            for offer in rows:
                offer.with_vat = True
            db.commit()

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"catalog VAT flag alignment — {mode}")
        print(f"offers_to_mark_with_vat: {len(rows)}")
        for offer in rows[:50]:
            print(f"{offer.id}\t{offer.name}\t{offer.code}\t{offer.vat_percent}")
        if len(rows) > 50:
            print(f"... {len(rows) - 50} more")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
