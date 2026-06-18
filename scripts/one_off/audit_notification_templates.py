#!/usr/bin/env python3
"""Read-only, context-aware audit of notification_templates.

For every stored row it reports:
  * double-brace ``{{var}}`` syntax (the live event renderer leaks this literal),
  * placeholders invalid for the template's ACTUAL send context — event context
    for automated codes, bulk context otherwise,
  * the dangerous in-between case: "valid under the union of contexts but invalid
    for the automated event context" (an automated template using a bulk-only
    variable like {customer_name} that previews fine but ships literal).

Usage (inside the app container)::

    python scripts/one_off/audit_notification_templates.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models.notification import NotificationTemplate  # noqa: E402
from app.services.notification_template_renderer import (  # noqa: E402
    KNOWN_PLACEHOLDERS,
    allowed_variables_for_code,
    automated_template_codes,
)

_DOUBLE = re.compile(r"\{\{\s*([\w]+)\s*\}\}")
_SINGLE = re.compile(r"(?<!\{)\{\s*([\w]+)\s*\}(?!\})")


def names(text: str) -> tuple[set[str], set[str]]:
    return set(_DOUBLE.findall(text or "")), set(_SINGLE.findall(text or ""))


def main() -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(NotificationTemplate)
            .order_by(NotificationTemplate.code, NotificationTemplate.channel)
            .all()
        )
    finally:
        db.close()

    automated = automated_template_codes()
    print(f"{len(rows)} template rows; {len(automated)} automated event codes\n")

    double_rows, context_bad, sneaky = [], [], []
    for t in rows:
        chan = getattr(t.channel, "value", str(t.channel))
        blob = f"{t.subject or ''}\n{t.body or ''}"
        dbl, single = names(blob)
        allowed, label = allowed_variables_for_code(t.code)
        ctx_unknown = {n for n in single if n not in allowed}
        union_unknown = {n for n in single if n not in KNOWN_PLACEHOLDERS}

        flags = []
        if dbl:
            flags.append("DOUBLE-BRACE")
            double_rows.append((t.code, chan, sorted(dbl)))
        if ctx_unknown:
            flags.append(f"BAD-FOR-{label.split()[0].upper()}")
            context_bad.append((t.code, chan, label, sorted(ctx_unknown)))
            # valid under the union of contexts, but invalid for its own context
            if not (ctx_unknown - union_unknown) and not (union_unknown & ctx_unknown):
                pass
            if ctx_unknown and not union_unknown:
                sneaky.append((t.code, chan, label, sorted(ctx_unknown)))

        marker = f"  <-- {', '.join(flags)}" if flags else ""
        print(f"[{chan:5}] {t.code:32} ({label}) | {(t.subject or '(no subject)')[:45]}{marker}")

    def section(title, items):
        print(f"\n=== {title} ===")
        if not items:
            print("  none")
        for it in items:
            print("  " + " | ".join(str(x) for x in it))

    section("DOUBLE-BRACE rows (leak literal on event send)", double_rows)
    section("placeholders invalid for the template's send context", context_bad)
    section(
        "valid globally but INVALID for automated event context "
        "(previews fine, ships literal)",
        sneaky,
    )


if __name__ == "__main__":
    main()
