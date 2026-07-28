#!/usr/bin/env python3
"""One-time backfill of POP + subscriber coordinates from the Splynx restore.

Sub owns POP/BTS site coordinates (``pop_sites``) and subscriber install
coordinates (``addresses``) going forward, but never captured them — the audit
found all 36 active POPs and every ONT/address without coordinates. The
historical source is the legacy Splynx billing DB (being retired), restored into
the ``splynx_restore`` MySQL container on seabone:

  * ``network_sites`` (``title``, ``gps``) -> Sub ``pop_sites`` (matched by name)
  * ``customers``     (``id``,    ``gps``) -> Sub ``addresses`` (matched by
    ``customers.id == subscribers.splynx_customer_id``, then the subscriber's
    service/primary address)

This runner only *reads* Splynx (SELECT), does the Splynx->Sub entity matching,
and hands clean ``{id: (lat, lng)}`` maps to the ``gis.spatial_sync`` owner
(``GeoSync.apply_pop_coordinates`` / ``apply_address_coordinates``), which owns
the geometry write and the ``geo_locations`` projection. GPS parsing (range
validation + axis-swap repair) lives in ``app.services.splynx_geo_import``.

Connection (read-only) — pass via args or env:
  SPLYNX_DB_HOST (default 127.0.0.1)  SPLYNX_DB_PORT (default 3306)
  SPLYNX_DB_USER (default root)       SPLYNX_DB_PASSWORD
  SPLYNX_DB_NAME (default splynx)
Sub target DB comes from the app's ``SessionLocal`` (``DATABASE_URL``).

Usage (on seabone, against the restore + the target Sub DB):
  python scripts/migration/import_splynx_geo.py --dry-run           # report only
  python scripts/migration/import_splynx_geo.py --out /tmp/splynx-geo
Always start with --dry-run and review the CSVs before writing.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models.network_monitoring import PopSite  # noqa: E402
from app.models.subscriber import Address, AddressType, Subscriber  # noqa: E402
from app.services.gis_sync import GeoSync  # noqa: E402
from app.services.splynx_geo_import import (  # noqa: E402
    ParsedPoint,
    clean_pop_name,
    detect_region,
    keys_match,
    normalize_site_name,
    parse_gps,
)
from app.services.web_network_pop_sites import create_site  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("SPLYNX_DB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("SPLYNX_DB_PORT", "3306"))
    )
    parser.add_argument("--user", default=os.getenv("SPLYNX_DB_USER", "root"))
    parser.add_argument("--password", default=os.getenv("SPLYNX_DB_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("SPLYNX_DB_NAME", "splynx"))
    parser.add_argument(
        "--sites-json",
        type=Path,
        help="Read network_sites from a JSON file instead of Splynx "
        "(list of {id,title,gps}); pre-filtered to deleted=0 and non-empty gps. "
        "Use with --customers-json for the file method (no DB connection).",
    )
    parser.add_argument(
        "--customers-json",
        type=Path,
        help="Read customers from a JSON file instead of Splynx "
        "(list of {id,street_1,city,zip_code,gps}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matches and would-be writes; do not touch the Sub DB.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("splynx-geo"),
        help="Directory for the unmatched / needs-review CSV artifacts.",
    )
    parser.add_argument(
        "--skip-pops", action="store_true", help="Skip the POP-site backfill."
    )
    parser.add_argument(
        "--create-missing-pops",
        action="store_true",
        help="Create Sub POPs for geocoded Splynx BTS sites with no name match.",
    )
    parser.add_argument(
        "--skip-subscribers",
        action="store_true",
        help="Skip the subscriber-address backfill.",
    )
    parser.add_argument(
        "--materialize-addresses",
        action="store_true",
        help="Create the canonical service Address per subscriber from Splynx "
        "text + gps (the customer-domain step), instead of only writing "
        "coordinates onto existing addresses.",
    )
    return parser.parse_args()


def splynx_connect(args: argparse.Namespace) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        cursorclass=DictCursor,
        read_default_group="",
    )


class _FileCursor:
    """Minimal pymysql-cursor stand-in serving pre-extracted JSON rows.

    Routes by the table named in the SQL so the resolvers stay unchanged.
    """

    def __init__(self, sites: list[dict], customers: list[dict]) -> None:
        self._sites = sites
        self._customers = customers
        self._rows: list[dict] = []

    def __enter__(self) -> _FileCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, *args: object) -> None:
        lowered = sql.lower()
        if "network_sites" in lowered:
            self._rows = self._sites
        elif "customers" in lowered:
            self._rows = self._customers
        else:
            self._rows = []

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class FileSplynx:
    """Splynx source backed by JSON files (the file method, no DB)."""

    def __init__(self, sites: list[dict], customers: list[dict]) -> None:
        self._sites = sites
        self._customers = customers

    def cursor(self) -> _FileCursor:
        return _FileCursor(self._sites, self._customers)

    def close(self) -> None:
        return None


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# --- POP sites ---------------------------------------------------------------


def _match_pop(site_key: str, pop_index: dict[str, list[PopSite]]) -> PopSite | None:
    """Match a Splynx site key to exactly one Sub POP, else ``None``.

    Exact normalized equality wins; otherwise a unique token-subset match
    (``keys_match``) — e.g. "boi asokoro" -> "asokoro".
    """
    exact = pop_index.get(site_key, [])
    if exact:
        return exact[0] if len(exact) == 1 else None
    hits = [
        pop
        for key, pops in pop_index.items()
        for pop in pops
        if keys_match(site_key, key)
    ]
    return hits[0] if len(hits) == 1 else None


def resolve_pop_coordinates(
    splynx, db, create_missing: bool, dry_run: bool
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Return (``{pop_site_id: (lat,lng)}``, created rows, unmatched, review)."""
    with splynx.cursor() as cur:
        cur.execute(
            "SELECT id, title, gps FROM network_sites "
            "WHERE deleted='0' AND gps IS NOT NULL AND gps<>''"
        )
        sites = cur.fetchall()

    pop_index: dict[str, list[PopSite]] = defaultdict(list)
    for pop in db.query(PopSite).all():
        key = normalize_site_name(pop.name)
        if key:
            pop_index[key].append(pop)

    coordinates: dict = {}
    created: list[dict] = []
    unmatched: list[dict] = []
    review: list[dict] = []
    for site in sites:
        point = parse_gps(site["gps"])
        key = normalize_site_name(site["title"])
        if point is None:
            unmatched.append(
                {
                    "splynx_id": site["id"],
                    "title": site["title"],
                    "gps": site["gps"],
                    "reason": "unparseable_gps",
                    "match_key": key,
                }
            )
            continue
        pop = _match_pop(key, pop_index)
        if pop is None:
            record = {
                "splynx_id": site["id"],
                "title": site["title"],
                "gps": site["gps"],
                "reason": "no_pop_match",
                "match_key": key,
            }
            if create_missing:
                name = clean_pop_name(site["title"])
                if dry_run:
                    record["action"] = "would_create"
                    record["pop_name"] = name
                    created.append(record)
                else:
                    new_pop = create_site(
                        db,
                        {
                            "name": name,
                            "region": detect_region(site["title"]),
                            "is_active": True,
                            "notes": f"Imported from Splynx network_sites id={site['id']}",
                        },
                    )
                    pop_index[normalize_site_name(new_pop.name)].append(new_pop)
                    coordinates[new_pop.id] = (point.latitude, point.longitude)
                    record["action"] = "created"
                    record["pop_name"] = name
                    created.append(record)
                continue
            unmatched.append(record)
            continue
        coordinates[pop.id] = (point.latitude, point.longitude)
        if point.swapped or point.needs_review:
            review.append(
                {
                    "kind": "pop",
                    "splynx_id": site["id"],
                    "title": site["title"],
                    "pop_name": pop.name,
                    "gps": site["gps"],
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "swapped": point.swapped,
                    "needs_review": point.needs_review,
                }
            )
    return coordinates, created, unmatched, review


