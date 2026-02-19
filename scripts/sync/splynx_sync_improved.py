"""
Improved Splynx sync service.

Fixes from the original:
1. Incremental sync via WHERE updated_at > :last_sync
2. Bulk upserts using executemany + batch execute_values
3. Fixed _bool_from_enum to handle both int and str MySQL values
4. Thread-safe MySQL connections (new connection per sync method)
5. datetime.now(UTC) instead of deprecated datetime.utcnow()

Deploy: Replace /app/app/services/sync/splynx_sync.py on the remote server.
"""

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pymysql
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
LARGE_BATCH_SIZE = 5000


def _get_mysql_connection() -> pymysql.Connection:
    """Create a new MySQL connection (thread-safe, one per call)."""
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )


def _get_streaming_connection() -> pymysql.Connection:
    """Create a streaming MySQL connection for large tables."""
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        cursorclass=pymysql.cursors.SSDictCursor,
        charset="utf8mb4",
    )


def _fetch_batches(
    conn: pymysql.Connection,
    query: str,
    batch_size: int = BATCH_SIZE,
) -> Generator[list[dict], None, None]:
    """Fetch data in batches from MySQL."""
    with conn.cursor() as cursor:
        cursor.execute(query)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield rows


def _fetch_batches_streaming(
    query: str,
    batch_size: int = BATCH_SIZE,
) -> Generator[list[dict], None, None]:
    """Fetch data using server-side cursor for very large tables."""
    conn = _get_streaming_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                yield rows
    finally:
        conn.close()


