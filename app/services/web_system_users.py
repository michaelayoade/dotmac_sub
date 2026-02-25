"""Service helpers for admin system user listing/statistics."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, UserCredential
from app.models.rbac import Role
from app.models.rbac import SubscriberRole as SubscriberRoleModel
from app.models.subscriber import Subscriber, UserType
from app.services.dynamic_filters import (
    DEFAULT_OPERATORS_BY_TYPE,
    OPERATOR_LABELS,
    FilterCondition,
    FilterFieldSpec,
    FilterQuery,
    FilterValidationError,
    build_filter_expression,
    build_sort_clause,
    parse_filter_payload,
)


USER_TYPE_OPTIONS = [
    ("system_user", "System User"),
    ("customer", "Customer"),
    ("reseller", "Reseller"),
]
USER_TYPE_LABELS = {key: label for key, label in USER_TYPE_OPTIONS}

USER_DOCTYPE = "User"


def _pending_credential_expression():
    active_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
    )
    pending_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
        .where(UserCredential.must_change_password.is_(True))
    )
    return or_(~active_credential, pending_credential)


def _parse_uuid_value(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FilterValidationError("Expected a valid UUID value") from exc


def _parse_uuid_list(value: object) -> list[UUID]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        raise FilterValidationError("Expected an array value")
    if not items:
        raise FilterValidationError("Array filter value cannot be empty")
    return [_parse_uuid_value(item) for item in items]


def _role_filter_expression(operator: str, value: object):
    if operator == "is":
        token = str(value).strip().lower() if value is not None else ""
        if token in {"", "null", "none", "nil"}:
            return ~exists(
                select(SubscriberRoleModel.id).where(
                    SubscriberRoleModel.subscriber_id == Subscriber.id
                )
            )
        raise FilterValidationError("Role field supports only 'is null'")

    if operator == "is not":
        token = str(value).strip().lower() if value is not None else ""
        if token in {"", "null", "none", "nil"}:
            return exists(
                select(SubscriberRoleModel.id).where(
                    SubscriberRoleModel.subscriber_id == Subscriber.id
                )
            )
        raise FilterValidationError("Role field supports only 'is not null'")

    if operator == "=":
        role_id = _parse_uuid_value(value)
        return exists(
            select(SubscriberRoleModel.id)
            .where(SubscriberRoleModel.subscriber_id == Subscriber.id)
            .where(SubscriberRoleModel.role_id == role_id)
        )

    if operator == "!=":
        role_id = _parse_uuid_value(value)
        return ~exists(
            select(SubscriberRoleModel.id)
            .where(SubscriberRoleModel.subscriber_id == Subscriber.id)
            .where(SubscriberRoleModel.role_id == role_id)
        )

    if operator == "in":
        role_ids = _parse_uuid_list(value)
        return exists(
            select(SubscriberRoleModel.id)
            .where(SubscriberRoleModel.subscriber_id == Subscriber.id)
            .where(SubscriberRoleModel.role_id.in_(role_ids))
        )

    if operator == "not in":
        role_ids = _parse_uuid_list(value)
        return ~exists(
            select(SubscriberRoleModel.id)
            .where(SubscriberRoleModel.subscriber_id == Subscriber.id)
            .where(SubscriberRoleModel.role_id.in_(role_ids))
        )

    raise FilterValidationError(f"Operator '{operator}' is not allowed for role_id")


def _status_filter_expression(operator: str, value: object):
    status_value = str(value).strip().lower()
    if status_value not in {"active", "inactive", "pending"}:
        raise FilterValidationError("Status must be one of: active, inactive, pending")

    if status_value == "active":
        expr = Subscriber.is_active.is_(True)
    elif status_value == "inactive":
        expr = Subscriber.is_active.is_(False)
    else:
        expr = _pending_credential_expression()

    if operator == "=":
        return expr
    if operator == "!=":
        return ~expr
    raise FilterValidationError("Status field supports only '=' and '!='")


def _mfa_filter_expression(operator: str, value: object):
    mfa_exists = exists(
        select(MFAMethod.id)
        .where(MFAMethod.subscriber_id == Subscriber.id)
        .where(MFAMethod.enabled.is_(True))
        .where(MFAMethod.is_active.is_(True))
    )

    value_text = str(value).strip().lower() if value is not None else ""
    if value_text in {"true", "1", "yes", "on"}:
        target = mfa_exists
    elif value_text in {"false", "0", "no", "off"}:
        target = ~mfa_exists
    else:
        raise FilterValidationError("mfa_enabled expects a boolean value")

    if operator == "=":
        return target
    if operator == "!=":
        return ~target
    if operator == "is":
        return target
    if operator == "is not":
        return ~target

    raise FilterValidationError("mfa_enabled supports '=', '!=', 'is', and 'is not'")


def _last_login_expression():
    return (
        select(func.max(UserCredential.last_login_at))
        .where(UserCredential.subscriber_id == Subscriber.id)
        .scalar_subquery()
    )


USER_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "first_name": FilterFieldSpec(field="first_name", expression=Subscriber.first_name, field_type="text"),
    "last_name": FilterFieldSpec(field="last_name", expression=Subscriber.last_name, field_type="text"),
    "display_name": FilterFieldSpec(field="display_name", expression=Subscriber.display_name, field_type="text"),
    "email": FilterFieldSpec(field="email", expression=Subscriber.email, field_type="text"),
    "user_type": FilterFieldSpec(
        field="user_type",
        expression=Subscriber.user_type,
        field_type="select",
        options={item[0] for item in USER_TYPE_OPTIONS},
    ),
    "is_active": FilterFieldSpec(field="is_active", expression=Subscriber.is_active, field_type="boolean"),
    "status": FilterFieldSpec(
        field="status",
        field_type="select",
        options={"active", "inactive", "pending"},
        operators={"=", "!="},
        builder=_status_filter_expression,
    ),
    "role_id": FilterFieldSpec(
        field="role_id",
        field_type="uuid",
        operators={"=", "!=", "in", "not in", "is", "is not"},
        builder=_role_filter_expression,
    ),
    "mfa_enabled": FilterFieldSpec(
        field="mfa_enabled",
        field_type="boolean",
        operators={"=", "!=", "is", "is not"},
        builder=_mfa_filter_expression,
    ),
    "created_at": FilterFieldSpec(field="created_at", expression=Subscriber.created_at, field_type="datetime"),
    "updated_at": FilterFieldSpec(field="updated_at", expression=Subscriber.updated_at, field_type="datetime"),
    "last_login": FilterFieldSpec(field="last_login", expression=_last_login_expression(), field_type="datetime"),
}


USER_SORT_FIELDS = {
    "first_name": Subscriber.first_name,
    "last_name": Subscriber.last_name,
    "email": Subscriber.email,
    "created_at": Subscriber.created_at,
    "updated_at": Subscriber.updated_at,
}


def normalize_user_type(value: str | None) -> UserType:
    if value in USER_TYPE_LABELS:
        return UserType(value)
    return UserType.system_user


def user_type_label(value: UserType | str | None) -> str:
    if isinstance(value, UserType):
        key = value.value
    elif isinstance(value, str):
        key = value
    else:
        key = UserType.system_user.value
    return USER_TYPE_LABELS.get(key, "System User")


def get_user_stats(db: Session) -> dict[str, int]:
    """Return summary statistics for system users page."""
    total = db.scalar(select(func.count()).select_from(Subscriber)) or 0
    active = (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.is_active.is_(True))
        )
        or 0
    )

    admin_role_id = db.scalar(
        select(Role.id)
        .where(func.lower(Role.name) == "admin")
        .where(Role.is_active.is_(True))
        .limit(1)
    )
    admins = 0
    if admin_role_id:
        admins = (
            db.scalar(
                select(func.count(func.distinct(SubscriberRoleModel.subscriber_id))).where(
                    SubscriberRoleModel.role_id == admin_role_id
                )
            )
            or 0
        )

    pending = (
        db.scalar(select(func.count()).select_from(Subscriber).where(_pending_credential_expression()))
        or 0
    )

    return {"total": total, "active": active, "admins": admins, "pending": pending}


def _legacy_filters(
    *,
    search: str | None,
    role_id: str | None,
    status: str | None,
) -> FilterQuery:
    and_rows: list[FilterCondition] = []
    or_rows: list[FilterCondition] = []

    if search and search.strip():
        query = search.strip()
        or_rows.extend(
            [
                FilterCondition(USER_DOCTYPE, "first_name", "like", query),
                FilterCondition(USER_DOCTYPE, "last_name", "like", query),
                FilterCondition(USER_DOCTYPE, "email", "like", query),
                FilterCondition(USER_DOCTYPE, "display_name", "like", query),
            ]
        )

    if role_id:
        and_rows.append(FilterCondition(USER_DOCTYPE, "role_id", "=", role_id))

    if status:
        and_rows.append(FilterCondition(USER_DOCTYPE, "status", "=", status))

    return FilterQuery(and_filters=and_rows, or_filters=or_rows)


def _merge_filter_queries(*queries: FilterQuery) -> FilterQuery:
    and_filters: list[FilterCondition] = []
    or_filters: list[FilterCondition] = []
    for query in queries:
        and_filters.extend(query.and_filters)
        or_filters.extend(query.or_filters)
    return FilterQuery(and_filters=and_filters, or_filters=or_filters)


def _serialize_filter_schema(db: Session) -> list[dict[str, object]]:
    roles = list_active_roles(db)
    role_options = [{"value": str(role.id), "label": role.name} for role in roles]

    options_map: dict[str, list[dict[str, str]]] = {
        "user_type": [{"value": value, "label": label} for value, label in USER_TYPE_OPTIONS],
        "status": [
            {"value": "active", "label": "Active"},
            {"value": "inactive", "label": "Inactive"},
            {"value": "pending", "label": "Pending"},
        ],
        "is_active": [
            {"value": "true", "label": "True"},
            {"value": "false", "label": "False"},
        ],
        "mfa_enabled": [
            {"value": "true", "label": "Enabled"},
            {"value": "false", "label": "Disabled"},
        ],
        "role_id": role_options,
    }

    labels = {
        "first_name": "First Name",
        "last_name": "Last Name",
        "display_name": "Display Name",
        "email": "Email",
        "user_type": "User Type",
        "is_active": "Is Active",
        "status": "Status",
        "role_id": "Role",
        "mfa_enabled": "MFA Enabled",
        "created_at": "Created At",
        "updated_at": "Updated At",
        "last_login": "Last Login",
    }

    schema: list[dict[str, object]] = []
    for field_name, spec in USER_FILTER_SPECS.items():
        operators = (
            sorted(list(spec.operators))
            if spec.operators is not None
            else sorted(list(DEFAULT_OPERATORS_BY_TYPE.get(spec.field_type, {"="})))
        )
        schema.append(
            {
                "field": field_name,
                "label": labels.get(field_name, field_name.replace("_", " ").title()),
                "type": spec.field_type,
                "operators": [
                    {"value": operator, "label": OPERATOR_LABELS.get(operator, operator)}
                    for operator in operators
                    if operator in OPERATOR_LABELS
                ],
                "options": options_map.get(field_name, []),
            }
        )

    return schema


def list_users(
    db: Session,
    *,
    search: str | None,
    role_id: str | None,
    status: str | None,
    filters: str | None,
    order_by: str | None,
    order_dir: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    """Return paginated users list with role and auth metadata."""
    stmt = select(Subscriber)

    try:
        dynamic_query = parse_filter_payload(filters, default_doctype=USER_DOCTYPE)
        legacy_query = _legacy_filters(search=search, role_id=role_id, status=status)
        merged_query = _merge_filter_queries(dynamic_query, legacy_query)
        where_clause = build_filter_expression(
            merged_query,
            doctype=USER_DOCTYPE,
            field_specs=USER_FILTER_SPECS,
        )
        if where_clause is not None:
            stmt = stmt.where(where_clause)

        sort_clause = build_sort_clause(
            order_by=order_by,
            order_dir=order_dir,
            allowed_sort_fields=USER_SORT_FIELDS,
            default_field="last_name",
            default_dir="asc",
        )
    except FilterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    people = db.execute(
        stmt.order_by(sort_clause, Subscriber.first_name.asc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()

    person_ids = [person.id for person in people]
    if not person_ids:
        return [], total

    credentials = db.execute(
        select(UserCredential).where(UserCredential.subscriber_id.in_(person_ids))
    ).scalars().all()

    credential_info: dict = {}
    for credential in credentials:
        info = credential_info.setdefault(
            credential.subscriber_id,
            {"last_login": None, "has_active": False, "must_change_password": False},
        )
        if credential.is_active:
            info["has_active"] = True
            if credential.must_change_password:
                info["must_change_password"] = True
        if credential.last_login_at and (
            info["last_login"] is None or credential.last_login_at > info["last_login"]
        ):
            info["last_login"] = credential.last_login_at

    mfa_enabled = set(
        db.execute(
            select(MFAMethod.subscriber_id)
            .where(MFAMethod.subscriber_id.in_(person_ids))
            .where(MFAMethod.enabled.is_(True))
            .where(MFAMethod.is_active.is_(True))
        )
        .scalars()
        .all()
    )

    roles_rows = db.execute(
        select(SubscriberRoleModel, Role)
        .join(Role, Role.id == SubscriberRoleModel.role_id)
        .where(SubscriberRoleModel.subscriber_id.in_(person_ids))
        .order_by(SubscriberRoleModel.assigned_at.desc())
    ).all()
    role_map: dict = {}
    for person_role, role in roles_rows:
        role_map.setdefault(person_role.subscriber_id, []).append(
            {
                "id": str(role.id),
                "name": role.name,
                "is_active": role.is_active,
            }
        )

    users: list[dict] = []
    for person in people:
        name = person.display_name or f"{person.first_name} {person.last_name}".strip()
        info = credential_info.get(person.id, {})
        users.append(
            {
                "id": str(person.id),
                "name": name,
                "email": person.email,
                "roles": role_map.get(person.id, []),
                "user_type": person.user_type.value if person.user_type else UserType.system_user.value,
                "user_type_label": user_type_label(person.user_type),
                "is_active": bool(person.is_active),
                "mfa_enabled": person.id in mfa_enabled,
                "last_login": info.get("last_login"),
            }
        )

    return users, total


def list_active_roles(db: Session) -> list[Role]:
    roles = (
        db.execute(
            select(Role)
            .where(Role.is_active.is_(True))
            .order_by(Role.name.asc())
            .limit(500)
        )
        .scalars()
        .all()
    )
    return list(roles)


def build_users_page_state(
    db: Session,
    *,
    search: str | None,
    role: str | None,
    status: str | None,
    filters: str | None,
    order_by: str | None,
    order_dir: str | None,
    offset: int,
    limit: int,
) -> dict[str, object]:
    users, total = list_users(
        db,
        search=search,
        role_id=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return {
        "users": users,
        "search": search,
        "role": role,
        "status": status,
        "filters": filters,
        "order_by": order_by or "last_name",
        "order_dir": order_dir or "asc",
        "stats": get_user_stats(db),
        "roles": list_active_roles(db),
        "user_type_options": USER_TYPE_OPTIONS,
        "filter_schema": _serialize_filter_schema(db),
        "pagination": total > limit,
        "total": total,
        "offset": offset,
        "limit": limit,
    }
