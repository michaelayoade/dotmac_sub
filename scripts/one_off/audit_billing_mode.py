"""Report billing_mode drift across account / subscription / offer.

Run before enabling the local billing runner (Splynx cutover): surfaces
accounts where Subscriber.billing_mode, Subscription.billing_mode, and the
offer's billing_mode disagree, or where an account holds mixed-mode active
subscriptions. Read-only.

    python -m scripts.one_off.audit_billing_mode
"""

from __future__ import annotations

from collections import Counter

from app.db import SessionLocal
from app.services.billing_mode_audit import find_billing_mode_inconsistencies


def main() -> int:
    db = SessionLocal()
    try:
        issues = find_billing_mode_inconsistencies(db)
    finally:
        db.close()

    if not issues:
        print("billing_mode audit: OK — no account/subscription/offer drift found.")
        return 0

    by_kind = Counter(i["issue"] for i in issues)
    print(f"billing_mode audit: {len(issues)} issue(s) found")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count}")
    print()
    for i in issues:
        print("  " + ", ".join(f"{k}={v}" for k, v in i.items()))
    # Non-zero exit so this can gate a pre-cutover check / CI job.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
