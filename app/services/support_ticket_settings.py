from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service

STATUS_OPTIONS_KEY = "support_ticket_status_options"
PRIORITY_OPTIONS_KEY = "support_ticket_priority_options"
TYPE_OPTIONS_KEY = "support_ticket_type_options"
STATUS_COLORS_KEY = "support_ticket_status_colors"
SERVICE_TEAMS_KEY = "support_service_teams"
REGION_OPTIONS_KEY = "support_ticket_region_options"
AUTO_ASSIGN_ENABLED_KEY = "support_ticket_auto_assign_enabled"
REGION_ASSIGNMENT_RULES_KEY = "support_region_assignment_rules"
SERVICE_TEAM_MEMBERS_KEY = "support_service_team_members"
SLA_POLICY_KEY = "support_ticket_sla_policy"
SETTINGS_DOMAIN = SettingDomain.workflow

DEFAULT_STATUS_OPTIONS = [
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "resolved",
    "closed",
    "canceled",
    "merged",
]
DEFAULT_PRIORITY_OPTIONS = [
    "lower",
    "low",
    "medium",
    "normal",
    "high",
    "urgent",
]
DEFAULT_TYPE_OPTIONS = [
    "incident",
    "request",
    "change",
    "maintenance",
    "outage",
]
DEFAULT_REGION_OPTIONS = ["north", "south", "east", "west", "central"]
DEFAULT_STATUS_COLORS = {
    "new": "blue",
    "open": "emerald",
    "pending": "amber",
    "waiting_on_customer": "amber",
    "lastmile_rerun": "amber",
    "site_under_construction": "amber",
    "on_hold": "orange",
    "resolved": "teal",
    "closed": "slate",
    "canceled": "red",
    "merged": "violet",
}
DEFAULT_SLA_POLICY = {
    "urgent": {"response_hours": 1, "resolution_hours": 8, "aging_hours": 4},
    "high": {"response_hours": 4, "resolution_hours": 24, "aging_hours": 12},
    "normal": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "medium": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "low": {"response_hours": 24, "resolution_hours": 120, "aging_hours": 48},
    "lower": {"response_hours": 24, "resolution_hours": 168, "aging_hours": 72},
}
STATUS_COLOR_VARIANTS = [
    "slate",
    "blue",
    "emerald",
    "amber",
    "orange",
    "teal",
    "red",
    "violet",
]
TERMINAL_STATUSES = {"resolved", "closed", "canceled", "merged"}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _settings_service():
    service = getattr(domain_settings_service, "workflow_settings", None)
    if service is not None:
        return service
    return domain_settings_service.settings


def display_label(value: str) -> str:
    text = str(value or "").strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in text.split()) or "-"


