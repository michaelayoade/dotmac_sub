"""Guard the retired Splynx import archive against accidental resurrection.

See docs/designs/SPLYNX_RETIREMENT.md. Splynx was the pre-migration BSS; its
archive models were never populated (every table held zero rows) and are gone.

The carve-out matters as much as the removal: ``Subscriber.splynx_customer_id``
is NOT retired. It is the provenance reference CRM linkage resolves through,
populated on 99.8% of subscribers, and it retires with CRM. It is asserted
positively below so that a future "clean up splynx" sweep that reaches for it
fails a test instead of a regulatory filing.
"""

from pathlib import Path

from app.db import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REMOVED_RUNTIME_PATHS = ("app/models/splynx_archive.py",)

RETIRED_TABLES = (
    "splynx_archived_tickets",
    "splynx_archived_ticket_messages",
    "splynx_archived_quotes",
    "splynx_archived_quote_items",
    "portal_onboarding_states",
)

RETIRED_SYMBOLS = (
    "SplynxArchivedTicket",
    "SplynxArchivedTicketMessage",
    "SplynxArchivedQuote",
    "SplynxArchivedQuoteItem",
    "PortalOnboardingState",
)


def test_splynx_archive_runtime_paths_stay_retired() -> None:
    present = [path for path in REMOVED_RUNTIME_PATHS if (PROJECT_ROOT / path).exists()]
    assert not present, "Retired Splynx runtime paths returned:\n  " + "\n  ".join(
        present
    )


def test_splynx_archive_imports_stay_absent() -> None:
    violations: list[str] = []
    roots = ("app", "scripts")
    forbidden = ("app.models.splynx_archive", *RETIRED_SYMBOLS)
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