# --- Subscriber service-address materialization ------------------------------


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fit(value: str | None, max_len: int) -> str | None:
    """Truncate to a Sub column's length; Splynx free-text often overflows."""
    if value is None:
        return None
    return value[:max_len]


def resolve_subscriber_addresses(
    splynx, db, dry_run: bool
) -> tuple[dict, list[dict], dict]:
    """Materialize the canonical service ``Address`` per subscriber from Splynx.

    Sub's ``addresses`` table is empty and subscriber location is thin inline
    text; Splynx ``customers`` carries the richer street/city/zip + gps. This
    builds one service Address per matched subscriber (address text + Splynx
    coord), which the customer domain owns. Returns
    (``{address_id: (lat,lng)}`` for coordinate projection, sample rows for the
    CSV, counts).

    In ``dry_run`` nothing is written — only counts + a sample are produced.
    """
    with splynx.cursor() as cur:
        cur.execute("SELECT id, street_1, city, zip_code, gps FROM customers")
        customers = {int(r["id"]): r for r in cur.fetchall()}

    subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.splynx_customer_id.in_(customers.keys()))
        .all()
        if customers
        else []
    )

    counts = {
        "splynx_customers": len(customers),
        "matched_subscribers": len(subscribers),
        "already_has_address": 0,
        "would_create": 0,
        "with_coord": 0,
        "skipped_no_text_no_coord": 0,
        "errors": 0,
    }
    coordinates: dict = {}
    sample: list[dict] = []
    for sub in subscribers:
        cust = customers[sub.splynx_customer_id]
        if any(a.address_type == AddressType.service for a in sub.addresses):
            counts["already_has_address"] += 1
            continue
        point = parse_gps(cust["gps"])
        line1 = _clean(cust["street_1"]) or _clean(sub.address_line1)
        city = _clean(cust["city"]) or _clean(sub.city)
        # Splynx `city` is dirty — some rows hold a numeric login id, not a city.
        if city and city.isdigit():
            city = None
        # address_line1 is NOT NULL; fall back to city, else skip unless a coord
        # still makes a location worth recording.
        resolved_line1 = line1 or city
        if resolved_line1 is None and point is None:
            counts["skipped_no_text_no_coord"] += 1
            continue
        if not dry_run:
            try:
                with db.begin_nested():
                    address = Address(
                        subscriber_id=sub.id,
                        address_type=AddressType.service,
                        is_primary=True,
                        address_line1=_fit(resolved_line1, 120)
                        or "(no street on record)",
                        address_line2=_fit(_clean(sub.address_line2), 120),
                        city=_fit(city, 80),
                        region=_fit(_clean(sub.region), 80),
                        postal_code=_fit(_clean(cust["zip_code"]), 20),
                    )
                    db.add(address)
                    db.flush()
                if point is not None:
                    coordinates[address.id] = (point.latitude, point.longitude)
            except Exception as exc:  # isolate a bad row, keep the batch going
                counts["errors"] += 1
                logging.warning(
                    "address materialize failed for splynx_customer_id=%s: %s",
                    sub.splynx_customer_id,
                    exc,
                )
                continue
        counts["would_create"] += 1
        if point is not None:
            counts["with_coord"] += 1
        if len(sample) < 25:
            sample.append(
                {
                    "splynx_customer_id": sub.splynx_customer_id,
                    "address_line1": resolved_line1,
                    "city": city,
                    "has_coord": point is not None,
                }
            )
    if not dry_run:
        db.commit()
    return coordinates, sample, counts


