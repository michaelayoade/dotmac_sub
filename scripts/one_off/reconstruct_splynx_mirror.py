"""Load the authoritative Splynx cutoff ledger into an ephemeral audit database.

This is not a production importer. It exists only for adjudicating billing
alignment findings from two retained, isolated backups when the Sub backup's
``splynx_billing_transactions`` table is empty.

Safety invariants:

* the target database name must end in ``_audit``;
* ``BILLING_AUDIT_EPHEMERAL=1`` and ``--execute`` are both required;
* the target mirror must be empty;
* the source row count and maximum transaction date must match explicit CLI
  fingerprints;
* transaction-bearing source ledgers must reconcile exactly to source deposits;
* every source ``customer_billing.deposit`` is copied as the authoritative
  cutoff/final position, including customers whose retained transaction history
  is empty;
* final service mode copies only non-identifying schedule facts and the last
  source service charge/paid-through period;
* either the whole mirror is committed, or the transaction is rolled back.

Sub's current ``subscribers.deposit`` is deliberately *not* a load gate.  The
purpose of this audit is to detect whether post-cutover scripts changed the
customer financial state incorrectly.  Requiring the source cutoff net to equal
that mutable current field would let the state under audit validate itself.  A
deposit difference is therefore reported as an audit observation and the
source-faithful cutoff ledger is still committed to the ephemeral database.

Only financial mirror fields are copied. No customer identity, credentials, or
delivery coordinates are selected or emitted.
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pymysql
import pymysql.cursors

BATCH_SIZE = 2000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("cutoff", "final", "services"),
        default="cutoff",
        help=(
            "load the opening cutoff, final legacy balance snapshot, or final "
            "service paid-through schedule"
        ),
    )
    parser.add_argument("--expected-source-rows", type=int, required=True)
    parser.add_argument("--expected-source-customers", type=int, required=True)
    parser.add_argument("--max-source-date", type=date.fromisoformat, required=True)
    parser.add_argument("--expected-source-services", type=int)
    return parser


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).replace("\x00", "") or None


def _integer(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result or None


def _entry_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"credit", "debit"} else "other"


def _deleted(value: Any) -> bool:
    return str(value) in {"1", "True", "true"}


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = str(value)
    if not normalized or normalized.startswith("0000"):
        return None
    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        return None


def _source_connection() -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ["SPLYNX_MYSQL_HOST"],
        port=int(os.environ.get("SPLYNX_MYSQL_PORT", "3306")),
        user=os.environ.get("SPLYNX_MYSQL_USER", "root"),
        password=os.environ.get("SPLYNX_MYSQL_PASS", ""),
        database=os.environ.get("SPLYNX_MYSQL_DB", "splynx"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.SSDictCursor,
        connect_timeout=20,
        read_timeout=600,
    )


def _assert_source(
    source: pymysql.Connection,
    *,
    expected_rows: int,
    expected_customers: int,
    max_date: date,
) -> None:
    with source.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n, MAX(date) AS max_date FROM billing_transactions"
        )
        fingerprint = cursor.fetchone()
        if not fingerprint:
            raise RuntimeError("source fingerprint query returned no row")
        cursor.fetchall()
        actual_rows = int(fingerprint["n"])
        actual_max = fingerprint["max_date"]
        if actual_rows != expected_rows or actual_max != max_date:
            raise RuntimeError(
                "source fingerprint mismatch: "
                f"rows={actual_rows}, max_date={actual_max}"
            )

        cursor.execute(
            """
            SELECT COUNT(*) AS customers,
                   SUM(ABS(x.net - COALESCE(cb.deposit, 0)) > 0.01) AS mismatches
            FROM (
                SELECT customer_id,
                       ROUND(SUM(CASE
                           WHEN type = 'credit' THEN total
                           WHEN type = 'debit' THEN -total
                           ELSE 0
                       END), 2) AS net
                FROM billing_transactions
                WHERE deleted = '0'
                GROUP BY customer_id
            ) x
            JOIN customer_billing cb ON cb.customer_id = x.customer_id
            """
        )
        parity = cursor.fetchone()
        cursor.fetchall()
        if not parity or int(parity["mismatches"] or 0) != 0:
            mismatch_count = int(parity["mismatches"] or 0) if parity else -1
            raise RuntimeError(
                f"source transaction/deposit parity failed: {mismatch_count} mismatches"
            )
        print(
            "source fingerprint and parity: "
            f"{actual_rows} rows, {int(parity['customers'])} customers, 0 mismatches"
        )

        cursor.execute(
            """
            SELECT COUNT(*) AS customers,
                   SUM(x.customer_id IS NULL AND ABS(cb.deposit) > 0.01)
                       AS deposit_only_customers,
                   ROUND(SUM(CASE WHEN x.customer_id IS NULL
                                  THEN ABS(cb.deposit) ELSE 0 END), 2)
                       AS deposit_only_absolute_total
            FROM customer_billing cb
            LEFT JOIN (
                SELECT DISTINCT customer_id
                FROM billing_transactions
                WHERE deleted = '0'
            ) x ON x.customer_id = cb.customer_id
            """
        )
        coverage = cursor.fetchone()
        cursor.fetchall()
        if coverage is None:
            raise RuntimeError("source cutoff-deposit coverage query returned no row")
        actual_customers = int(coverage["customers"] or 0)
        if actual_customers != expected_customers:
            raise RuntimeError(
                "source customer fingerprint mismatch: "
                f"{actual_customers} != {expected_customers}"
            )
        print(
            "source cutoff deposits: "
            f"{actual_customers} customers, "
            f"{int(coverage['deposit_only_customers'] or 0)} non-zero deposits "
            "without retained transaction rows, absolute total "
            f"{Decimal(str(coverage['deposit_only_absolute_total'] or 0)):.2f}"
        )


def _target_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("PGHOST", "db"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "postgres"),
        dbname=os.environ.get("PGDATABASE", "dotmac_sub_audit"),
    )


def _assert_audit_database(target: psycopg.Connection) -> None:
    with target.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_row = cursor.fetchone()
        if database_row is None:
            raise RuntimeError("target database identity query returned no row")
        database = str(database_row[0])
        if not database.endswith("_audit"):
            raise RuntimeError(f"refusing non-audit target database: {database}")


def _subscriber_mapping(target: psycopg.Connection) -> dict[int, uuid.UUID]:
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT splynx_customer_id, id
            FROM subscribers
            WHERE splynx_customer_id IS NOT NULL
            """
        )
        return {
            int(customer_id): subscriber_id for customer_id, subscriber_id in cursor
        }


