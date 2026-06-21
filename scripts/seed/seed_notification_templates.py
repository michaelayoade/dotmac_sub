#!/usr/bin/env python3
"""RETIRED — do not run.

This script previously seeded notification templates using DOUBLE-brace
``{{variable}}`` syntax and variables the runtime renderers never produce
(``payment_link``, ``company_name``, ``customer_name``, ``days_overdue``,
``short_link`` ...), plus alarming all-caps debt copy ("URGENT", "OVERDUE",
"YOUR SERVICE WILL BE SUSPENDED").

The live event renderer (events/handlers/notification.py:_render_text) only
substitutes SINGLE-brace ``{variable}`` for variables in its render context, so
rows written by this script went out to customers with literal ``{{amount}}`` /
blank placeholders. It is the root cause of the "blank template" incident.

Use these instead:

* Defaults are seeded automatically at app startup by
  ``app/services/settings_seed.py`` (_seed_missing_notification_templates),
  which uses single-brace syntax and only supported variables.
* To fix bad rows already in the database, run
  ``scripts/one_off/repair_notification_templates.py`` (dry-run by default).
* To audit what is stored, run
  ``scripts/one_off/audit_notification_templates.py``.
"""

import sys


def main() -> int:
    sys.stderr.write(
        "seed_notification_templates.py is RETIRED and intentionally does "
        "nothing.\nIt previously seeded broken double-brace templates that "
        "leaked literal {{placeholders}} to customers.\n\n"
        "Use instead:\n"
        "  - startup seeding via app/services/settings_seed.py (automatic)\n"
        "  - scripts/one_off/repair_notification_templates.py  (fix stored rows)\n"
        "  - scripts/one_off/audit_notification_templates.py   (inspect rows)\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
