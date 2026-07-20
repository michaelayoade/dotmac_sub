from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscription_engine import SettingValueType
from app.models.support import TicketStatus
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service

STATUS_OPTIONS_KEY = "support_ticket_status_options"
PRIORITY_OPTIONS_KEY = "support_ticket_priority_options"
TYPE_OPTIONS_KEY = "support_ticket_type_options"
SERVICE_TEAMS_KEY = "support_service_teams"
REGION_OPTIONS_KEY = "support_ticket_region_options"
AUTO_ASSIGN_ENABLED_KEY = "support_ticket_auto_assign_enabled"
AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY = "support_ticket_auto_assign_max_open_tickets"
REGION_ASSIGNMENT_RULES_KEY = "support_region_assignment_rules"
SERVICE_TEAM_MEMBERS_KEY = "support_service_team_members"
SLA_POLICY_KEY = "support_ticket_sla_policy"
SETTINGS_DOMAIN = SettingDomain.workflow

DEFAULT_STATUS_OPTIONS = [status.value for status in TicketStatus]
VALID_STATUS_OPTIONS = frozenset(DEFAULT_STATUS_OPTIONS)
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
DEFAULT_SLA_POLICY = {
    "urgent": {"response_hours": 1, "resolution_hours": 8, "aging_hours": 4},
    "high": {"response_hours": 4, "resolution_hours": 24, "aging_hours": 12},
    "normal": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "medium": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "low": {"response_hours": 24, "resolution_hours": 120, "aging_hours": 48},
    "lower": {"response_hours": 24, "resolution_hours": 168, "aging_hours": 72},
}
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


def normalize_ticket_status(value: str) -> str:
    """Keep configured choices inside the lifecycle owner's vocabulary."""
    normalized = normalize_system_value(value)
    return normalized if normalized in VALID_STATUS_OPTIONS else ""


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


def _write_optional_int(db: Session, *, key: str, value: int | None) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.integer
        if value is not None
        else SettingValueType.string,
        value_text=str(value) if value is not None else "",
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


