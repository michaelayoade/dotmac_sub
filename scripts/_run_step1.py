"""Execute Step 1 of splynx_sync_complete.sql."""

from app.db import SessionLocal
from sqlalchemy import text


def main() -> None:
    db = SessionLocal()

    with open("scripts/splynx_sync_complete.sql") as f:
        sql = f.read()

    # Extract Step 1 (between STEP 1 header and STEP 2 header)
    step1_sql = sql.split("-- STEP 2:")[0]
    # Get everything after the first sub-step comment
    step1_sql = step1_sql[step1_sql.index("-- 1a."):]

    # Split into individual statements
    statements = []
    current: list[str] = []
    for line in step1_sql.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--") or stripped == "":
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []

    labels = [
        "1a: Seed map_monitoring for IP-matchable (49 devices)",
        "1b: Seed map_monitoring for hostname-matchable (6 devices)",
        "1c: Create network_devices for truly orphaned (~174)",
        "1d: Seed map_monitoring for newly created devices",
        "1e: Insert device_metrics for all newly mapped logs",
    ]

    print(f"Found {len(statements)} statements\n")

    for stmt, label in zip(statements, labels):
        print(label)
        result = db.execute(text(stmt))
        print(f"  -> rows affected: {result.rowcount:,}\n")

    db.commit()
    print("Step 1 committed successfully.")
    db.close()


if __name__ == "__main__":
    main()