def _subscription_mapping(target: psycopg.Connection) -> dict[int, uuid.UUID]:
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT splynx_service_id, id
            FROM subscriptions
            WHERE splynx_service_id IS NOT NULL
            """
        )
        return {
            int(service_id): subscription_id for service_id, subscription_id in cursor
        }


def _assert_cutoff_target(target: psycopg.Connection) -> dict[int, uuid.UUID]:
    _assert_audit_database(target)
    with target.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM splynx_billing_transactions")
        count_row = cursor.fetchone()
        if count_row is None:
            raise RuntimeError("target mirror count query returned no row")
        mirror_rows = int(count_row[0])
        if mirror_rows:
            raise RuntimeError(f"target mirror is not empty: {mirror_rows} rows")
        cursor.execute("SELECT to_regclass('public.audit_splynx_cutoff_balances')")
        audit_table = cursor.fetchone()
        if audit_table and audit_table[0] is not None:
            raise RuntimeError("target cutoff-balance audit table already exists")
        cursor.execute(
            """
            CREATE TABLE audit_splynx_cutoff_balances (
                splynx_customer_id bigint PRIMARY KEY,
                subscriber_id uuid NULL,
                cutoff_deposit numeric(19, 4) NOT NULL,
                active_transaction_net numeric(19, 4) NULL,
                active_transaction_rows integer NOT NULL,
                transaction_reconciled boolean NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX ix_audit_splynx_cutoff_balances_subscriber "
            "ON audit_splynx_cutoff_balances (subscriber_id)"
        )
    return _subscriber_mapping(target)


