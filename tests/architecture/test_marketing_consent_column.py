"""The marketing-send decision must own `Subscriber.marketing_opt_in`.

The existing consent guard (`test_communication_eligibility_ownership.py`) keys on
the `may_send` / `is_marketing` *functions*, so a marketing-send decision expressed
as a direct read of the `marketing_opt_in` **column** is invisible to it — exactly
how `comms_campaigns` and `communication_intents` came to gate marketing sends on
the column instead of asking the eligibility owner (2026-07-14 audit finding).

This ratchets the column: every module that reads `marketing_opt_in` must be either
the eligibility owner, a classified display/CRUD/schema reader (allowlist), or a
tracked send-gate bypass (shrink-only baseline). A new module gating a send on the
column can no longer land silently — it must move the decision into the owner or be
explicitly classified.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

COLUMN = "marketing_opt_in"

# The consent owner. It should own the marketing-send rule; it is always allowed
# to read the column (today it does not yet — moving the rule in is the follow-up).
OWNER = "app/services/communication_eligibility.py"

# Legitimate non-send readers: forms/CRUD that SET the flag, schemas that
# serialize it, and admin table display/filter. Not a marketing-send decision.
DISPLAY_CRUD_ALLOWLIST = {
    "app/web/admin/customers.py",
    "app/schemas/subscriber.py",
    "app/api/defaults.py",
    "app/services/web_customer_actions.py",
    "app/services/web_subscriber_actions.py",
    "app/services/table_config.py",
    "app/services/smart_defaults.py",
}

# Shrink-only debt: modules that gate a marketing send on the column directly
# instead of asking the eligibility owner. Remove an entry once its decision is
# routed through the owner.
SEND_GATE_BYPASS_BASELINE = {
    "app/services/comms_campaigns.py",
    "app/services/communication_intents.py",
}


def _column_readers() -> set[str]:
    readers: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(PROJECT_ROOT))
        # the model defines the column; migrations reference the schema — neither
        # is a business read of the consent decision.
        if rel == "app/models/subscriber.py" or "/alembic/" in rel or "test" in rel:
            continue
        if COLUMN in path.read_text(encoding="utf-8"):
            readers.add(rel)
    return readers


def test_only_classified_modules_read_the_marketing_consent_column() -> None:
    readers = _column_readers()
    allowed = {OWNER} | DISPLAY_CRUD_ALLOWLIST | SEND_GATE_BYPASS_BASELINE
    unexpected = sorted(readers - allowed)
    assert not unexpected, (
        "module(s) reading Subscriber.marketing_opt_in without classification. "
        "If this gates a marketing send, route the decision through "
        "communication_eligibility (may_send/is_marketing) instead of the column; "
        "if it is display/CRUD/schema, add it to DISPLAY_CRUD_ALLOWLIST:\n  "
        + "\n  ".join(unexpected)
    )


def test_send_gate_bypass_baseline_only_shrinks() -> None:
    readers = _column_readers()
    stale = sorted(SEND_GATE_BYPASS_BASELINE - readers)
    assert not stale, (
        "these modules no longer read marketing_opt_in — remove them from "
        "SEND_GATE_BYPASS_BASELINE so the debt reflects reality:\n  "
        + "\n  ".join(stale)
    )