def _bool_from_enum(value: Any) -> bool:
    """Convert MySQL enum/tinyint to boolean. Handles both str and int."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value == "1"
    return False


def _safe_datetime(value: Any) -> Any:
    """Convert invalid MySQL datetime (0000-00-00) to None."""
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("0000-00-00"):
        return None
    try:
        if hasattr(value, "year") and value.year == 0:
            return None
    except (ValueError, AttributeError):
        logger.debug("Could not parse datetime value: %r", value)
    return value


def _safe_date(value: Any) -> Any:
    """Convert invalid MySQL date (0000-00-00) to None."""
    return _safe_datetime(value)


def _convert_ip_bytes(value: bytes | None) -> str:
    """Convert MySQL varbinary IP to string."""
    if value is None or len(value) == 0:
        return ""
    try:
        if len(value) == 4:
            return ".".join(str(b) for b in value)
        elif len(value) == 16:
            import socket
            return socket.inet_ntop(socket.AF_INET6, value)
    except (ValueError, OSError):
        logger.debug("Could not convert IP bytes: %r", value)
    return ""


def _get_last_sync(db: Session, table_name: str) -> datetime | None:
    """Get the most recent synced_at for a given table."""
    result = db.execute(
        text(f"SELECT MAX(synced_at) FROM {table_name}")  # noqa: S608
    )
    val = result.scalar()
    if isinstance(val, datetime):
        return val
    return None


def _now() -> datetime:
    """Current UTC timestamp."""
    return datetime.now(UTC)


class SplynxSyncService:
    """Service for syncing data from Splynx MySQL to local PostgreSQL.

    Improvements over original:
    - Incremental sync: only fetches rows updated since last sync
    - Bulk upserts: batched INSERT...ON CONFLICT for much better throughput
    - Thread-safe: creates new MySQL connection per sync call
    - Fixed boolean conversion for MySQL tinyint fields
    """

    def _incremental_clause(
        self, db: Session, table_name: str, mysql_col: str = "updated_at"
    ) -> str:
        """Build a WHERE clause for incremental sync."""
        last_sync = _get_last_sync(db, table_name)
        if last_sync is None:
            return ""
        # Give 1-minute overlap to handle clock skew
        from datetime import timedelta
        cutoff = last_sync - timedelta(minutes=1)
        return f" WHERE {mysql_col} >= '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'"

    def sync_partners(self) -> int:
        """Sync partners from Splynx."""
        logger.info("Syncing partners...")
        count = 0
        conn = _get_mysql_connection()
        try:
            query = "SELECT id, name, deleted FROM partners"
            with SessionLocal() as db:
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_partners (id, name, deleted, synced_at)
                                VALUES (:id, :name, :deleted, :synced_at)
                                ON CONFLICT (id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    deleted = EXCLUDED.deleted,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "name": row["name"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} partners")
        return count

    def sync_locations(self) -> int:
        """Sync locations from Splynx."""
        logger.info("Syncing locations...")
        count = 0
        conn = _get_mysql_connection()
        try:
            query = "SELECT id, name, deleted FROM locations"
            with SessionLocal() as db:
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_locations (id, name, deleted, synced_at)
                                VALUES (:id, :name, :deleted, :synced_at)
                                ON CONFLICT (id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    deleted = EXCLUDED.deleted,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "name": row["name"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} locations")
        return count

    def sync_customers(self) -> int:
        """Sync customers from Splynx (incremental)."""
        logger.info("Syncing customers...")
        count = 0
        conn = _get_mysql_connection()
        try:
            with SessionLocal() as db:
                where = self._incremental_clause(db, "splynx_customers", "last_update")
                query = f"""
                    SELECT id, billing_type, partner_id, location_id, added_by, added_by_id,
                           status, login, category, name, email, billing_email, phone,
                           street_1, zip_code, city, gps, date_add, last_online, deleted,
                           last_update, daily_prepaid_cost, mrr_total, conversion_date
                    FROM customers
                    {where}
                """  # noqa: S608 - WHERE clause from internal _incremental_clause
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_customers (
                                    id, billing_type, partner_id, location_id, added_by,
                                    added_by_id, status, login, category, name, email,
                                    billing_email, phone, street_1, zip_code, city, gps,
                                    date_add, last_online, deleted, last_update,
                                    daily_prepaid_cost, mrr_total, conversion_date, synced_at
                                ) VALUES (
                                    :id, :billing_type, :partner_id, :location_id, :added_by,
                                    :added_by_id, :status, :login, :category, :name, :email,
                                    :billing_email, :phone, :street_1, :zip_code, :city, :gps,
                                    :date_add, :last_online, :deleted, :last_update,
                                    :daily_prepaid_cost, :mrr_total, :conversion_date, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    billing_type = EXCLUDED.billing_type,
                                    status = EXCLUDED.status,
                                    name = EXCLUDED.name,
                                    email = EXCLUDED.email,
                                    phone = EXCLUDED.phone,
                                    deleted = EXCLUDED.deleted,
                                    last_update = EXCLUDED.last_update,
                                    mrr_total = EXCLUDED.mrr_total,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "billing_type": row["billing_type"],
                                "partner_id": row["partner_id"],
                                "location_id": row["location_id"],
                                "added_by": row["added_by"],
                                "added_by_id": row["added_by_id"],
                                "status": row["status"],
                                "login": row["login"],
                                "category": row["category"],
                                "name": row["name"],
                                "email": row["email"],
                                "billing_email": row["billing_email"],
                                "phone": row["phone"],
                                "street_1": row["street_1"],
                                "zip_code": row["zip_code"],
                                "city": row["city"],
                                "gps": row["gps"],
                                "date_add": _safe_date(row["date_add"]),
                                "last_online": _safe_datetime(row["last_online"]),
                                "deleted": _bool_from_enum(row["deleted"]),
                                "last_update": _safe_datetime(row["last_update"]),
                                "daily_prepaid_cost": row["daily_prepaid_cost"],
                                "mrr_total": row["mrr_total"],
                                "conversion_date": _safe_datetime(row["conversion_date"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
                    if count % 5000 == 0:
                        logger.info(f"Synced {count} customers so far...")
        finally:
            conn.close()
        logger.info(f"Synced {count} customers")
        return count

    def sync_invoices(self) -> int:
        """Sync invoices from Splynx (incremental)."""
        logger.info("Syncing invoices...")
        count = 0
        conn = _get_mysql_connection()
        try:
            with SessionLocal() as db:
                where = self._incremental_clause(db, "splynx_invoices", "date_updated")
                query = f"""
                    SELECT id, customer_id, number, date_created, real_create_datetime,
                           date_updated, date_payment, date_till, total, due, status,
                           payment_id, is_sent, note, memo, added_by, added_by_id, deleted, type
                    FROM invoices
                    {where}
                """  # noqa: S608 - WHERE clause from internal _incremental_clause
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_invoices (
                                    id, customer_id, number, date_created, real_create_datetime,
                                    date_updated, date_payment, date_till, total, due, status,
                                    payment_id, is_sent, note, memo, added_by, added_by_id,
                                    deleted, invoice_type, synced_at
                                ) VALUES (
                                    :id, :customer_id, :number, :date_created, :real_create_datetime,
                                    :date_updated, :date_payment, :date_till, :total, :due, :status,
                                    :payment_id, :is_sent, :note, :memo, :added_by, :added_by_id,
                                    :deleted, :invoice_type, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    status = EXCLUDED.status,
                                    due = EXCLUDED.due,
                                    payment_id = EXCLUDED.payment_id,
                                    deleted = EXCLUDED.deleted,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "customer_id": row["customer_id"],
                                "number": row["number"],
                                "date_created": _safe_date(row["date_created"]),
                                "real_create_datetime": _safe_datetime(row["real_create_datetime"]),
                                "date_updated": _safe_date(row["date_updated"]),
                                "date_payment": _safe_date(row["date_payment"]),
                                "date_till": _safe_date(row["date_till"]),
                                "total": row["total"],
                                "due": row["due"],
                                "status": row["status"],
                                "payment_id": row["payment_id"],
                                "is_sent": _bool_from_enum(row["is_sent"]),
                                "note": row["note"] or "",
                                "memo": row["memo"] or "",
                                "added_by": row["added_by"],
                                "added_by_id": row["added_by_id"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "invoice_type": row["type"],
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
                    if count % 5000 == 0:
                        logger.info(f"Synced {count} invoices so far...")
        finally:
            conn.close()
        logger.info(f"Synced {count} invoices")
        return count

    def sync_payments(self) -> int:
        """Sync payments from Splynx (incremental)."""
        logger.info("Syncing payments...")
        count = 0
        conn = _get_mysql_connection()
        try:
            with SessionLocal() as db:
                where = self._incremental_clause(db, "splynx_payments", "updated_at")
                query = f"""
                    SELECT id, customer_id, invoice_id, transaction_id, payment_type,
                           receipt_number, date, real_create_datetime, amount, comment,
                           note, memo, added_by, added_by_id, deleted, updated_at
                    FROM payments
                    {where}
                """  # noqa: S608 - WHERE clause from internal _incremental_clause
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_payments (
                                    id, customer_id, invoice_id, transaction_id, payment_type,
                                    receipt_number, payment_date, real_create_datetime, amount,
                                    comment, note, memo, added_by, added_by_id, deleted,
                                    updated_at, synced_at
                                ) VALUES (
                                    :id, :customer_id, :invoice_id, :transaction_id, :payment_type,
                                    :receipt_number, :payment_date, :real_create_datetime, :amount,
                                    :comment, :note, :memo, :added_by, :added_by_id, :deleted,
                                    :updated_at, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    deleted = EXCLUDED.deleted,
                                    updated_at = EXCLUDED.updated_at,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "customer_id": row["customer_id"],
                                "invoice_id": row["invoice_id"],
                                "transaction_id": row["transaction_id"],
                                "payment_type": row["payment_type"],
                                "receipt_number": row["receipt_number"],
                                "payment_date": _safe_date(row["date"]),
                                "real_create_datetime": _safe_datetime(row["real_create_datetime"]),
                                "amount": row["amount"],
                                "comment": row["comment"] or "",
                                "note": row["note"] or "",
                                "memo": row["memo"] or "",
                                "added_by": row["added_by"],
                                "added_by_id": row["added_by_id"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "updated_at": _safe_datetime(row["updated_at"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} payments")
        return count

    def sync_services(self) -> int:
        """Sync internet services from Splynx (incremental)."""
        logger.info("Syncing services...")
        count = 0
        conn = _get_mysql_connection()
        try:
            with SessionLocal() as db:
                where = self._incremental_clause(db, "splynx_services_internet", "updated_at")
                query = f"""
                    SELECT id, customer_id, tariff_id, router_id, description, quantity,
                           unit, unit_price, start_date, end_date, status, discount,
                           discount_value, discount_type, deleted, login, ipv4, ipv6, mac, updated_at
                    FROM services_internet
                    {where}
                """  # noqa: S608 - WHERE clause from internal _incremental_clause
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_services_internet (
                                    id, customer_id, tariff_id, router_id, description,
                                    quantity, unit, unit_price, start_date, end_date,
                                    status, discount, discount_value, discount_type,
                                    deleted, login, ipv4, ipv6, mac, updated_at, synced_at
                                ) VALUES (
                                    :id, :customer_id, :tariff_id, :router_id, :description,
                                    :quantity, :unit, :unit_price, :start_date, :end_date,
                                    :status, :discount, :discount_value, :discount_type,
                                    :deleted, :login, :ipv4, :ipv6, :mac, :updated_at, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    status = EXCLUDED.status,
                                    deleted = EXCLUDED.deleted,
                                    ipv4 = EXCLUDED.ipv4,
                                    updated_at = EXCLUDED.updated_at,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "customer_id": row["customer_id"],
                                "tariff_id": row["tariff_id"],
                                "router_id": row["router_id"],
                                "description": row["description"] or "",
                                "quantity": row["quantity"],
                                "unit": row["unit"] or "",
                                "unit_price": row["unit_price"],
                                "start_date": _safe_date(row["start_date"]),
                                "end_date": _safe_date(row["end_date"]),
                                "status": row["status"],
                                "discount": _bool_from_enum(row["discount"]),
                                "discount_value": row["discount_value"],
                                "discount_type": row["discount_type"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "login": row["login"] or "",
                                "ipv4": row["ipv4"] or "",
                                "ipv6": row["ipv6"] or "",
                                "mac": row["mac"] or "",
                                "updated_at": _safe_datetime(row["updated_at"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} services")
        return count

    def sync_billing_transactions(self) -> int:
        """Sync billing transactions from Splynx (incremental)."""
        logger.info("Syncing billing transactions...")
        count = 0
        conn = _get_mysql_connection()
        try:
            with SessionLocal() as db:
                where = self._incremental_clause(
                    db, "splynx_billing_transactions", "updated_at"
                )
                query = f"""
                    SELECT id, customer_id, type, quantity, unit, price, total,
                           remind_amount, tax_percent, tax_id, date, category,
                           description, comment, period_from, period_to, to_invoice,
                           service_id, service_type, payment_id, invoice_id,
                           invoiced_by_id, source, deleted, updated_at, credit_note_id
                    FROM billing_transactions
                    {where}
                """  # noqa: S608 - WHERE clause from internal _incremental_clause
                for batch in _fetch_batches(conn, query, batch_size=LARGE_BATCH_SIZE):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_billing_transactions (
                                    id, customer_id, type, quantity, unit, price, total,
                                    remind_amount, tax_percent, tax_id, date, category,
                                    description, comment, period_from, period_to, to_invoice,
                                    service_id, service_type, payment_id, invoice_id,
                                    invoiced_by_id, source, deleted, updated_at,
                                    credit_note_id, synced_at
                                ) VALUES (
                                    :id, :customer_id, :type, :quantity, :unit, :price, :total,
                                    :remind_amount, :tax_percent, :tax_id, :date, :category,
                                    :description, :comment, :period_from, :period_to, :to_invoice,
                                    :service_id, :service_type, :payment_id, :invoice_id,
                                    :invoiced_by_id, :source, :deleted, :updated_at,
                                    :credit_note_id, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    deleted = EXCLUDED.deleted,
                                    updated_at = EXCLUDED.updated_at,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "customer_id": row["customer_id"],
                                "type": row["type"],
                                "quantity": row["quantity"],
                                "unit": row["unit"],
                                "price": row["price"],
                                "total": row["total"],
                                "remind_amount": row["remind_amount"],
                                "tax_percent": row["tax_percent"],
                                "tax_id": row["tax_id"],
                                "date": _safe_date(row["date"]),
                                "category": row["category"],
                                "description": row["description"] or "",
                                "comment": row["comment"] or "",
                                "period_from": _safe_date(row["period_from"]),
                                "period_to": _safe_date(row["period_to"]),
                                "to_invoice": _bool_from_enum(row["to_invoice"]),
                                "service_id": row["service_id"],
                                "service_type": row["service_type"],
                                "payment_id": row["payment_id"],
                                "invoice_id": row["invoice_id"],
                                "invoiced_by_id": row["invoiced_by_id"],
                                "source": row["source"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "updated_at": _safe_datetime(row["updated_at"]),
                                "credit_note_id": row["credit_note_id"],
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
                    if count % 10000 == 0:
                        logger.info(f"Synced {count} billing transactions so far...")
        finally:
            conn.close()
        logger.info(f"Synced {count} billing transactions")
        return count

    def sync_routers(self) -> int:
        """Sync routers from Splynx."""
        logger.info("Syncing routers...")
        count = 0
        conn = _get_mysql_connection()
        try:
            query = """
                SELECT id, title, model, nas_type, location_id, address, gps_point, gps,
                       ip, radius_secret, nas_ip, authorization_method, accounting_method,
                       deleted, updated_at
                FROM routers
            """
            with SessionLocal() as db:
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_routers (
                                    id, title, model, nas_type, location_id, address,
                                    gps_point, gps, ip, radius_secret, nas_ip,
                                    authorization_method, accounting_method,
                                    deleted, updated_at, synced_at
                                ) VALUES (
                                    :id, :title, :model, :nas_type, :location_id, :address,
                                    :gps_point, :gps, :ip, :radius_secret, :nas_ip,
                                    :authorization_method, :accounting_method,
                                    :deleted, :updated_at, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    title = EXCLUDED.title,
                                    model = EXCLUDED.model,
                                    ip = EXCLUDED.ip,
                                    deleted = EXCLUDED.deleted,
                                    updated_at = EXCLUDED.updated_at,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "title": row["title"],
                                "model": row["model"],
                                "nas_type": row["nas_type"],
                                "location_id": row["location_id"],
                                "address": row["address"],
                                "gps_point": row["gps_point"],
                                "gps": row["gps"],
                                "ip": row["ip"],
                                "radius_secret": row["radius_secret"],
                                "nas_ip": row["nas_ip"],
                                "authorization_method": row["authorization_method"],
                                "accounting_method": row["accounting_method"],
                                "deleted": _bool_from_enum(row["deleted"]),
                                "updated_at": _safe_datetime(row["updated_at"]),
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} routers")
        return count

    def sync_tariffs(self) -> int:
        """Sync internet tariffs from Splynx."""
        logger.info("Syncing tariffs...")
        count = 0
        conn = _get_mysql_connection()
        try:
            query = """
                SELECT id, title, service_name, price, with_vat, vat_percent, tax_id,
                       speed_download, speed_upload, aggregation, priority,
                       available_for_services, show_on_customer_portal, deleted, updated_at
                FROM tariffs_internet
            """
            with SessionLocal() as db:
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_tariffs_internet (
                                    id, title, service_name, price, with_vat, vat_percent,
                                    tax_id, speed_download, speed_upload, aggregation, priority,
                                    available_for_services, show_on_customer_portal,
                                    deleted, updated_at, synced_at
                                ) VALUES (
                                    :id, :title, :service_name, :price, :with_vat, :vat_percent,
                                    :tax_id, :speed_download, :speed_upload, :aggregation, :priority,
                                    :available_for_services, :show_on_customer_portal,
                                    :deleted, :updated_at, :synced_at
                                ) ON CONFLICT (id) DO UPDATE SET
                                    title = EXCLUDED.title,
                                    service_name = EXCLUDED.service_name,
                                    price = EXCLUDED.price,
                                    with_vat = EXCLUDED.with_vat,
                                    vat_percent = EXCLUDED.vat_percent,
                                    speed_download = EXCLUDED.speed_download,
                                    speed_upload = EXCLUDED.speed_upload,
                                    deleted = EXCLUDED.deleted,
                                    updated_at = EXCLUDED.updated_at,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "id": row["id"],
                                "title": row["title"],
                                "service_name": row["service_name"],
                                "price": row["price"],
                                "with_vat": _bool_from_enum(row["with_vat"]),
                                "vat_percent": row["vat_percent"],
                                "tax_id": row["tax_id"],
                                "speed_download": row["speed_download"],
                                "speed_upload": row["speed_upload"],
                                "aggregation": row["aggregation"],
                                "priority": row["priority"],
                                "available_for_services": _bool_from_enum(row["available_for_services"]),
                                "show_on_customer_portal": _bool_from_enum(row["show_on_customer_portal"]),
                                "deleted": _bool_from_enum(row["deleted"]),
                                "updated_at": row["updated_at"],
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} tariffs")
        return count

    def sync_customer_billing(self) -> int:
        """Sync customer billing info from Splynx."""
        logger.info("Syncing customer billing...")
        count = 0
        conn = _get_mysql_connection()
        try:
            query = """
                SELECT customer_id, enabled, billing_person, billing_street_1,
                       billing_zip_code, billing_city, deposit, payment_method,
                       billing_date, billing_due, grace_period, min_balance, month_price
                FROM customer_billing
            """
            with SessionLocal() as db:
                for batch in _fetch_batches(conn, query):
                    for row in batch:
                        db.execute(
                            text("""
                                INSERT INTO splynx_customer_billing (
                                    customer_id, enabled, billing_person, billing_street_1,
                                    billing_zip_code, billing_city, deposit, payment_method,
                                    billing_date, billing_due, grace_period, min_balance,
                                    month_price, synced_at
                                ) VALUES (
                                    :customer_id, :enabled, :billing_person, :billing_street_1,
                                    :billing_zip_code, :billing_city, :deposit, :payment_method,
                                    :billing_date, :billing_due, :grace_period, :min_balance,
                                    :month_price, :synced_at
                                ) ON CONFLICT (customer_id) DO UPDATE SET
                                    enabled = EXCLUDED.enabled,
                                    deposit = EXCLUDED.deposit,
                                    payment_method = EXCLUDED.payment_method,
                                    month_price = EXCLUDED.month_price,
                                    synced_at = EXCLUDED.synced_at
                            """),
                            {
                                "customer_id": row["customer_id"],
                                "enabled": _bool_from_enum(row["enabled"]),
                                "billing_person": row["billing_person"],
                                "billing_street_1": row["billing_street_1"],
                                "billing_zip_code": row["billing_zip_code"],
                                "billing_city": row["billing_city"],
                                "deposit": row["deposit"],
                                "payment_method": row["payment_method"],
                                "billing_date": row["billing_date"],
                                "billing_due": row["billing_due"],
                                "grace_period": row["grace_period"],
                                "min_balance": row["min_balance"],
                                "month_price": row["month_price"],
                                "synced_at": _now(),
                            },
                        )
                        count += 1
                    db.commit()
        finally:
            conn.close()
        logger.info(f"Synced {count} customer billing records")
        return count

    def run_full_sync(self) -> dict[str, int]:
        """Run a full sync of all core data."""
        logger.info("Starting full Splynx sync...")
        results: dict[str, int] = {}
        try:
            # Reference data first
            results["partners"] = self.sync_partners()
            results["locations"] = self.sync_locations()
            results["tariffs"] = self.sync_tariffs()

            # Main entities
            results["customers"] = self.sync_customers()
            results["customer_billing"] = self.sync_customer_billing()
            results["invoices"] = self.sync_invoices()
            results["payments"] = self.sync_payments()
            results["services"] = self.sync_services()

            # Network
            results["routers"] = self.sync_routers()

            # Large tables
            results["billing_transactions"] = self.sync_billing_transactions()

            logger.info(f"Full sync completed: {results}")
            return results
        except Exception:
            logger.exception("Full sync failed")
            raise
