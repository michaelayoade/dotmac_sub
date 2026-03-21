"""Phase 3: Migrate operational data from Splynx.

Migrates:
1. tickets + ticket_messages → SplynxArchivedTicket + SplynxArchivedTicketMessage
2. mail_pool → CommunicationLog (email)
3. sms_pool → CommunicationLog (sms)
4. mrr_statistics → MrrSnapshot
5. credit_notes_to_invoices → CreditNoteApplication
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    fetch_batched,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val
    try:
        from datetime import date as date_type

        if isinstance(val, date_type):
            return datetime(val.year, val.month, val.day, tzinfo=UTC)
    except (ValueError, TypeError):
        pass
    return None


def _resolve_credit_note_application_amount(
    row: dict,
    *,
    credit_note_total: Decimal,
    application_count: int,
) -> Decimal | None:
    """Resolve application amount conservatively.

    Prefer explicit amount columns from Splynx when present. Only fall back to
    the full credit note total when there is exactly one linked invoice.
    """
    for key in ("amount", "total", "sum"):
        raw = row.get(key)
        if raw not in (None, ""):
            return Decimal(str(raw))
    if application_count == 1:
        return credit_note_total
    return None


def migrate_tickets(conn, db, customer_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate Splynx tickets → SplynxArchivedTicket + Messages."""
    from app.models.splynx_archive import (
        SplynxArchivedTicket,
        SplynxArchivedTicketMessage,
    )

    existing_ticket_ids = set(
        db.scalars(select(SplynxArchivedTicket.splynx_ticket_id)).all()
    )

    query = """
        SELECT t.*, ts.title_for_agent as status_name
        FROM ticket t
        LEFT JOIN ticket_statuses ts ON t.status_id = ts.id
        WHERE t.deleted='0'
        ORDER BY t.id
    """
    tickets = fetch_all(conn, query)
    created = 0

    for row in tickets:
        if row["id"] in existing_ticket_ids:
            continue
        subscriber_id = customer_mapping.get(row.get("customer_id"))

        ticket = SplynxArchivedTicket(
            splynx_ticket_id=row["id"],
            subscriber_id=subscriber_id,
            subject=(row.get("subject") or "")[:255] or "No subject",
            status=row.get("status_name") or "open",
            priority=(row.get("priority") or "normal")[:120],
            assigned_to=str(row.get("assign_to") or ""),
            body=(row.get("note") or "")[:10000] if row.get("note") else None,
            splynx_metadata={
                "group_id": row.get("group_id"),
                "type_id": row.get("type_id"),
                "source": row.get("source"),
                "closed": row.get("closed"),
                "reporter_type": row.get("reporter_type"),
            },
            is_active=True,
        )
        # Set created_at from Splynx
        ticket.created_at = _parse_date(row.get("created_at")) or datetime.now(UTC)
        db.add(ticket)
        created += 1

    db.flush()
    logger.info("Tickets: %d created", created)

    # Migrate messages
    msg_query = """
        SELECT * FROM ticket_messages
        WHERE deleted='0'
        ORDER BY ticket_id, id
    """
    existing_message_ids = set(
        db.scalars(select(SplynxArchivedTicketMessage.splynx_message_id)).all()
    )
    # Build ticket ID → UUID mapping
    ticket_map = {
        t.splynx_ticket_id: t.id
        for t in db.scalars(select(SplynxArchivedTicket)).all()
    }

    msg_created = 0
    for batch in fetch_batched(conn, msg_query, batch_size=2000):
        for row in batch:
            if row["id"] in existing_message_ids:
                continue
            ticket_id = ticket_map.get(row.get("ticket_id"))
            if not ticket_id:
                continue

            # Decode message body (stored as longblob)
            body = row.get("message")
            if isinstance(body, (bytes, bytearray)):
                try:
                    body = body.decode("utf-8", errors="replace")
                except Exception:
                    body = str(body)
            body = (body or "")[:50000]

            sender_type = row.get("author_type") or "system"
            msg = SplynxArchivedTicketMessage(
                ticket_id=ticket_id,
                splynx_message_id=row["id"],
                sender_type=sender_type[:40],
                sender_name=str(row.get("admin_id") or row.get("customer_id") or "")[:120],
                body=body,
                is_internal=row.get("message_type") == "note",
            )
            msg.created_at = _parse_date(row.get("date")) or datetime.now(UTC)
            db.add(msg)
            existing_message_ids.add(row["id"])
            msg_created += 1

        db.flush()

    logger.info("Ticket messages: %d created", msg_created)


