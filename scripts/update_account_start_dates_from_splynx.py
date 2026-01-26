#!/usr/bin/env python3
"""
Bulk update subscriber account_start_date from Splynx date_add field.

This script updates the account_start_date field in the subscribers table
using the original customer creation date (date_add) from Splynx staging data.

Usage:
    poetry run python scripts/update_account_start_dates_from_splynx.py

Requirements:
    - splynx_staging schema must exist with map_customers and splynx_customers tables
    - These are created during the initial Splynx migration process
"""

from app.db import SessionLocal
from sqlalchemy import text


def update_account_start_dates():
    """Update subscriber account_start_date from Splynx date_add."""
    db = SessionLocal()
    try:
        # Check how many records will be updated
        result = db.execute(text("""
            SELECT COUNT(*)
            FROM subscribers s
            JOIN splynx_staging.map_customers mc ON mc.subscriber_id = s.id
            JOIN splynx_staging.splynx_customers sc ON sc.id = mc.splynx_customer_id
            WHERE sc.date_add IS NOT NULL
        """))
        count = result.scalar()
        print(f"Records to update: {count}")

        if count == 0:
            print("No records to update.")
            return

        # Show date range
        result = db.execute(text("""
            SELECT MIN(sc.date_add), MAX(sc.date_add)
            FROM splynx_staging.map_customers mc
            JOIN splynx_staging.splynx_customers sc ON sc.id = mc.splynx_customer_id
            WHERE sc.date_add IS NOT NULL
        """))
        row = result.fetchone()
        print(f"Date range: {row[0]} to {row[1]}")

        # Confirm before updating
        response = input("\nProceed with update? (yes/no): ")
        if response.lower() != "yes":
            print("Update cancelled.")
            return

        # Run the bulk update
        result = db.execute(text("""
            UPDATE subscribers s
            SET account_start_date = sc.date_add::timestamp with time zone
            FROM splynx_staging.map_customers mc
            JOIN splynx_staging.splynx_customers sc ON sc.id = mc.splynx_customer_id
            WHERE mc.subscriber_id = s.id
              AND sc.date_add IS NOT NULL
        """))

        db.commit()
        print(f"\nUpdated {result.rowcount} subscriber records with Splynx start dates")

        # Verify the update
        result = db.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN account_start_date::date != created_at::date THEN 1 END) as different_dates
            FROM subscribers
            WHERE account_start_date IS NOT NULL
        """))
        row = result.fetchone()
        print(f"\nVerification:")
        print(f"  Total with account_start_date: {row[0]}")
        print(f"  With different date than created_at: {row[1]}")

    finally:
        db.close()


if __name__ == "__main__":
    update_account_start_dates()