def normalize_system_value(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _NON_ALNUM_RE.sub("_", text)
    return text.strip("_")


def _normalize_list(
    raw: Any,
    *,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    values = raw if isinstance(raw, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if normalizer is not None:
            text = normalizer(text)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized or list(defaults)


def _read_list(
    db: Session,
    *,
    key: str,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    service = _settings_service()
    try:
        setting = (
            service.get_by_key(db, key)
            if getattr(service, "domain", None) is not None
            else domain_settings_service.settings.get_by_key(db, key)
        )
    except Exception:
        setting = None
    raw = getattr(setting, "value_json", None)
    return _normalize_list(raw, defaults=defaults, normalizer=normalizer)


def _write_list(
    db: Session,
    *,
    key: str,
    values: list[str],
) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.json,
        value_text=None,
        value_json=list(values),
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _read_raw_setting(db: Session, key: str) -> Any:
    service = _settings_service()
    try:
        setting = (
            service.get_by_key(db, key)
            if getattr(service, "domain", None) is not None
            else domain_settings_service.settings.get_by_key(db, key)
        )
    except Exception:
        return None
    if setting is None:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def _write_json(db: Session, *, key: str, value: Any) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.json,
        value_text=None,
        value_json=value,
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _write_bool(db: Session, *, key: str, value: bool) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.boolean,
        value_text="true" if value else "false",
        value_json=value,
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _normalize_uuid(
    value: object | None, *, allow_generate: bool = False
) -> str | None:
    text = str(value or "").strip()
    if not text and allow_generate:
        return str(uuid4())
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        raise ValueError(f"{text!r} is not a valid UUID")


def _normalize_non_negative_int(value: object | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{text!r} must be a whole number") from exc
    return max(parsed, 0)


def list_status_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=STATUS_OPTIONS_KEY,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_priority_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=PRIORITY_OPTIONS_KEY,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_ticket_type_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=TYPE_OPTIONS_KEY,
        defaults=DEFAULT_TYPE_OPTIONS,
    )


def list_region_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=REGION_OPTIONS_KEY,
        defaults=DEFAULT_REGION_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_service_teams(db: Session) -> list[dict[str, str]]:
    raw = _read_raw_setting(db, SERVICE_TEAMS_KEY)
    teams = raw if isinstance(raw, list) else []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in teams:
        if not isinstance(item, dict):
            continue
        team_id = _normalize_uuid(item.get("id"))
        label = str(item.get("label") or "").strip()
        if not team_id or not label or team_id in seen:
            continue
        seen.add(team_id)
        normalized.append({"id": team_id, "label": label})
    return normalized


def status_color_options(db: Session) -> dict[str, str]:
    raw = _read_raw_setting(db, STATUS_COLORS_KEY)
    configured = raw if isinstance(raw, dict) else {}
    colors: dict[str, str] = {}
    for status in list_status_options(db):
        color = str(
            configured.get(status) or DEFAULT_STATUS_COLORS.get(status) or "slate"
        )
        colors[status] = color if color in STATUS_COLOR_VARIANTS else "slate"
    return colors


def auto_assign_enabled(db: Session) -> bool:
    raw = _read_raw_setting(db, AUTO_ASSIGN_ENABLED_KEY)
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return True


def region_assignment_rules(db: Session) -> dict[str, dict[str, Any]]:
    raw = _read_raw_setting(db, REGION_ASSIGNMENT_RULES_KEY)
    rules = raw if isinstance(raw, dict) else {}
    normalized: dict[str, dict[str, Any]] = {}
    for region, rule in rules.items():
        key = normalize_system_value(str(region))
        if not key or not isinstance(rule, dict):
            continue
        raw_assignees = rule.get("assignee_person_ids")
        assignee_values = raw_assignees if isinstance(raw_assignees, list) else []
        normalized[key] = {
            "ticket_manager_person_id": _normalize_uuid(
                rule.get("ticket_manager_person_id")
            ),
            "site_coordinator_person_id": _normalize_uuid(
                rule.get("site_coordinator_person_id")
            ),
            "technician_person_id": _normalize_uuid(rule.get("technician_person_id")),
            "service_team_id": _normalize_uuid(rule.get("service_team_id")),
            "assignee_person_ids": [
                uid
                for uid in (_normalize_uuid(item) for item in assignee_values)
                if uid
            ],
        }
    return normalized


def service_team_members(db: Session) -> dict[str, list[str]]:
    raw = _read_raw_setting(db, SERVICE_TEAM_MEMBERS_KEY)
    teams = raw if isinstance(raw, dict) else {}
    normalized: dict[str, list[str]] = {}
    for team_id, members in teams.items():
        uid = _normalize_uuid(team_id)
        if not uid:
            continue
        values = members if isinstance(members, list) else []
        normalized[uid] = [
            member for member in (_normalize_uuid(item) for item in values) if member
        ]
    return normalized


def sla_policy(db: Session) -> dict[str, dict[str, int]]:
    raw = _read_raw_setting(db, SLA_POLICY_KEY)
    configured = raw if isinstance(raw, dict) else {}
    priorities = list_priority_options(db)
    policy: dict[str, dict[str, int]] = {}
    for priority in priorities:
        source = configured.get(priority)
        if not isinstance(source, dict):
            source = DEFAULT_SLA_POLICY.get(priority, {})
        policy[priority] = {
            "response_hours": _normalize_non_negative_int(source.get("response_hours")),
            "resolution_hours": _normalize_non_negative_int(
                source.get("resolution_hours")
            ),
            "aging_hours": _normalize_non_negative_int(source.get("aging_hours")),
        }
    return policy


def update_options(
    db: Session,
    *,
    statuses: list[str],
    priorities: list[str],
    ticket_types: list[str],
    regions: list[str] | None = None,
    service_team_ids: list[str] | None = None,
    service_team_labels: list[str] | None = None,
    auto_assign: bool | None = None,
    routing_regions: list[str] | None = None,
    routing_ticket_manager_person_ids: list[str] | None = None,
    routing_site_coordinator_person_ids: list[str] | None = None,
    routing_technician_person_ids: list[str] | None = None,
    routing_service_team_ids: list[str] | None = None,
    routing_assignee_person_ids: list[str] | None = None,
    team_member_team_ids: list[str] | None = None,
    team_member_person_ids: list[str] | None = None,
    sla_priorities: list[str] | None = None,
    sla_response_hours: list[str] | None = None,
    sla_resolution_hours: list[str] | None = None,
    sla_aging_hours: list[str] | None = None,
    status_color_statuses: list[str] | None = None,
    status_color_values: list[str] | None = None,
) -> None:
    normalized_statuses = _normalize_list(
        statuses,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_priorities = _normalize_list(
        priorities,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_types = _normalize_list(
        ticket_types,
        defaults=DEFAULT_TYPE_OPTIONS,
    )
    _write_list(db, key=STATUS_OPTIONS_KEY, values=normalized_statuses)
    _write_list(db, key=PRIORITY_OPTIONS_KEY, values=normalized_priorities)
    _write_list(db, key=TYPE_OPTIONS_KEY, values=normalized_types)
    if regions is not None:
        _write_list(
            db,
            key=REGION_OPTIONS_KEY,
            values=_normalize_list(
                regions,
                defaults=DEFAULT_REGION_OPTIONS,
                normalizer=normalize_system_value,
            ),
        )
    if service_team_labels is not None:
        teams: list[dict[str, str]] = []
        seen: set[str] = set()
        ids = service_team_ids or []
        for index, label_raw in enumerate(service_team_labels):
            label = str(label_raw or "").strip()
            if not label:
                continue
            team_id = _normalize_uuid(
                ids[index] if index < len(ids) else None,
                allow_generate=True,
            )
            if not team_id or team_id in seen:
                continue
            seen.add(team_id)
            teams.append({"id": team_id, "label": label})
        _write_json(db, key=SERVICE_TEAMS_KEY, value=teams)
    if auto_assign is not None:
        _write_bool(db, key=AUTO_ASSIGN_ENABLED_KEY, value=auto_assign)
    if routing_regions is not None:

        def indexed(values: list[str] | None, index: int) -> str | None:
            return values[index] if values and index < len(values) else None

        rules: dict[str, dict[str, Any]] = {}
        for index, region_raw in enumerate(routing_regions):
            region = normalize_system_value(region_raw)
            if not region:
                continue
            assignee_raw = indexed(routing_assignee_person_ids, index) or ""
            assignees = [
                uid
                for uid in (
                    _normalize_uuid(item.strip())
                    for item in str(assignee_raw or "").split(",")
                    if item.strip()
                )
                if uid
            ]
            rules[region] = {
                "ticket_manager_person_id": _normalize_uuid(
                    indexed(routing_ticket_manager_person_ids, index)
                ),
                "site_coordinator_person_id": _normalize_uuid(
                    indexed(routing_site_coordinator_person_ids, index)
                ),
                "technician_person_id": _normalize_uuid(
                    indexed(routing_technician_person_ids, index)
                ),
                "service_team_id": _normalize_uuid(
                    indexed(routing_service_team_ids, index)
                ),
                "assignee_person_ids": assignees,
            }
        _write_json(db, key=REGION_ASSIGNMENT_RULES_KEY, value=rules)
    if team_member_team_ids is not None:
        members: dict[str, list[str]] = {}
        person_ids = team_member_person_ids or []
        for index, team_raw in enumerate(team_member_team_ids):
            team_id = _normalize_uuid(team_raw)
            person_id = _normalize_uuid(
                person_ids[index] if index < len(person_ids) else None
            )
            if not team_id or not person_id:
                continue
            members.setdefault(team_id, [])
            if person_id not in members[team_id]:
                members[team_id].append(person_id)
        _write_json(db, key=SERVICE_TEAM_MEMBERS_KEY, value=members)
    if sla_priorities is not None:
        policy: dict[str, dict[str, int]] = {}
        for index, priority_raw in enumerate(sla_priorities):
            priority = normalize_system_value(priority_raw)
            if not priority:
                continue
            policy[priority] = {
                "response_hours": _normalize_non_negative_int(
                    sla_response_hours[index]
                    if sla_response_hours and index < len(sla_response_hours)
                    else None
                ),
                "resolution_hours": _normalize_non_negative_int(
                    sla_resolution_hours[index]
                    if sla_resolution_hours and index < len(sla_resolution_hours)
                    else None
                ),
                "aging_hours": _normalize_non_negative_int(
                    sla_aging_hours[index]
                    if sla_aging_hours and index < len(sla_aging_hours)
                    else None
                ),
            }
        _write_json(db, key=SLA_POLICY_KEY, value=policy)
    if status_color_statuses is not None:
        colors: dict[str, str] = {}
        color_values = status_color_values or []
        for index, status_raw in enumerate(status_color_statuses):
            status = normalize_system_value(status_raw)
            color = (
                str(color_values[index] or "").strip()
                if index < len(color_values)
                else ""
            )
            if status:
                colors[status] = color if color in STATUS_COLOR_VARIANTS else "slate"
        _write_json(db, key=STATUS_COLORS_KEY, value=colors)


def default_status(db: Session) -> str:
    options = list_status_options(db)
    return "open" if "open" in options else options[0]


def default_priority(db: Session) -> str:
    options = list_priority_options(db)
    return "normal" if "normal" in options else options[0]


def status_is_terminal(value: str | None) -> bool:
    return str(value or "").strip() in TERMINAL_STATUSES


def status_is_merged(value: str | None) -> bool:
    return str(value or "").strip() == "merged"


def status_color(value: str) -> str:
    return DEFAULT_STATUS_COLORS.get(str(value or "").strip(), "slate")
