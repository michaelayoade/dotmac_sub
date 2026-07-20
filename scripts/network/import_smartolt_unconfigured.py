#!/usr/bin/env python3
"""Audit SmartOLT CSV observations against Sub without mutating canonical state.

Direct SmartOLT import writes are retired. This command is preview-only: it
records exact serial/OLT/F/S/P agreement, explicit gaps, and conflicts for
review. It never creates or updates an ONT, assignment, subscription,
credential, PON, or RADIUS record. Customer names, account-number suffixes,
addresses, and credentials are not matching inputs.

Example:
    poetry run python scripts/network/import_smartolt_unconfigured.py \
      --csv-path SmartOLT_onus.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CSV = "SmartOLT_onus_list_2026-03-21_12_07_53.075749.csv"


@dataclass(frozen=True)
class CsvObservation:
    row_number: int
    serial_number: str
    olt_name: str
    observed_fsp: str | None
    username_present: bool
    password_present: bool


@dataclass(frozen=True)
class AuditResult:
    row_number: int
    observation_sha256: str
    serial_number: str
    olt_name: str
    observed_fsp: str | None
    ont_unit_id: str | None
    observed_olt_id: str | None
    observed_pon_port_id: str | None
    canonical_olt_id: str | None
    canonical_pon_port_id: str | None
    active_assignment_ids: tuple[str, ...]
    status: str
    reasons: tuple[str, ...]


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _normalize_serial(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", _clean(value)).upper()


def _observed_fsp(board: str | None, port: str | None) -> str | None:
    board_text = _clean(board)
    port_text = _clean(port)
    if not board_text or not port_text:
        return None
    return f"0/{board_text}/{port_text}"


def _digest(observation: CsvObservation) -> str:
    payload = {
        "olt_name": observation.olt_name,
        "observed_fsp": observation.observed_fsp,
        "row_number": observation.row_number,
        "serial_number": observation.serial_number,
        "source": "smartolt_csv_audit",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_csv(path: Path, *, limit: int | None = None) -> list[CsvObservation]:
    observations: list[CsvObservation] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, raw in enumerate(reader, start=2):
            observations.append(
                CsvObservation(
                    row_number=row_number,
                    serial_number=_clean(raw.get("SN"))
                    or _clean(raw.get("ONU external ID")),
                    olt_name=_clean(raw.get("OLT")),
                    observed_fsp=_observed_fsp(raw.get("Board"), raw.get("Port")),
                    username_present=bool(_clean(raw.get("Username"))),
                    password_present=bool(_clean(raw.get("Password"))),
                )
            )
            if limit is not None and len(observations) >= max(0, limit):
                break
    return observations


def _audit(observations: list[CsvObservation]) -> list[AuditResult]:
    with SessionLocal() as db:
        olts = list(
            db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        )
        pon_ports = list(
            db.scalars(select(PonPort).where(PonPort.is_active.is_(True))).all()
        )
        onts = list(db.scalars(select(OntUnit)).all())
        assignments = list(
            db.scalars(
                select(OntAssignment).where(OntAssignment.active.is_(True))
            ).all()
        )

    olts_by_name: dict[str, list[OLTDevice]] = defaultdict(list)
    for indexed_olt in olts:
        olts_by_name[_clean(indexed_olt.name).casefold()].append(indexed_olt)
    onts_by_serial: dict[str, list[OntUnit]] = defaultdict(list)
    for indexed_ont in onts:
        serial = _normalize_serial(indexed_ont.serial_number)
        if serial:
            onts_by_serial[serial].append(indexed_ont)
    pon_by_exact_key: dict[tuple[str, str], list[PonPort]] = defaultdict(list)
    for indexed_pon in pon_ports:
        pon_by_exact_key[(str(indexed_pon.olt_id), _clean(indexed_pon.name))].append(
            indexed_pon
        )
    assignments_by_ont: dict[str, list[OntAssignment]] = defaultdict(list)
    for assignment in assignments:
        assignments_by_ont[str(assignment.ont_unit_id)].append(assignment)

    results: list[AuditResult] = []
    for observation in observations:
        reasons: list[str] = []
        conflicting = False
        serial = _normalize_serial(observation.serial_number)
        ont_candidates = onts_by_serial.get(serial, []) if serial else []
        olt_candidates = olts_by_name.get(observation.olt_name.casefold(), [])
        ont = ont_candidates[0] if len(ont_candidates) == 1 else None
        olt = olt_candidates[0] if len(olt_candidates) == 1 else None

        if not serial:
            reasons.append("missing ONT serial")
        elif not ont_candidates:
            reasons.append("no exact local ONT serial")
        elif len(ont_candidates) > 1:
            reasons.append("serial matches multiple local ONTs")
            conflicting = True

        if not observation.olt_name:
            reasons.append("missing OLT name")
        elif not olt_candidates:
            reasons.append("no exact active OLT name")
        elif len(olt_candidates) > 1:
            reasons.append("OLT name matches multiple active OLTs")
            conflicting = True

        pon_candidates: list[PonPort] = []
        if observation.observed_fsp is None:
            reasons.append("missing exact F/S/P")
        elif olt is not None:
            pon_candidates = pon_by_exact_key.get(
                (str(olt.id), observation.observed_fsp), []
            )
            if not pon_candidates:
                reasons.append("no exact active modeled PON")
            elif len(pon_candidates) > 1:
                reasons.append("F/S/P matches multiple active modeled PONs")
                conflicting = True
        pon = pon_candidates[0] if len(pon_candidates) == 1 else None

        active_assignments = assignments_by_ont.get(str(ont.id), []) if ont else []
        if ont is not None and olt is not None:
            if ont.olt_device_id is not None and ont.olt_device_id != olt.id:
                reasons.append("observed OLT conflicts with canonical ONT OLT")
                conflicting = True
        if ont is not None and pon is not None:
            if ont.pon_port_id is not None and ont.pon_port_id != pon.id:
                reasons.append("observed PON conflicts with canonical ONT PON")
                conflicting = True
            disagreeing = [
                row for row in active_assignments if row.pon_port_id != pon.id
            ]
            if disagreeing:
                reasons.append("active assignment PON conflicts with observation")
                conflicting = True
        if len(active_assignments) > 1:
            reasons.append("ONT has multiple active assignments")
            conflicting = True
        if any(row.subscription_id is None for row in active_assignments):
            reasons.append("active assignment lacks exact subscription identity")

        status = (
            "review_required"
            if conflicting
            else "incomplete"
            if reasons
            else "confirmed"
        )
        results.append(
            AuditResult(
                row_number=observation.row_number,
                observation_sha256=_digest(observation),
                serial_number=observation.serial_number,
                olt_name=observation.olt_name,
                observed_fsp=observation.observed_fsp,
                ont_unit_id=str(ont.id) if ont else None,
                observed_olt_id=str(olt.id) if olt else None,
                observed_pon_port_id=str(pon.id) if pon else None,
                canonical_olt_id=(
                    str(ont.olt_device_id) if ont and ont.olt_device_id else None
                ),
                canonical_pon_port_id=(
                    str(ont.pon_port_id) if ont and ont.pon_port_id else None
                ),
                active_assignment_ids=tuple(
                    sorted(str(row.id) for row in active_assignments)
                ),
                status=status,
                reasons=tuple(reasons),
            )
        )
    return results


def _write_results(
    output_dir: Path,
    observations: list[CsvObservation],
    results: list[AuditResult],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for result in results:
        data = asdict(result)
        data["active_assignment_ids"] = ",".join(result.active_assignment_ids)
        data["reasons"] = " | ".join(result.reasons)
        rows.append(data)
    csv_path = output_dir / "observations.csv"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "preview_only",
        "source": "smartolt_csv_audit",
        "total_rows": len(results),
        "credential_presence": {
            "password_present_rows": sum(row.password_present for row in observations),
            "username_present_rows": sum(row.username_present for row in observations),
        },
        "outcomes": {
            status: sum(row.status == status for row in results)
            for status in ("confirmed", "incomplete", "review_required")
        },
        "results": [asdict(result) for result in results],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit SmartOLT ONU CSV observations without writing Sub state"
    )
    parser.add_argument("--csv-path", default=DEFAULT_CSV, help="SmartOLT CSV export")
    parser.add_argument(
        "--output-dir",
        default=f"tmp/smartolt_audit_{_utc_stamp()}",
        help="Directory for preview-only audit artifacts",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit source rows inspected"
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    observations = _load_csv(csv_path, limit=args.limit)
    results = _audit(observations)
    output_dir = Path(args.output_dir)
    _write_results(output_dir, observations, results)
    logger.info("Preview-only audit complete. Review artifacts in %s", output_dir)


if __name__ == "__main__":
    main()