def _assert_final_target(target: psycopg.Connection) -> dict[int, uuid.UUID]:
    _assert_audit_database(target)
    with target.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.audit_splynx_cutoff_balances')")
        cutoff_table = cursor.fetchone()
        if not cutoff_table or cutoff_table[0] is None:
            raise RuntimeError("final snapshot requires the opening cutoff table")
        cursor.execute("SELECT to_regclass('public.audit_splynx_final_balances')")
        final_table = cursor.fetchone()
        if final_table and final_table[0] is not None:
            raise RuntimeError("target final-balance audit table already exists")
        cursor.execute(
            """
            CREATE TABLE audit_splynx_final_balances (
                splynx_customer_id bigint PRIMARY KEY,
                subscriber_id uuid NULL,
                final_deposit numeric(19, 4) NOT NULL,
                active_transaction_net numeric(19, 4) NULL,
                active_transaction_rows integer NOT NULL,
                transaction_reconciled boolean NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX ix_audit_splynx_final_balances_subscriber "
            "ON audit_splynx_final_balances (subscriber_id)"
        )
    return _subscriber_mapping(target)


def _assert_services_target(target: psycopg.Connection) -> None:
    _assert_audit_database(target)
    with target.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.audit_splynx_final_balances')")
        final_table = cursor.fetchone()
        if not final_table or final_table[0] is None:
            raise RuntimeError("service replay requires the final balance table")
        cursor.execute("SELECT to_regclass('public.audit_splynx_final_services')")
        services_table = cursor.fetchone()
        if services_table and services_table[0] is not None:
            raise RuntimeError("target final-service audit table already exists")
        cursor.execute(
            """
            CREATE TABLE audit_splynx_final_services (
                splynx_service_id bigint PRIMARY KEY,
                splynx_customer_id bigint NOT NULL,
                source_status text NOT NULL,
                source_deleted boolean NOT NULL,
                tariff_id bigint NULL,
                quantity integer NULL,
                unit_price numeric(19, 4) NULL,
                discount boolean NULL,
                discount_value numeric(19, 4) NULL,
                discount_type text NULL,
                start_date date NULL,
                source_updated_at timestamp NULL,
                last_transaction_id bigint NULL,
                last_charge_total numeric(19, 4) NULL,
                last_period_from date NULL,
                last_period_to date NULL,
                subscriber_id uuid NULL,
                subscription_id uuid NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX ix_audit_splynx_final_services_customer "
            "ON audit_splynx_final_services (splynx_customer_id)"
        )


def _assert_source_services(
    source: pymysql.Connection, *, expected_services: int
) -> None:
    with source.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS services,
                   SUM(status = 'active' AND deleted = '0') AS active_services,
                   MAX(updated_at) AS max_updated_at
            FROM services_internet
            """
        )
        row = cursor.fetchone()
        cursor.fetchall()
        if row is None:
            raise RuntimeError("source service fingerprint returned no row")
        actual_services = int(row["services"] or 0)
        if actual_services != expected_services:
            raise RuntimeError(
                f"source service fingerprint mismatch: {actual_services} "
                f"!= {expected_services}"
            )
        print(
            "source service fingerprint: "
            f"{actual_services} rows, {int(row['active_services'] or 0)} "
            f"active/non-deleted, max_updated_at={row['max_updated_at']}"
        )


def _categories(source: pymysql.Connection) -> dict[int, str]:
    with source.cursor() as cursor:
        cursor.execute("SELECT id, name FROM billing_transactions_categories")
        return {int(row["id"]): str(row["name"]) for row in cursor.fetchall()}


def _copy_mirror(
    source: pymysql.Connection,
    target: psycopg.Connection,
    subscribers: dict[int, uuid.UUID],
) -> tuple[int, int]:
    categories = _categories(source)
    scanned = 0
    unlinked = 0
    now = datetime.now(UTC)
    query = """
        SELECT id, customer_id, type, total, category, description, date,
               period_from, period_to, invoice_id, payment_id, credit_note_id,
               service_id, service_type, source, deleted
        FROM billing_transactions
        ORDER BY id
    """
    copy_sql = """
        COPY splynx_billing_transactions (
            id, splynx_transaction_id, splynx_customer_id, subscriber_id,
            entry_type, amount, category_id, category_name, description,
            transaction_date, period_from, period_to, splynx_invoice_id,
            splynx_payment_id, splynx_credit_note_id, service_id, service_type,
            source, deleted, created_at, updated_at
        ) FROM STDIN
    """
    with source.cursor() as source_cursor, target.cursor() as target_cursor:
        source_cursor.execute(query)
        with target_cursor.copy(copy_sql) as copy:
            while rows := source_cursor.fetchmany(BATCH_SIZE):
                for row in rows:
                    customer_id = int(row["customer_id"])
                    subscriber_id = subscribers.get(customer_id)
                    if subscriber_id is None:
                        unlinked += 1
                    category_id = _integer(row["category"])
                    copy.write_row(
                        (
                            uuid.uuid4(),
                            int(row["id"]),
                            customer_id,
                            subscriber_id,
                            _entry_type(row["type"]),
                            Decimal(str(row["total"] or 0)),
                            category_id,
                            categories.get(category_id) if category_id else None,
                            _text(row["description"]),
                            _date(row["date"]),
                            _date(row["period_from"]),
                            _date(row["period_to"]),
                            _integer(row["invoice_id"]),
                            _integer(row["payment_id"]),
                            _integer(row["credit_note_id"]),
                            _integer(row["service_id"]),
                            _text(row["service_type"]),
                            _text(row["source"]),
                            _deleted(row["deleted"]),
                            now,
                            now,
                        )
                    )
                    scanned += 1
    return scanned, unlinked


def _copy_source_balances(
    source: pymysql.Connection,
    target: psycopg.Connection,
    subscribers: dict[int, uuid.UUID],
    *,
    target_table: str,
    deposit_column: str,
) -> tuple[int, int]:
    allowed_targets = {
        ("audit_splynx_cutoff_balances", "cutoff_deposit"),
        ("audit_splynx_final_balances", "final_deposit"),
    }
    if (target_table, deposit_column) not in allowed_targets:
        raise RuntimeError("refusing an unknown source-balance target")
    scanned = 0
    unlinked = 0
    query = """
        SELECT cb.customer_id, cb.deposit,
               COALESCE(x.transaction_rows, 0) AS transaction_rows,
               x.net AS transaction_net
        FROM customer_billing cb
        LEFT JOIN (
            SELECT customer_id, COUNT(*) AS transaction_rows,
                   ROUND(SUM(CASE
                       WHEN type = 'credit' THEN total
                       WHEN type = 'debit' THEN -total
                       ELSE 0
                   END), 2) AS net
            FROM billing_transactions
            WHERE deleted = '0'
            GROUP BY customer_id
        ) x ON x.customer_id = cb.customer_id
        ORDER BY cb.customer_id
    """
    copy_sql = f"""
        COPY {target_table} (
            splynx_customer_id, subscriber_id, {deposit_column},
            active_transaction_net, active_transaction_rows,
            transaction_reconciled
        ) FROM STDIN
    """
    with source.cursor() as source_cursor, target.cursor() as target_cursor:
        source_cursor.execute(query)
        with target_cursor.copy(copy_sql) as copy:
            while rows := source_cursor.fetchmany(BATCH_SIZE):
                for row in rows:
                    customer_id = int(row["customer_id"])
                    subscriber_id = subscribers.get(customer_id)
                    if subscriber_id is None:
                        unlinked += 1
                    deposit = Decimal(str(row["deposit"] or 0))
                    transaction_rows = int(row["transaction_rows"] or 0)
                    transaction_net = (
                        Decimal(str(row["transaction_net"]))
                        if row["transaction_net"] is not None
                        else None
                    )
                    reconciled = bool(
                        transaction_rows
                        and transaction_net is not None
                        and abs(transaction_net - deposit) <= Decimal("0.01")
                    )
                    copy.write_row(
                        (
                            customer_id,
                            subscriber_id,
                            deposit,
                            transaction_net,
                            transaction_rows,
                            reconciled,
                        )
                    )
                    scanned += 1
    return scanned, unlinked


def _copy_final_services(
    source: pymysql.Connection,
    target: psycopg.Connection,
    subscribers: dict[int, uuid.UUID],
    subscriptions: dict[int, uuid.UUID],
) -> tuple[int, int, int]:
    scanned = 0
    unlinked_subscribers = 0
    unlinked_subscriptions = 0
    query = """
        WITH ranked AS (
            SELECT id, customer_id, service_id, total, period_from, period_to,
                   ROW_NUMBER() OVER (
                       PARTITION BY service_id
                       ORDER BY period_to DESC, id DESC
                   ) AS rn
            FROM billing_transactions
            WHERE deleted = '0'
              AND type = 'debit'
              AND category = 1
              AND service_id IS NOT NULL
        )
        SELECT si.id, si.customer_id, si.status, si.deleted, si.tariff_id,
               si.quantity, si.unit_price, si.discount, si.discount_value,
               si.discount_type, si.start_date, si.updated_at,
               ranked.id AS last_transaction_id,
               ranked.total AS last_charge_total,
               ranked.period_from AS last_period_from,
               ranked.period_to AS last_period_to
        FROM services_internet si
        LEFT JOIN ranked ON ranked.service_id = si.id AND ranked.rn = 1
        ORDER BY si.id
    """
    copy_sql = """
        COPY audit_splynx_final_services (
            splynx_service_id, splynx_customer_id, source_status,
            source_deleted, tariff_id, quantity, unit_price, discount,
            discount_value, discount_type, start_date, source_updated_at,
            last_transaction_id, last_charge_total, last_period_from,
            last_period_to, subscriber_id, subscription_id
        ) FROM STDIN
    """
    with source.cursor() as source_cursor, target.cursor() as target_cursor:
        source_cursor.execute(query)
        with target_cursor.copy(copy_sql) as copy:
            while rows := source_cursor.fetchmany(BATCH_SIZE):
                for row in rows:
                    service_id = int(row["id"])
                    customer_id = int(row["customer_id"])
                    subscriber_id = subscribers.get(customer_id)
                    subscription_id = subscriptions.get(service_id)
                    if subscriber_id is None:
                        unlinked_subscribers += 1
                    if subscription_id is None:
                        unlinked_subscriptions += 1
                    copy.write_row(
                        (
                            service_id,
                            customer_id,
                            _text(row["status"]) or "unknown",
                            _deleted(row["deleted"]),
                            _integer(row["tariff_id"]),
                            _integer(row["quantity"]),
                            (
                                Decimal(str(row["unit_price"]))
                                if row["unit_price"] is not None
                                else None
                            ),
                            _deleted(row["discount"]),
                            (
                                Decimal(str(row["discount_value"]))
                                if row["discount_value"] is not None
                                else None
                            ),
                            _text(row["discount_type"]),
                            _date(row["start_date"]),
                            row["updated_at"],
                            _integer(row["last_transaction_id"]),
                            (
                                Decimal(str(row["last_charge_total"]))
                                if row["last_charge_total"] is not None
                                else None
                            ),
                            _date(row["last_period_from"]),
                            _date(row["last_period_to"]),
                            subscriber_id,
                            subscription_id,
                        )
                    )
                    scanned += 1
    return scanned, unlinked_subscribers, unlinked_subscriptions


def _assess_cutoff_target(
    target: psycopg.Connection, expected_rows: int, expected_customers: int
) -> tuple[int, int, Decimal]:
    with target.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM splynx_billing_transactions")
        count_row = cursor.fetchone()
        if count_row is None:
            raise RuntimeError("target mirror count query returned no row")
        actual_rows = int(count_row[0])
        if actual_rows != expected_rows:
            raise RuntimeError(
                f"target mirror count mismatch: {actual_rows} != {expected_rows}"
            )
        cursor.execute("SELECT COUNT(*) FROM audit_splynx_cutoff_balances")
        customer_count_row = cursor.fetchone()
        if customer_count_row is None:
            raise RuntimeError("target cutoff-balance count query returned no row")
        actual_customers = int(customer_count_row[0])
        if actual_customers != expected_customers:
            raise RuntimeError(
                "target cutoff-balance count mismatch: "
                f"{actual_customers} != {expected_customers}"
            )
        cursor.execute(
            """
            SELECT COUNT(*) AS accounts,
                   COUNT(*) FILTER (
                       WHERE ABS(cutoff.cutoff_deposit
                                 - COALESCE(subscribers.deposit, 0)) > 0.01
                   ) AS differences,
                   COALESCE(SUM(
                       ABS(cutoff.cutoff_deposit
                           - COALESCE(subscribers.deposit, 0))
                   ) FILTER (
                       WHERE ABS(cutoff.cutoff_deposit
                                 - COALESCE(subscribers.deposit, 0)) > 0.01
                   ), 0) AS absolute_difference
            FROM audit_splynx_cutoff_balances cutoff
            JOIN subscribers ON subscribers.id = cutoff.subscriber_id
            """
        )
        assessment = cursor.fetchone()
        if assessment is None:
            raise RuntimeError("target assessment query returned no row")
        accounts, differences, absolute_difference = assessment
        return (
            int(accounts),
            int(differences),
            Decimal(str(absolute_difference or 0)),
        )


def _assess_final_target(
    target: psycopg.Connection, expected_customers: int
) -> tuple[int, int, Decimal, Decimal, int, Decimal]:
    with target.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM audit_splynx_final_balances")
        count_row = cursor.fetchone()
        if count_row is None:
            raise RuntimeError("target final-balance count query returned no row")
        actual_customers = int(count_row[0])
        if actual_customers != expected_customers:
            raise RuntimeError(
                "target final-balance count mismatch: "
                f"{actual_customers} != {expected_customers}"
            )
        cursor.execute(
            """
            SELECT COUNT(*) AS joined_accounts,
                   COUNT(*) FILTER (
                       WHERE ABS(final.final_deposit
                                 - cutoff.cutoff_deposit) > 0.01
                   ) AS changed_accounts,
                   COALESCE(SUM(
                       final.final_deposit - cutoff.cutoff_deposit
                   ) FILTER (
                       WHERE ABS(final.final_deposit
                                 - cutoff.cutoff_deposit) > 0.01
                   ), 0) AS signed_change,
                   COALESCE(SUM(
                       ABS(final.final_deposit - cutoff.cutoff_deposit)
                   ) FILTER (
                       WHERE ABS(final.final_deposit
                                 - cutoff.cutoff_deposit) > 0.01
                   ), 0) AS absolute_change
            FROM audit_splynx_final_balances final
            JOIN audit_splynx_cutoff_balances cutoff
              USING (splynx_customer_id)
            """
        )
        overlap = cursor.fetchone()
        if overlap is None:
            raise RuntimeError("source overlap assessment returned no row")
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (
                       WHERE ABS(final.final_deposit
                                 - COALESCE(subscribers.deposit, 0)) > 0.01
                   ) AS differences,
                   COALESCE(SUM(
                       ABS(final.final_deposit
                           - COALESCE(subscribers.deposit, 0))
                   ) FILTER (
                       WHERE ABS(final.final_deposit
                                 - COALESCE(subscribers.deposit, 0)) > 0.01
                   ), 0) AS absolute_difference
            FROM audit_splynx_final_balances final
            JOIN subscribers ON subscribers.id = final.subscriber_id
            """
        )
        current = cursor.fetchone()
        if current is None:
            raise RuntimeError("final/current assessment returned no row")
        return (
            int(overlap[1]),
            int(overlap[0]),
            Decimal(str(overlap[2] or 0)),
            Decimal(str(overlap[3] or 0)),
            int(current[0]),
            Decimal(str(current[1] or 0)),
        )


