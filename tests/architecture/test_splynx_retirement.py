"""Guard the retired Splynx surface against accidental resurrection.

See docs/designs/SPLYNX_RETIREMENT.md. Splynx was the pre-migration BSS; its
archive models were never populated (every table held zero rows) and are gone,
as is the legacy-BSS id-mapping path (``legacy_bss`` was unreachable — nothing
imported it, so its before_flush listener never registered).

**Two carve-outs matter as much as the removals**, and both are asserted
positively so a future "clean up splynx" sweep fails a test rather than a
filing or a money audit:

1. ``Subscriber.splynx_customer_id`` — the provenance reference CRM linkage
   resolves through, populated on 99.8% of subscribers. Retires with CRM.
2. ``splynx_billing_transactions`` — the restore target for the retained-backup
   adjudication workflow in ``scripts/one_off/``. Empty in production is not
   the same as "the backups are gone".
"""

from pathlib import Path

from app.db import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REMOVED_RUNTIME_PATHS = (
    "app/models/splynx_archive.py",
    "app/models/splynx_mapping.py",
    "app/services/splynx_mapping.py",
    "app/services/legacy_bss.py",
)

RETIRED_TABLES = (
    "splynx_archived_tickets",
    "splynx_archived_ticket_messages",
    "splynx_archived_quotes",
    "splynx_archived_quote_items",
    "portal_onboarding_states",
    "splynx_id_mappings",
)

RETIRED_SYMBOLS = (
    "SplynxArchivedTicket",
    "SplynxArchivedTicketMessage",
    "SplynxArchivedQuote",
    "SplynxArchivedQuoteItem",
    "PortalOnboardingState",
    "SplynxIdMapping",
    "SplynxEntityType",
    "_legacy_bss_customer_id",
)

RETIRED_MODULES = (
    "app.models.splynx_archive",
    "app.models.splynx_mapping",
    "app.services.splynx_mapping",
    "app.services.legacy_bss",
)


def test_splynx_archive_runtime_paths_stay_retired() -> None:
    present = [path for path in REMOVED_RUNTIME_PATHS if (PROJECT_ROOT / path).exists()]
    assert not present, "Retired Splynx runtime paths returned:\n  " + "\n  ".join(
        present
    )


def test_splynx_archive_imports_stay_absent() -> None:
    violations: list[str] = []
    roots = ("app", "scripts")
    forbidden = (*RETIRED_MODULES, *RETIRED_SYMBOLS)
    for root in roots:
        for path in (PROJECT_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: {marker}")
    assert not violations, (
        "Retired Splynx archive reference returned:\n  " + "\n  ".join(violations)
    )


def test_retired_tables_are_absent_from_the_model_registry() -> None:
    import app.models  # noqa: F401  (registers the full model graph)

    registered = set(Base.metadata.tables)
    resurrected = sorted(name for name in RETIRED_TABLES if name in registered)
    assert not resurrected, (
        "Retired Splynx tables are registered again — a model was re-added:\n  "
        + "\n  ".join(resurrected)
    )


def test_splynx_customer_id_is_deliberately_preserved() -> None:
    """The carve-out. splynx_customer_id is provenance, not archive residue:
    CRM linkage resolves through it for 99.8% of subscribers. It retires with
    CRM, not with the import archive."""
    import app.models  # noqa: F401

    subscribers = Base.metadata.tables["subscribers"]
    assert "splynx_customer_id" in subscribers.c, (
        "splynx_customer_id was removed from subscribers. It is NOT part of the "
        "Splynx archive retirement — crm_portal.resolve_crm_subscriber_id "
        "resolves the CRM link through it. See docs/designs/SPLYNX_RETIREMENT.md."
    )


def test_splynx_billing_transactions_is_deliberately_preserved() -> None:
    """The second carve-out. ``splynx_billing_transactions`` is the restore
    target for the retained-Splynx-backup adjudication workflow that
    ``scripts/one_off/billing_alignment_audit.py`` and
    ``audit_void_mirror_double_reversals.py`` run in an isolated environment —
    the latter uses the Splynx mirror as *proof* before soft-deleting contra
    debit ledger rows. The table being empty in production does not mean the
    backups are gone; dropping it would remove the schema they load into."""
    import app.models  # noqa: F401

    assert "splynx_billing_transactions" in Base.metadata.tables, (
        "splynx_billing_transactions was removed. It is NOT archive residue — "
        "it is a money-adjudication restore target. It retires only once that "
        "reconciliation is confirmed closed. See docs/designs/SPLYNX_RETIREMENT.md."
    )