# --- Subscriber coordinates (existing addresses) -----------------------------


def _pick_address(addresses: list[Address]) -> Address | None:
    """Choose the subscriber's service/install address deterministically."""
    if not addresses:
        return None
    for predicate in (
        lambda a: a.is_primary and a.address_type == AddressType.service,
        lambda a: a.is_primary,
        lambda a: a.address_type == AddressType.service,
    ):
        match = [a for a in addresses if predicate(a)]
        if len(match) == 1:
            return match[0]
        if match:
            # Multiple equally-ranked candidates: fall through to a stable pick.
            return sorted(match, key=lambda a: a.created_at)[0]
    return sorted(addresses, key=lambda a: a.created_at)[0]


def resolve_subscriber_coordinates(splynx, db) -> tuple[dict, list[dict], list[dict]]:
    """Return (``{address_id: (lat,lng)}``, unmatched rows, review rows)."""
    with splynx.cursor() as cur:
        cur.execute("SELECT id, gps FROM customers WHERE gps IS NOT NULL AND gps<>''")
        customers = cur.fetchall()

    parsed: dict[int, ParsedPoint] = {}
    unmatched: list[dict] = []
    for row in customers:
        point = parse_gps(row["gps"])
        if point is None:
            unmatched.append(
                {
                    "splynx_customer_id": row["id"],
                    "gps": row["gps"],
                    "reason": "unparseable_gps",
                }
            )
            continue
        parsed[int(row["id"])] = point

    # Resolve the Splynx customer ids to Sub subscribers + their addresses.
    subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.splynx_customer_id.in_(parsed.keys()))
        .all()
        if parsed
        else []
    )
    by_splynx_id = {s.splynx_customer_id: s for s in subscribers}

    coordinates: dict = {}
    review: list[dict] = []
    for splynx_id, point in parsed.items():
        subscriber = by_splynx_id.get(splynx_id)
        if subscriber is None:
            unmatched.append(
                {
                    "splynx_customer_id": splynx_id,
                    "gps": None,
                    "reason": "no_subscriber",
                }
            )
            continue
        address = _pick_address(list(subscriber.addresses))
        if address is None:
            unmatched.append(
                {"splynx_customer_id": splynx_id, "gps": None, "reason": "no_address"}
            )
            continue
        coordinates[address.id] = (point.latitude, point.longitude)
        if point.swapped or point.needs_review:
            review.append(
                {
                    "kind": "subscriber",
                    "splynx_customer_id": splynx_id,
                    "address_id": str(address.id),
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "swapped": point.swapped,
                    "needs_review": point.needs_review,
                }
            )
    return coordinates, unmatched, review


