#!/usr/bin/env python3
"""Import SmartOLT ONU CSV rows into reversible DotMac ONT/account state.

This utility is intentionally conservative for production use.

It only applies rows when all of the following can be resolved unambiguously:
- human-readable ONT serial number
- PPPoE username/password
- subscriber match
- exactly one active subscription
- OLT + PON port
- no conflicting active ONT assignment
- no conflicting active access credential

Default mode is dry-run and writes review artifacts to disk.

Examples:
    poetry run python scripts/import_smartolt_unconfigured.py
    poetry run python scripts/import_smartolt_unconfigured.py --apply
    poetry run python scripts/import_smartolt_unconfigured.py --rollback artifacts/.../rollback.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.network import (
    GponChannel,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    PonType,
    WanMode,
)
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber
from app.services.credential_crypto import encrypt_credential
from app.services.radius import ensure_radius_users_for_subscription

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CSV = "SmartOLT_onus_list_2026-03-21_12_07_53.075749.csv"


@dataclass
class CsvRow:
    row_number: int
    serial_number: str
    model: str
    name: str
    olt_name: str
    board: str
    port: str
    allocated_onu: str
    pon_type: str
    wan_mode: str
    username: str
    password: str
    address: str
    status: str
    raw: dict[str, str]


@dataclass
class RowPlan:
    row_number: int
    serial_number: str
    username: str
    olt_name: str
    subscriber_id: str | None
    subscriber_number: str | None
    subscription_id: str | None
    ont_id: str | None
    pon_port_id: str | None
    assignment_id: str | None
    credential_id: str | None
    can_apply: bool
    reasons: list[str]
    actions: list[str]
    create_ont: bool = False
    create_assignment: bool = False
    create_credential: bool = False
    update_credential: bool = False
    update_subscription_login: bool = False
    update_ont: bool = False


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_serial(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "").strip()).upper()


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _suffix5(value: str | None) -> str:
    digits = _digits(value)
    return digits[-5:] if len(digits) >= 5 else ""


def _derive_vendor(serial_number: str, olt: OLTDevice | None) -> str | None:
    if olt and olt.vendor:
        return olt.vendor
    serial = _normalize_serial(serial_number)
    if serial.startswith(("HW", "HWT")):
        return "Huawei"
    if serial.startswith("ZT"):
        return "ZTE"
    if serial.startswith("NK"):
        return "Nokia"
    return None


def _extract_fsp(name: str | None) -> str | None:
    text = _clean(name)
    match = re.search(r"(\d+/\d+/\d+)\s*$", text)
    if match:
        return match.group(1)
    return text if re.fullmatch(r"\d+/\d+/\d+", text) else None


def _serialize_model(model: Any, fields: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in fields:
        data[field] = _json_default(getattr(model, field))
    return data


def _restore_enum(enum_cls: type[Enum], value: Any) -> Any:
    if value in (None, ""):
        return None
    return enum_cls(value)


def _load_csv_rows(csv_path: Path) -> list[CsvRow]:
    rows: list[CsvRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, raw in enumerate(reader, start=2):
            serial = _clean(raw.get("SN")) or _clean(raw.get("ONU external ID"))
            rows.append(
                CsvRow(
                    row_number=index,
                    serial_number=serial,
                    model=_clean(raw.get("Onu Type")),
                    name=_clean(raw.get("Name")),
                    olt_name=_clean(raw.get("OLT")),
                    board=_clean(raw.get("Board")),
                    port=_clean(raw.get("Port")),
                    allocated_onu=_clean(raw.get("Allocated ONU")),
                    pon_type=_clean(raw.get("PON Type")),
                    wan_mode=_clean(raw.get("WAN mode")),
                    username=_clean(raw.get("Username")),
                    password=_clean(raw.get("Password")),
                    address=_clean(raw.get("Address")),
                    status=_clean(raw.get("Status")),
                    raw={str(k): _clean(v) for k, v in raw.items()},
                )
            )
    return rows


def _preselect_rows(rows: list[CsvRow]) -> tuple[list[CsvRow], dict[int, list[str]]]:
    skipped: dict[int, list[str]] = defaultdict(list)
    by_serial: dict[str, list[CsvRow]] = defaultdict(list)
    for row in rows:
        serial = _normalize_serial(row.serial_number)
        if not serial:
            skipped[row.row_number].append("Missing serial number")
            continue
        by_serial[serial].append(row)

    selected: list[CsvRow] = []
    seen_usernames: dict[str, list[int]] = defaultdict(list)
    for serial, group in by_serial.items():
        candidates = [
            row
            for row in group
            if row.wan_mode.lower() == "pppoe"
            and _clean(row.username)
            and _clean(row.password)
        ]
        if not candidates:
            for row in group:
                skipped[row.row_number].append(
                    "Row is not a usable PPPoE import candidate"
                )
            continue
        chosen = candidates[0]
        selected.append(chosen)
        for row in group:
            if row.row_number != chosen.row_number:
                skipped[row.row_number].append(
                    f"Duplicate serial in CSV; selected row {chosen.row_number}"
                )
        seen_usernames[chosen.username].append(chosen.row_number)

    duplicate_usernames = {
        username: row_numbers
        for username, row_numbers in seen_usernames.items()
        if username and len(row_numbers) > 1
    }
    filtered: list[CsvRow] = []
    for row in selected:
        dup_rows = duplicate_usernames.get(row.username)
        if dup_rows:
            skipped[row.row_number].append(
                f"Duplicate PPPoE username in CSV across rows {dup_rows}"
            )
            continue
        filtered.append(row)
    filtered.sort(key=lambda item: item.row_number)
    return filtered, skipped


def _resolve_subscriber(
    row: CsvRow, subscribers: list[Subscriber]
) -> tuple[Subscriber | None, str | None]:
    active_subscribers = [item for item in subscribers if item.is_active]

    exact_keys = {
        _clean(row.username).lower(),
        _clean(row.name).lower() if row.name.isdigit() else "",
        _suffix5(row.username).lower(),
    }
    exact_keys = {key for key in exact_keys if key}

    exact_matches: list[Subscriber] = []
    suffix_matches: list[Subscriber] = []
    suffix = _suffix5(row.username)
    for subscriber in active_subscribers:
        number = _clean(subscriber.subscriber_number).lower()
        account = _clean(subscriber.account_number).lower()
        if number in exact_keys or account in exact_keys:
            exact_matches.append(subscriber)
            continue
        if suffix and (
            (number and _digits(number).endswith(suffix))
            or (account and _digits(account).endswith(suffix))
        ):
            suffix_matches.append(subscriber)

    exact_unique = {str(item.id): item for item in exact_matches}
    if len(exact_unique) == 1:
        return next(iter(exact_unique.values())), "exact_number_match"
    if len(exact_unique) > 1:
        return None, "ambiguous_exact_subscriber_match"

    suffix_unique = {str(item.id): item for item in suffix_matches}
    if len(suffix_unique) == 1:
        return next(iter(suffix_unique.values())), "suffix5_match"
    if len(suffix_unique) > 1:
        return None, "ambiguous_suffix5_subscriber_match"
    return None, "subscriber_not_found"


def _resolve_olt(row: CsvRow, olts: list[OLTDevice]) -> OLTDevice | None:
    target = row.olt_name.lower()
    exact = [
        olt for olt in olts if _clean(olt.name).lower() == target and olt.is_active
    ]
    if len(exact) == 1:
        return exact[0]
    return None


def _resolve_pon_port(row: CsvRow, olt_ports: list[PonPort]) -> PonPort | None:
    if not row.board or not row.port:
        return None
    candidate_fsp = f"0/{row.board}/{row.port}"
    candidate_short = f"{row.board}/{row.port}"
    extracted = []
    for port in olt_ports:
        fsp = _extract_fsp(port.name)
        extracted.append((port, fsp))

    exact = [port for port, fsp in extracted if fsp == candidate_fsp]
    if len(exact) == 1:
        return exact[0]

    short = [port for port, fsp in extracted if fsp and fsp.endswith(candidate_short)]
    if len(short) == 1:
        return short[0]

    return None


def _resolve_active_subscription(
    subscriptions: list[Subscription], subscriber_id: str
) -> tuple[Subscription | None, str | None]:
    active = [
        item
        for item in subscriptions
        if str(item.subscriber_id) == subscriber_id
        and item.status == SubscriptionStatus.active
    ]
    if len(active) == 1:
        return active[0], None
    if len(active) > 1:
        return None, "multiple_active_subscriptions"
    return None, "no_active_subscription"


def _resolve_credential(
    row: CsvRow,
    subscriber_id: str,
    credentials: list[AccessCredential],
) -> tuple[AccessCredential | None, str | None]:
    username = row.username.lower()
    active_for_subscriber = [
        item
        for item in credentials
        if str(item.subscriber_id) == subscriber_id and item.is_active
    ]
    exact_for_subscriber = [
        item
        for item in credentials
        if str(item.subscriber_id) == subscriber_id
        and _clean(item.username).lower() == username
    ]
    if exact_for_subscriber:
        return exact_for_subscriber[0], None
    if active_for_subscriber:
        return None, "subscriber_has_conflicting_active_credential"
    return None, None


def _build_plan(
    row: CsvRow,
    subscribers: list[Subscriber],
    subscriptions: list[Subscription],
    credentials: list[AccessCredential],
    olts: list[OLTDevice],
    pon_ports_by_olt_id: dict[str, list[PonPort]],
    onts_by_serial: dict[str, OntUnit],
    active_assignments_by_ont_id: dict[str, OntAssignment],
) -> RowPlan:
    reasons: list[str] = []
    actions: list[str] = []

    if row.wan_mode.lower() != "pppoe":
        reasons.append(f"WAN mode is '{row.wan_mode or '-'}', not PPPoE")
    if not row.username or not row.password:
        reasons.append("Missing PPPoE username/password")

    subscriber, subscriber_match_reason = _resolve_subscriber(row, subscribers)
    if not subscriber:
        reasons.append(subscriber_match_reason or "Subscriber not found")

    subscription = None
    if subscriber:
        subscription, subscription_reason = _resolve_active_subscription(
            subscriptions, str(subscriber.id)
        )
        if not subscription:
            reasons.append(subscription_reason or "Active subscription not found")

    olt = _resolve_olt(row, olts)
    if not olt:
        reasons.append("OLT not found by exact active name")

    pon_port = None
    if olt:
        pon_port = _resolve_pon_port(row, pon_ports_by_olt_id.get(str(olt.id), []))
        if not pon_port:
            reasons.append(
                "PON port could not be resolved uniquely from OLT/board/port"
            )

    serial_key = _normalize_serial(row.serial_number)
    ont = onts_by_serial.get(serial_key)
    active_assignment = active_assignments_by_ont_id.get(str(ont.id)) if ont else None
    if ont and olt and ont.olt_device_id and str(ont.olt_device_id) != str(olt.id):
        reasons.append("Existing ONT is linked to a different OLT")

    if active_assignment and subscriber and subscription and pon_port:
        same_assignment = str(active_assignment.subscriber_id or "") == str(
            subscriber.id
        ) and str(active_assignment.pon_port_id) == str(pon_port.id)
        if not same_assignment:
            reasons.append("Existing active ONT assignment conflicts with CSV mapping")

    credential = None
    if subscriber:
        username_conflict = next(
            (
                item
                for item in credentials
                if _clean(item.username).lower() == row.username.lower()
                and str(item.subscriber_id) != str(subscriber.id)
            ),
            None,
        )
        if username_conflict:
            reasons.append("PPPoE username already belongs to another subscriber")
        else:
            credential, credential_reason = _resolve_credential(
                row,
                str(subscriber.id),
                credentials,
            )
            if credential_reason:
                reasons.append(credential_reason)

    if (
        subscription
        and _clean(subscription.login)
        and _clean(subscription.login).lower() != row.username.lower()
    ):
        reasons.append("Active subscription already has a different login")

    create_ont = ont is None and olt is not None and pon_port is not None
    if create_ont:
        actions.append("create_ont")
    if ont is not None:
        actions.append("update_ont")
    create_assignment = (
        active_assignment is None
        and subscriber is not None
        and subscription is not None
        and pon_port is not None
    )
    if create_assignment:
        actions.append("create_assignment")
    if credential is None and subscriber is not None:
        actions.append("create_credential")
    elif credential is not None:
        actions.append("update_credential")
    if subscription and not _clean(subscription.login):
        actions.append("set_subscription_login")

    can_apply = not reasons
    return RowPlan(
        row_number=row.row_number,
        serial_number=row.serial_number,
        username=row.username,
        olt_name=row.olt_name,
        subscriber_id=str(subscriber.id) if subscriber else None,
        subscriber_number=_clean(subscriber.subscriber_number) if subscriber else None,
        subscription_id=str(subscription.id) if subscription else None,
        ont_id=str(ont.id) if ont else None,
        pon_port_id=str(pon_port.id) if pon_port else None,
        assignment_id=str(active_assignment.id) if active_assignment else None,
        credential_id=str(credential.id) if credential else None,
        can_apply=can_apply,
        reasons=reasons,
        actions=actions,
        create_ont=create_ont,
        create_assignment=create_assignment,
        create_credential=credential is None and subscriber is not None,
        update_credential=credential is not None,
        update_subscription_login=bool(subscription and not _clean(subscription.login)),
        update_ont=bool(ont is not None or create_ont),
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _prepare(
    output_dir: Path, csv_path: Path, limit: int | None = None
) -> tuple[list[CsvRow], list[RowPlan], dict[int, list[str]]]:
    rows = _load_csv_rows(csv_path)
    candidate_rows, pre_skipped = _preselect_rows(rows)
    if limit:
        candidate_rows = candidate_rows[:limit]

    with SessionLocal() as db:
        subscribers = list(db.scalars(select(Subscriber)).all())
        subscriptions = list(db.scalars(select(Subscription)).all())
        credentials = list(db.scalars(select(AccessCredential)).all())
        olts = list(db.scalars(select(OLTDevice)).all())
        pon_ports = list(
            db.scalars(select(PonPort).where(PonPort.is_active.is_(True))).all()
        )
        onts = list(db.scalars(select(OntUnit)).all())
        active_assignments = list(
            db.scalars(
                select(OntAssignment).where(OntAssignment.active.is_(True))
            ).all()
        )

    pon_ports_by_olt_id: dict[str, list[PonPort]] = defaultdict(list)
    for pon_port in pon_ports:
        pon_ports_by_olt_id[str(pon_port.olt_id)].append(pon_port)

    onts_by_serial = {
        _normalize_serial(ont.serial_number): ont
        for ont in onts
        if _normalize_serial(ont.serial_number)
    }
    active_assignments_by_ont_id = {
        str(item.ont_unit_id): item for item in active_assignments
    }

    plans = [
        _build_plan(
            row,
            subscribers,
            subscriptions,
            credentials,
            olts,
            pon_ports_by_olt_id,
            onts_by_serial,
            active_assignments_by_ont_id,
        )
        for row in candidate_rows
    ]

    apply_rows: list[dict[str, object]] = []
    skip_rows: list[dict[str, object]] = []
    for plan in plans:
        target = apply_rows if plan.can_apply else skip_rows
        target.append(
            {
                "row_number": plan.row_number,
                "serial_number": plan.serial_number,
                "username": plan.username,
                "olt_name": plan.olt_name,
                "subscriber_id": plan.subscriber_id or "",
                "subscriber_number": plan.subscriber_number or "",
                "subscription_id": plan.subscription_id or "",
                "ont_id": plan.ont_id or "",
                "pon_port_id": plan.pon_port_id or "",
                "assignment_id": plan.assignment_id or "",
                "credential_id": plan.credential_id or "",
                "actions": ", ".join(plan.actions),
                "reasons": " | ".join(plan.reasons),
            }
        )
    for row_number, reasons in sorted(pre_skipped.items()):
        original = next((row for row in rows if row.row_number == row_number), None)
        skip_rows.append(
            {
                "row_number": row_number,
                "serial_number": original.serial_number if original else "",
                "username": original.username if original else "",
                "olt_name": original.olt_name if original else "",
                "subscriber_id": "",
                "subscriber_number": "",
                "subscription_id": "",
                "ont_id": "",
                "pon_port_id": "",
                "assignment_id": "",
                "credential_id": "",
                "actions": "",
                "reasons": " | ".join(reasons),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "applyable_rows.csv", apply_rows)
    _write_csv(output_dir / "skipped_rows.csv", skip_rows)
    (output_dir / "plan.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "csv_path": str(csv_path),
                "total_csv_rows": len(rows),
                "candidate_rows": len(candidate_rows),
                "applyable_rows": len([item for item in plans if item.can_apply]),
                "skipped_rows": len(skip_rows),
                "plans": [plan.__dict__ for plan in plans],
                "pre_skipped": {str(key): value for key, value in pre_skipped.items()},
            },
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    return candidate_rows, plans, pre_skipped


def _find_row(rows: list[CsvRow], row_number: int) -> CsvRow:
    for row in rows:
        if row.row_number == row_number:
            return row
    raise KeyError(f"CSV row {row_number} not found in prepared rows")


def _apply_rows(csv_rows: list[CsvRow], plans: list[RowPlan], output_dir: Path) -> None:
    rollback_entries: list[dict[str, Any]] = []
    applied_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []

    for plan in plans:
        if not plan.can_apply:
            skipped_rows.append(
                {
                    "row_number": plan.row_number,
                    "serial_number": plan.serial_number,
                    "username": plan.username,
                    "reasons": " | ".join(plan.reasons),
                }
            )
            continue

        row = _find_row(csv_rows, plan.row_number)
        db = SessionLocal()
        try:
            rollback_entry: dict[str, Any] = {
                "row_number": row.row_number,
                "serial_number": row.serial_number,
                "username": row.username,
                "subscriber_id": plan.subscriber_id,
                "subscription_id": plan.subscription_id,
                "created_ont_id": None,
                "created_assignment_id": None,
                "created_credential_id": None,
                "created_radius_user_ids": [],
                "subscription_before": None,
                "ont_before": None,
                "credential_before": None,
                "radius_user_before": None,
            }

            subscriber = db.get(Subscriber, plan.subscriber_id)
            subscription = db.get(Subscription, plan.subscription_id)
            pon_port = db.get(PonPort, plan.pon_port_id)
            olt = db.get(OLTDevice, pon_port.olt_id) if pon_port else None
            if not subscriber or not subscription or not pon_port or not olt:
                raise ValueError("Resolved entities could not be reloaded for apply")

            ont: OntUnit | None = None
            if plan.ont_id:
                ont = db.get(OntUnit, plan.ont_id)
            if ont is not None:
                rollback_entry["ont_before"] = _serialize_model(
                    ont,
                    [
                        "id",
                        "model",
                        "vendor",
                        "olt_device_id",
                        "pon_type",
                        "gpon_channel",
                        "board",
                        "port",
                        "name",
                        "address_or_comment",
                        "wan_mode",
                        "pppoe_username",
                        "pppoe_password",
                        "is_active",
                    ],
                )
            else:
                ont = OntUnit(
                    serial_number=row.serial_number,
                    model=row.model or None,
                    vendor=_derive_vendor(row.serial_number, olt),
                    is_active=True,
                    olt_device_id=olt.id,
                    pon_type=PonType.gpon if row.pon_type.lower() == "gpon" else None,
                    gpon_channel=GponChannel.gpon
                    if row.pon_type.lower() == "gpon"
                    else None,
                    board=f"0/{row.board}" if row.board else None,
                    port=row.port or None,
                    external_id=row.allocated_onu or None,
                    name=row.name or None,
                    address_or_comment=row.address or None,
                    online_status=OnuOnlineStatus.unknown,
                    wan_mode=WanMode.pppoe,
                    pppoe_username=row.username,
                    pppoe_password=encrypt_credential(row.password),
                )
                db.add(ont)
                db.flush()
                rollback_entry["created_ont_id"] = str(ont.id)

            ont.model = row.model or ont.model
            ont.vendor = _derive_vendor(row.serial_number, olt) or ont.vendor
            ont.olt_device_id = olt.id
            ont.pon_type = (
                PonType.gpon if row.pon_type.lower() == "gpon" else ont.pon_type
            )
            ont.gpon_channel = (
                GponChannel.gpon if row.pon_type.lower() == "gpon" else ont.gpon_channel
            )
            ont.board = f"0/{row.board}" if row.board else ont.board
            ont.port = row.port or ont.port
            ont.name = row.name or ont.name
            ont.address_or_comment = row.address or ont.address_or_comment
            ont.wan_mode = WanMode.pppoe
            ont.pppoe_username = row.username
            ont.pppoe_password = encrypt_credential(row.password)
            ont.is_active = True

            active_assignment = db.scalars(
                select(OntAssignment)
                .where(OntAssignment.ont_unit_id == ont.id)
                .where(OntAssignment.active.is_(True))
                .limit(1)
            ).first()
            if active_assignment is None:
                assignment = OntAssignment(
                    ont_unit_id=ont.id,
                    pon_port_id=pon_port.id,
                    subscriber_id=subscriber.id,
                    subscription_id=subscription.id,
                    assigned_at=datetime.now(UTC),
                    active=True,
                    notes="Imported from SmartOLT CSV",
                )
                db.add(assignment)
                db.flush()
                rollback_entry["created_assignment_id"] = str(assignment.id)

            credential = None
            if plan.credential_id:
                credential = db.get(AccessCredential, plan.credential_id)
            if credential is not None:
                rollback_entry["credential_before"] = _serialize_model(
                    credential,
                    [
                        "id",
                        "subscriber_id",
                        "username",
                        "secret_hash",
                        "is_active",
                        "radius_profile_id",
                    ],
                )
                existing_radius_user = db.scalars(
                    select(RadiusUser)
                    .where(RadiusUser.access_credential_id == credential.id)
                    .limit(1)
                ).first()
                if existing_radius_user:
                    rollback_entry["radius_user_before"] = _serialize_model(
                        existing_radius_user,
                        [
                            "id",
                            "subscriber_id",
                            "subscription_id",
                            "access_credential_id",
                            "username",
                            "secret_hash",
                            "radius_profile_id",
                            "is_active",
                        ],
                    )
                credential.username = row.username
                credential.secret_hash = encrypt_credential(row.password)
                credential.is_active = True
                if not credential.radius_profile_id and subscription.radius_profile_id:
                    credential.radius_profile_id = subscription.radius_profile_id
            else:
                credential = AccessCredential(
                    subscriber_id=subscriber.id,
                    username=row.username,
                    secret_hash=encrypt_credential(row.password),
                    is_active=True,
                    radius_profile_id=subscription.radius_profile_id,
                )
                db.add(credential)
                db.flush()
                rollback_entry["created_credential_id"] = str(credential.id)

            if not _clean(subscription.login):
                rollback_entry["subscription_before"] = {
                    "id": str(subscription.id),
                    "login": subscription.login,
                }
                subscription.login = row.username

            pre_radius_users = {
                str(item.id)
                for item in db.scalars(
                    select(RadiusUser).where(
                        RadiusUser.subscription_id == subscription.id
                    )
                ).all()
            }
            ensure_radius_users_for_subscription(db, subscription)
            db.commit()

            post_radius_users = {
                str(item.id)
                for item in db.scalars(
                    select(RadiusUser).where(
                        RadiusUser.subscription_id == subscription.id
                    )
                ).all()
            }
            rollback_entry["created_radius_user_ids"] = sorted(
                post_radius_users - pre_radius_users
            )
            rollback_entries.append(rollback_entry)
            applied_rows.append(
                {
                    "row_number": plan.row_number,
                    "serial_number": plan.serial_number,
                    "username": plan.username,
                    "subscriber_id": plan.subscriber_id,
                    "subscription_id": plan.subscription_id,
                    "ont_id": str(ont.id),
                    "created_ont": bool(rollback_entry["created_ont_id"]),
                    "created_assignment": bool(rollback_entry["created_assignment_id"]),
                    "created_credential": bool(rollback_entry["created_credential_id"]),
                }
            )
            logger.info(
                "Applied row %s serial=%s subscriber=%s",
                plan.row_number,
                plan.serial_number,
                plan.subscriber_number or plan.subscriber_id,
            )
        except Exception as exc:
            db.rollback()
            skipped_rows.append(
                {
                    "row_number": plan.row_number,
                    "serial_number": plan.serial_number,
                    "username": plan.username,
                    "reasons": str(exc),
                }
            )
            logger.warning("Skipped row %s during apply: %s", plan.row_number, exc)
        finally:
            db.close()

    rollback_path = output_dir / "rollback.json"
    rollback_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "entries": rollback_entries,
            },
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    _write_csv(output_dir / "applied_rows.csv", applied_rows)
    _write_csv(output_dir / "apply_runtime_skipped_rows.csv", skipped_rows)
    logger.info("Wrote rollback file: %s", rollback_path)


def _rollback(rollback_path: Path) -> None:
    payload = json.loads(rollback_path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    for entry in reversed(entries):
        db = SessionLocal()
        try:
            for radius_user_id in entry.get("created_radius_user_ids", []):
                radius_user = db.get(RadiusUser, radius_user_id)
                if radius_user:
                    db.delete(radius_user)

            radius_user_before = entry.get("radius_user_before")
            if radius_user_before:
                radius_user = db.get(RadiusUser, radius_user_before["id"])
                if radius_user:
                    radius_user.subscriber_id = radius_user_before["subscriber_id"]
                    radius_user.subscription_id = radius_user_before["subscription_id"]
                    radius_user.access_credential_id = radius_user_before[
                        "access_credential_id"
                    ]
                    radius_user.username = radius_user_before["username"]
                    radius_user.secret_hash = radius_user_before["secret_hash"]
                    radius_user.radius_profile_id = radius_user_before[
                        "radius_profile_id"
                    ]
                    radius_user.is_active = radius_user_before["is_active"]

            created_assignment_id = entry.get("created_assignment_id")
            if created_assignment_id:
                assignment = db.get(OntAssignment, created_assignment_id)
                if assignment:
                    db.delete(assignment)

            created_credential_id = entry.get("created_credential_id")
            if created_credential_id:
                credential = db.get(AccessCredential, created_credential_id)
                if credential:
                    radius_user = db.scalars(
                        select(RadiusUser)
                        .where(RadiusUser.access_credential_id == credential.id)
                        .limit(1)
                    ).first()
                    if radius_user:
                        db.delete(radius_user)
                    db.delete(credential)

            credential_before = entry.get("credential_before")
            if credential_before:
                credential = db.get(AccessCredential, credential_before["id"])
                if credential:
                    credential.subscriber_id = credential_before["subscriber_id"]
                    credential.username = credential_before["username"]
                    credential.secret_hash = credential_before["secret_hash"]
                    credential.is_active = credential_before["is_active"]
                    credential.radius_profile_id = credential_before[
                        "radius_profile_id"
                    ]

            subscription_before = entry.get("subscription_before")
            if subscription_before:
                subscription = db.get(Subscription, subscription_before["id"])
                if subscription:
                    subscription.login = subscription_before["login"]

            ont_before = entry.get("ont_before")
            if ont_before:
                ont = db.get(OntUnit, ont_before["id"])
                if ont:
                    ont.model = ont_before["model"]
                    ont.vendor = ont_before["vendor"]
                    ont.olt_device_id = ont_before["olt_device_id"]
                    ont.pon_type = _restore_enum(PonType, ont_before["pon_type"])
                    ont.gpon_channel = _restore_enum(
                        GponChannel, ont_before["gpon_channel"]
                    )
                    ont.board = ont_before["board"]
                    ont.port = ont_before["port"]
                    ont.name = ont_before["name"]
                    ont.address_or_comment = ont_before["address_or_comment"]
                    ont.wan_mode = _restore_enum(WanMode, ont_before["wan_mode"])
                    ont.pppoe_username = ont_before["pppoe_username"]
                    ont.pppoe_password = ont_before["pppoe_password"]
                    ont.is_active = ont_before["is_active"]

            created_ont_id = entry.get("created_ont_id")
            if created_ont_id:
                ont = db.get(OntUnit, created_ont_id)
                if ont:
                    db.delete(ont)

            subscription_id = entry.get("subscription_id")
            if subscription_id:
                subscription = db.get(Subscription, subscription_id)
                if subscription:
                    ensure_radius_users_for_subscription(db, subscription)

            db.commit()
            logger.info(
                "Rolled back row %s serial=%s",
                entry.get("row_number"),
                entry.get("serial_number"),
            )
        except Exception as exc:
            db.rollback()
            logger.error(
                "Rollback failed for row %s serial=%s: %s",
                entry.get("row_number"),
                entry.get("serial_number"),
                exc,
            )
            raise
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import SmartOLT unconfigured ONU rows safely"
    )
    parser.add_argument(
        "--csv-path",
        default=DEFAULT_CSV,
        help="Path to SmartOLT CSV export",
    )
    parser.add_argument(
        "--output-dir",
        default=f"tmp/smartolt_import_{_utc_stamp()}",
        help="Directory for dry-run/apply artifacts",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit candidate rows processed"
    )
    parser.add_argument("--apply", action="store_true", help="Apply safe rows")
    parser.add_argument(
        "--rollback", default=None, help="Rollback from a previous rollback.json"
    )
    args = parser.parse_args()

    if args.apply and args.rollback:
        raise SystemExit("Use either --apply or --rollback, not both.")

    if args.rollback:
        _rollback(Path(args.rollback))
        return

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    output_dir = Path(args.output_dir)
    csv_rows, plans, _pre_skipped = _prepare(output_dir, csv_path, args.limit)

    safe_count = len([item for item in plans if item.can_apply])
    logger.info(
        "Prepared plan: %d safe row(s), %d skipped row(s)",
        safe_count,
        len(csv_rows) - safe_count,
    )

    if args.apply:
        _apply_rows(csv_rows, plans, output_dir)
    else:
        logger.info("Dry run complete. Review artifacts in %s", output_dir)


if __name__ == "__main__":
    main()