def main() -> None:
    args = _parser().parse_args()
    if not args.execute or os.environ.get("BILLING_AUDIT_EPHEMERAL") != "1":
        raise RuntimeError(
            "refusing to write: require --execute and BILLING_AUDIT_EPHEMERAL=1"
        )

    source = _source_connection()
    target = _target_connection()
    try:
        _assert_source(
            source,
            expected_rows=args.expected_source_rows,
            expected_customers=args.expected_source_customers,
            max_date=args.max_source_date,
        )
        if args.mode == "cutoff":
            subscribers = _assert_cutoff_target(target)
            scanned, unlinked = _copy_mirror(source, target, subscribers)
            cutoff_customers, unlinked_cutoff_customers = _copy_source_balances(
                source,
                target,
                subscribers,
                target_table="audit_splynx_cutoff_balances",
                deposit_column="cutoff_deposit",
            )
            accounts, deposit_differences, absolute_difference = _assess_cutoff_target(
                target,
                args.expected_source_rows,
                args.expected_source_customers,
            )
            target.commit()
            print(
                "cutoff mirror committed: "
                f"{scanned} rows, {accounts} linked accounts, "
                f"{unlinked} unlinked rows, {cutoff_customers} cutoff balances, "
                f"{unlinked_cutoff_customers} unlinked cutoff balances, "
                f"{deposit_differences} current-deposit differences, "
                f"absolute difference {absolute_difference:.2f}"
            )
        elif args.mode == "final":
            subscribers = _assert_final_target(target)
            final_customers, unlinked_final_customers = _copy_source_balances(
                source,
                target,
                subscribers,
                target_table="audit_splynx_final_balances",
                deposit_column="final_deposit",
            )
            (
                changed_accounts,
                joined_accounts,
                signed_change,
                absolute_change,
                current_differences,
                current_absolute_difference,
            ) = _assess_final_target(target, args.expected_source_customers)
            target.commit()
            print(
                "final legacy snapshot committed: "
                f"{final_customers} balances, {unlinked_final_customers} unlinked, "
                f"{joined_accounts} cutoff/final joins, "
                f"{changed_accounts} source changes, signed {signed_change:.2f}, "
                f"absolute {absolute_change:.2f}; "
                f"{current_differences} current-deposit differences, "
                f"absolute {current_absolute_difference:.2f}"
            )
        else:
            if args.expected_source_services is None:
                raise RuntimeError("services mode requires --expected-source-services")
            _assert_source_services(
                source,
                expected_services=args.expected_source_services,
            )
            _assert_services_target(target)
            service_rows, unlinked_subscribers, unlinked_subscriptions = (
                _copy_final_services(
                    source,
                    target,
                    _subscriber_mapping(target),
                    _subscription_mapping(target),
                )
            )
            if service_rows != args.expected_source_services:
                raise RuntimeError(
                    "target final-service count mismatch: "
                    f"{service_rows} != {args.expected_source_services}"
                )
            target.commit()
            print(
                "final service schedule committed: "
                f"{service_rows} services, "
                f"{unlinked_subscribers} unlinked subscribers, "
                f"{unlinked_subscriptions} unlinked subscriptions"
            )
    except BaseException:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