def main() -> int:
    args = parse_args()
    if args.sites_json or args.customers_json:
        if not (args.sites_json and args.customers_json):
            raise SystemExit("--sites-json and --customers-json must be used together")
        sites = json.loads(args.sites_json.read_text())
        customers = json.loads(args.customers_json.read_text())
        splynx: object = FileSplynx(sites, customers)
    else:
        splynx = splynx_connect(args)
    db = SessionLocal()
    summary: dict[str, object] = {"dry_run": args.dry_run}
    all_review: list[dict] = []
    try:
        if not args.skip_pops:
            coords, created, unmatched, review = resolve_pop_coordinates(
                splynx, db, args.create_missing_pops, args.dry_run
            )
            all_review.extend(review)
            _write_csv(
                args.out / "unmatched_pops.csv",
                unmatched,
                ["splynx_id", "title", "gps", "reason", "match_key"],
            )
            _write_csv(
                args.out / "created_pops.csv",
                created,
                [
                    "splynx_id",
                    "title",
                    "gps",
                    "reason",
                    "match_key",
                    "action",
                    "pop_name",
                ],
            )
            created_count = len(created)
            existing_matches = len(coords) - (0 if args.dry_run else created_count)
            would_write = len(coords) + (created_count if args.dry_run else 0)
            if args.dry_run:
                pop_result = {"would_write": would_write}
            else:
                pop_result = GeoSync.apply_pop_coordinates(db, coords).__dict__
            summary["pops"] = {
                "source_geocoded": existing_matches + created_count + len(unmatched),
                "matched_existing": existing_matches,
                "created": created_count,
                "unmatched": len(unmatched),
                "result": pop_result,
            }

        if not args.skip_subscribers and args.materialize_addresses:
            coords, sample, counts = resolve_subscriber_addresses(
                splynx, db, args.dry_run
            )
            _write_csv(
                args.out / "materialized_addresses_sample.csv",
                sample,
                ["splynx_customer_id", "address_line1", "city", "has_coord"],
            )
            if args.dry_run:
                sub_result = {"coords_would_write": counts["with_coord"]}
            else:
                sub_result = GeoSync.apply_address_coordinates(db, coords).__dict__
            summary["subscribers"] = {**counts, "result": sub_result}
        elif not args.skip_subscribers:
            coords, unmatched, review = resolve_subscriber_coordinates(splynx, db)
            all_review.extend(review)
            _write_csv(
                args.out / "unmatched_subscribers.csv",
                unmatched,
                ["splynx_customer_id", "gps", "reason"],
            )
            if args.dry_run:
                sub_result = {"would_write": len(coords)}
            else:
                sub_result = GeoSync.apply_address_coordinates(db, coords).__dict__
            summary["subscribers"] = {
                "matched_to_sub": len(coords),
                "unmatched": len(unmatched),
                "result": sub_result,
            }

        _write_csv(
            args.out / "needs_review.csv",
            all_review,
            [
                "kind",
                "splynx_id",
                "splynx_customer_id",
                "title",
                "pop_name",
                "address_id",
                "gps",
                "latitude",
                "longitude",
                "swapped",
                "needs_review",
            ],
        )
    finally:
        db.close()
        splynx.close()

    print(json.dumps(summary, indent=2, default=str))
    print(
        f"\nArtifacts written to {args.out}/ "
        f"(unmatched_pops.csv, unmatched_subscribers.csv, needs_review.csv)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