def _normalize_optional_non_negative_int(value: object | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _normalize_non_negative_int(value)


def _team_type_for_label(label: str) -> str:
    normalized = normalize_system_value(label)
    if "field" in normalized:
        return ServiceTeamType.field_service.value
    if "operation" in normalized or "ops" in normalized:
        return ServiceTeamType.operations.value
    return ServiceTeamType.support.value


def _sync_service_team_tables(
    db: Session,
    *,
    teams: list[dict[str, str]],
    members: dict[str, list[str]] | None = None,
) -> None:
    """Mirror settings-page teams into the native assignment tables.

    The old settings payload remains the display/config source for the existing
    support UI, while the CRM-style assignment engine reads the real
    ``service_teams`` and ``service_team_members`` tables. Keeping both in sync
    lets admins configure rules before ticket write cutover.
    """
    member_map = members or {}
    for item in teams:
        team_id = _normalize_uuid(item.get("id"))
        label = str(item.get("label") or "").strip()
        if not team_id or not label:
            continue
        team = db.get(ServiceTeam, UUID(team_id))
        if team is None:
            team = ServiceTeam(
                id=UUID(team_id),
                name=label,
                team_type=_team_type_for_label(label),
                is_active=True,
            )
            db.add(team)
        else:
            team.name = label
            team.team_type = team.team_type or _team_type_for_label(label)
            team.is_active = True

        configured_members = {
            UUID(member_id)
            for member_id in member_map.get(team_id, [])
            if _normalize_uuid(member_id)
        }
        existing_members = (
            db.query(ServiceTeamMember)
            .filter(ServiceTeamMember.team_id == UUID(team_id))
            .all()
        )
        by_person = {row.person_id: row for row in existing_members}
        for row in existing_members:
            row.is_active = row.person_id in configured_members
        for person_id in configured_members:
            member_row = by_person.get(person_id)
            if member_row is None:
                db.add(
                    ServiceTeamMember(
                        team_id=UUID(team_id),
                        person_id=person_id,
                        is_active=True,
                    )
                )
            else:
                member_row.is_active = True


def list_assignment_rules(db: Session) -> list[dict[str, Any]]:
    team_lookup = {str(team.id): team.name for team in db.query(ServiceTeam).all()}
    rows = (
        db.query(TicketAssignmentRule)
        .order_by(
            TicketAssignmentRule.priority.desc(), TicketAssignmentRule.created_at.asc()
        )
        .all()
    )
    rules: list[dict[str, Any]] = []
    for rule in rows:
        config = rule.match_config if isinstance(rule.match_config, dict) else {}
        team_id = str(rule.team_id) if rule.team_id else ""
        rules.append(
            {
                "id": str(rule.id),
                "name": rule.name,
                "priority": int(rule.priority or 0),
                "is_active": bool(rule.is_active),
                "strategy": str(
                    rule.strategy or TicketAssignmentStrategy.round_robin.value
                ),
                "team_id": team_id,
                "team_label": team_lookup.get(team_id, team_id),
                "assignment_target": str(
                    config.get("assignment_target") or "technician"
                ),
                "assignee_person_id": str(config.get("assignee_person_id") or ""),
                "ticket_types": config.get("ticket_types")
                if isinstance(config.get("ticket_types"), list)
                else [],
                "regions": config.get("regions")
                if isinstance(config.get("regions"), list)
                else [],
            }
        )
    return rules


def create_assignment_rule(
    db: Session,
    *,
    name: str,
    priority: object,
    strategy: str,
    team_id: str | None,
    ticket_types: list[str],
    regions: list[str],
    assignee_person_id: str | None,
    assignment_target: str,
    is_active: bool,
) -> TicketAssignmentRule:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Assignment rule name is required.")
    clean_strategy = str(strategy or TicketAssignmentStrategy.round_robin.value).strip()
    if clean_strategy not in {item.value for item in TicketAssignmentStrategy}:
        raise ValueError("Assignment strategy is invalid.")
    clean_team_id = _normalize_uuid(team_id)
    clean_assignee_id = _normalize_uuid(assignee_person_id)
    config: dict[str, Any] = {"entity_types": ["ticket"]}
    normalized_ticket_types = [
        str(item).strip() for item in ticket_types if str(item).strip()
    ]
    normalized_regions = [
        normalize_system_value(str(item)) for item in regions if str(item).strip()
    ]
    if normalized_ticket_types:
        config["ticket_types"] = normalized_ticket_types
    if normalized_regions:
        config["regions"] = normalized_regions
    if clean_assignee_id:
        config["assignee_person_id"] = clean_assignee_id
        config["assignment_target"] = (
            str(assignment_target or "technician").strip() or "technician"
        )

    rule = TicketAssignmentRule(
        name=clean_name,
        priority=_normalize_non_negative_int(priority),
        is_active=is_active,
        match_config=config,
        strategy=clean_strategy,
        team_id=UUID(clean_team_id) if clean_team_id else None,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def delete_assignment_rule(db: Session, rule_id: str) -> None:
    clean_id = _normalize_uuid(rule_id)
    if not clean_id:
        raise ValueError("Assignment rule ID is invalid.")
    rule = db.get(TicketAssignmentRule, UUID(clean_id))
    if rule is None:
        raise ValueError("Assignment rule not found.")
    db.delete(rule)
    db.commit()


def list_status_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=STATUS_OPTIONS_KEY,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_ticket_status,
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


def auto_assign_max_open_tickets(db: Session) -> int | None:
    raw = _read_raw_setting(db, AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


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
    auto_assign_max_open_tickets: str | int | None = None,
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
) -> None:
    requested_statuses = _normalize_list(
        statuses,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )
    invalid_statuses = [
        status for status in requested_statuses if status not in VALID_STATUS_OPTIONS
    ]
    if invalid_statuses:
        unsupported = ", ".join(invalid_statuses)
        raise ValueError(f"Unsupported ticket status: {unsupported}")
    normalized_statuses = requested_statuses
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
    else:
        teams = list_service_teams(db)
    if auto_assign is not None:
        _write_bool(db, key=AUTO_ASSIGN_ENABLED_KEY, value=auto_assign)
    if auto_assign_max_open_tickets is not None:
        value = _normalize_optional_non_negative_int(auto_assign_max_open_tickets)
        _write_optional_int(db, key=AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY, value=value)
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
    else:
        members = service_team_members(db)
    _sync_service_team_tables(db, teams=teams, members=members)
    db.commit()
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