def migrate_emails(conn, db, customer_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate Splynx mail_pool → CommunicationLog."""
    from app.models.communication_log import (
        CommunicationChannel,
        CommunicationDirection,
        CommunicationLog,
        CommunicationStatus,
    )

    existing_message_ids = set(
        db.scalars(
            select(CommunicationLog.splynx_message_id).where(
                CommunicationLog.channel == CommunicationChannel.email,
                CommunicationLog.splynx_message_id.isnot(None),
            )
        ).all()
    )

    status_map = {
        "sent": CommunicationStatus.sent,
        "new": CommunicationStatus.pending,
        "sending": CommunicationStatus.pending,
        "error": CommunicationStatus.failed,
        "expired": CommunicationStatus.failed,
        "attachment_error": CommunicationStatus.failed,
    }

    query = "SELECT id, customer_id, recipient, subject, status, datetime_added, datetime_sent FROM mail_pool ORDER BY id"
    created = 0

    for batch in fetch_batched(conn, query, batch_size=5000):
        for row in batch:
            if row["id"] in existing_message_ids:
                continue
            subscriber_id = customer_mapping.get(row.get("customer_id"))
            status = status_map.get(row.get("status"), CommunicationStatus.sent)

            log = CommunicationLog(
                subscriber_id=subscriber_id,
                channel=CommunicationChannel.email,
                direction=CommunicationDirection.outbound,
                recipient=(row.get("recipient") or "")[:255] or None,
                subject=(row.get("subject") or "")[:500] or None,
                status=status,
                sent_at=_parse_date(row.get("datetime_sent")),
                splynx_message_id=row["id"],
            )
            log.created_at = _parse_date(row.get("datetime_added")) or datetime.now(UTC)
            db.add(log)
            existing_message_ids.add(row["id"])
            created += 1

        db.flush()
        if created % 50000 == 0 and created > 0:
            logger.info("Emails: %d created so far", created)

    logger.info("Emails: %d created", created)


def migrate_sms(conn, db, customer_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate Splynx sms_pool → CommunicationLog."""
    from app.models.communication_log import (
        CommunicationChannel,
        CommunicationDirection,
        CommunicationLog,
        CommunicationStatus,
    )

    existing_message_ids = set(
        db.scalars(
            select(CommunicationLog.splynx_message_id).where(
                CommunicationLog.channel == CommunicationChannel.sms,
                CommunicationLog.splynx_message_id.isnot(None),
            )
        ).all()
    )

    status_map = {
        "sent": CommunicationStatus.sent,
        "new": CommunicationStatus.pending,
        "sending": CommunicationStatus.pending,
        "error": CommunicationStatus.failed,
    }

    query = "SELECT id, customer_id, recipient, message, status, datetime_added, datetime_sent FROM sms_pool ORDER BY id"
    created = 0

    for batch in fetch_batched(conn, query, batch_size=5000):
        for row in batch:
            if row["id"] in existing_message_ids:
                continue
            subscriber_id = customer_mapping.get(row.get("customer_id"))
            status = status_map.get(row.get("status"), CommunicationStatus.sent)

            log = CommunicationLog(
                subscriber_id=subscriber_id,
                channel=CommunicationChannel.sms,
                direction=CommunicationDirection.outbound,
                recipient=(row.get("recipient") or "")[:255] or None,
                body=(row.get("message") or "")[:2000] or None,
                status=status,
                sent_at=_parse_date(row.get("datetime_sent")),
                splynx_message_id=row["id"],
            )
            log.created_at = _parse_date(row.get("datetime_added")) or datetime.now(UTC)
            db.add(log)
            existing_message_ids.add(row["id"])
            created += 1

        db.flush()
        if created % 50000 == 0 and created > 0:
            logger.info("SMS: %d created so far", created)

    logger.info("SMS: %d created", created)


def migrate_mrr_snapshots(conn, db, customer_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate Splynx mrr_statistics → MrrSnapshot."""
    from app.models.mrr_snapshot import MrrSnapshot

    existing_pairs = {
        (str(row[0]), row[1])
        for row in db.execute(select(MrrSnapshot.subscriber_id, MrrSnapshot.snapshot_date)).all()
    }

    query = """
        SELECT customer_id, date, total as mrr
        FROM mrr_statistics
        ORDER BY customer_id, date
    """
    created = 0
    skipped = 0

    for batch in fetch_batched(conn, query, batch_size=5000):
        for row in batch:
            subscriber_id = customer_mapping.get(row.get("customer_id"))
            if not subscriber_id:
                skipped += 1
                continue

            snapshot_date = row.get("date")
            if not snapshot_date:
                skipped += 1
                continue
            key = (str(subscriber_id), snapshot_date)
            if key in existing_pairs:
                skipped += 1
                continue

            mrr = Decimal(str(row.get("mrr") or "0"))

            snapshot = MrrSnapshot(
                subscriber_id=subscriber_id,
                snapshot_date=snapshot_date,
                mrr_amount=mrr,
                currency="NGN",
                splynx_customer_id=row.get("customer_id"),
            )
            db.add(snapshot)
            existing_pairs.add(key)
            created += 1

        db.flush()
        if created % 100000 == 0 and created > 0:
            logger.info("MRR snapshots: %d created so far (%d skipped)", created, skipped)

    logger.info("MRR snapshots: %d created, %d skipped", created, skipped)


def migrate_credit_note_applications(
    conn, db,
    invoice_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx credit_notes_to_invoices → CreditNoteApplication."""
    from app.models.billing import CreditNote, CreditNoteApplication
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    cn_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.credit_note
            )
        ).all()
    }

    cn_totals = {
        cn.id: cn.total for cn in db.scalars(select(CreditNote)).all()
    }
    existing_pairs = {
        (str(app.credit_note_id), str(app.invoice_id))
        for app in db.scalars(select(CreditNoteApplication)).all()
    }
    application_counts: dict[int, int] = {}
    for row in fetch_all(conn, "SELECT * FROM credit_notes_to_invoices ORDER BY credit_note_id"):
        credit_note_id = row.get("credit_note_id")
        if credit_note_id is not None:
            application_counts[credit_note_id] = application_counts.get(credit_note_id, 0) + 1

    rows = fetch_all(conn, "SELECT * FROM credit_notes_to_invoices ORDER BY credit_note_id")
    created = 0
    skipped = 0

    for row in rows:
        splynx_credit_note_id = row.get("credit_note_id")
        cn_id = cn_map.get(splynx_credit_note_id)
        inv_id = invoice_mapping.get(row.get("invoice_id"))
        if not cn_id or not inv_id:
            skipped += 1
            continue
        key = (str(cn_id), str(inv_id))
        if key in existing_pairs:
            skipped += 1
            continue

        amount = _resolve_credit_note_application_amount(
            row,
            credit_note_total=cn_totals.get(cn_id, Decimal("0")),
            application_count=application_counts.get(splynx_credit_note_id, 0),
        )
        if amount is None:
            logger.warning(
                "Skipping credit note application for Splynx credit_note_id=%s invoice_id=%s: ambiguous amount",
                splynx_credit_note_id,
                row.get("invoice_id"),
            )
            skipped += 1
            continue
        app = CreditNoteApplication(
            credit_note_id=cn_id,
            invoice_id=inv_id,
            amount=amount,
        )
        db.add(app)
        existing_pairs.add(key)
        created += 1

    db.flush()
    logger.info("Credit note applications: %d created, %d skipped", created, skipped)


def run_phase3(dry_run: bool = True) -> None:
    """Execute Phase 3 migration."""
    logger.info("=== Phase 3: Operational Data Migration ===")

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                logger.info("DRY RUN — counting source data only")
                tables = [
                    ("tickets", "SELECT COUNT(*) as cnt FROM ticket WHERE deleted='0'"),
                    ("ticket_messages", "SELECT COUNT(*) as cnt FROM ticket_messages WHERE deleted='0'"),
                    ("mail_pool", "SELECT COUNT(*) as cnt FROM mail_pool"),
                    ("sms_pool", "SELECT COUNT(*) as cnt FROM sms_pool"),
                    ("mrr_statistics", "SELECT COUNT(*) as cnt FROM mrr_statistics"),
                    ("credit_notes_to_invoices", "SELECT COUNT(*) as cnt FROM credit_notes_to_invoices"),
                ]
                for name, query in tables:
                    rows = fetch_all(conn, query)
                    logger.info("  %s: %d rows", name, rows[0]["cnt"])
                logger.info("Run with --execute to migrate")
                return

            # Load mappings
            from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

            customer_mapping = {
                m.splynx_id: m.dotmac_id
                for m in db.scalars(
                    select(SplynxIdMapping).where(
                        SplynxIdMapping.entity_type == SplynxEntityType.customer
                    )
                ).all()
            }
            invoice_mapping = {
                m.splynx_id: m.dotmac_id
                for m in db.scalars(
                    select(SplynxIdMapping).where(
                        SplynxIdMapping.entity_type == SplynxEntityType.invoice
                    )
                ).all()
            }
            logger.info(
                "Loaded %d customer mappings, %d invoice mappings",
                len(customer_mapping),
                len(invoice_mapping),
            )

            # Step 1: Tickets
            migrate_tickets(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Tickets committed ---")

            # Step 2: Emails
            migrate_emails(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Emails committed ---")

            # Step 3: SMS
            migrate_sms(conn, db, customer_mapping)
            db.commit()
            logger.info("--- SMS committed ---")

            # Step 4: MRR snapshots
            migrate_mrr_snapshots(conn, db, customer_mapping)
            db.commit()
            logger.info("--- MRR snapshots committed ---")

            # Step 5: Credit note applications
            migrate_credit_note_applications(conn, db, invoice_mapping)
            db.commit()
            logger.info("--- Credit note applications committed ---")

            logger.info("=== Phase 3 complete ===")


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_phase3(dry_run=False)
    else:
        run_phase3(dry_run=True)
        print(
            "\nTo execute: poetry run python -m scripts.migration.phase3_operational_data --execute"
        )
