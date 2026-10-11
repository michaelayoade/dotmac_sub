"""Count credential security cohorts without materializing credential values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression

from app.models.auth import AuthProvider, UserCredential
from app.services.domain_errors import DomainError


class CredentialFormat(StrEnum):
    ARGON2ID = "argon2id"
    PBKDF2_SHA256 = "pbkdf2_sha256"
    BCRYPT = "bcrypt"
    SHA512_CRYPT = "sha512_crypt"
    PLAIN = "plain"
    ENCRYPTED = "encrypted"
    SECRET_REFERENCE = "secret_reference"
    MISSING = "missing"
    UNKNOWN = "unknown"


class PrincipalType(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"
    RESELLER = "reseller"
    INVALID = "invalid"


class AgeBand(StrEnum):
    UNKNOWN = "unknown"
    FUTURE = "future"
    UNDER_30 = "under_30_days"
    DAYS_30_89 = "30_to_89_days"
    DAYS_90_364 = "90_to_364_days"
    DAYS_365_PLUS = "365_days_or_more"


class ProviderGroup(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"


@dataclass(frozen=True)
class CredentialInventoryQuery:
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


@dataclass(frozen=True)
class CredentialCohort:
    principal: PrincipalType
    provider: ProviderGroup
    format: CredentialFormat
    password_age: AgeBand
    last_login_age: AgeBand
    active: bool
    count: int


@dataclass(frozen=True)
class CredentialInventoryResult:
    observed_at: datetime
    cohorts: tuple[CredentialCohort, ...]

    @property
    def total_count(self) -> int:
        return sum(row.count for row in self.cohorts)

    @property
    def reversible_count(self) -> int:
        return sum(
            row.count
            for row in self.cohorts
            if row.format
            in (
                CredentialFormat.PLAIN,
                CredentialFormat.ENCRYPTED,
                CredentialFormat.SECRET_REFERENCE,
            )
        )

    @property
    def dormant_legacy_count(self) -> int:
        """Inventory candidate only; not a reset decision or T0 deadline."""
        return sum(
            row.count
            for row in self.cohorts
            if row.active
            and row.provider == ProviderGroup.LOCAL
            and row.format
            in (
                CredentialFormat.PBKDF2_SHA256,
                CredentialFormat.BCRYPT,
                CredentialFormat.SHA512_CRYPT,
            )
            and row.last_login_age
            in (
                AgeBand.UNKNOWN,
                AgeBand.DAYS_90_364,
                AgeBand.DAYS_365_PLUS,
            )
        )


def _age_band(
    column: SQLColumnExpression[datetime | None], observed_at: datetime
) -> ColumnElement[str]:
    return case(
        (column.is_(None), AgeBand.UNKNOWN.value),
        (column > observed_at, AgeBand.FUTURE.value),
        (column > observed_at - timedelta(days=30), AgeBand.UNDER_30.value),
        (column > observed_at - timedelta(days=90), AgeBand.DAYS_30_89.value),
        (column > observed_at - timedelta(days=365), AgeBand.DAYS_90_364.value),
        else_=AgeBand.DAYS_365_PLUS.value,
    )


def credential_inventory_statement(
    *, query: CredentialInventoryQuery
) -> Select[tuple[str, str, str, str, str, bool, int]]:
    """Only CASE labels and COUNT cross the database boundary; never raw rows.

    Prefix families intentionally include malformed values. This is a stored
    representation census, not password/hash validation or secret resolution.
    """
    credential = UserCredential
    stored = credential.password_hash
    format_label = case(
        (stored.is_(None), CredentialFormat.MISSING.value),
        (stored == "", CredentialFormat.MISSING.value),
        (stored.startswith("$argon2id$"), CredentialFormat.ARGON2ID.value),
        (stored.startswith("$pbkdf2-sha256$"), CredentialFormat.PBKDF2_SHA256.value),
        (
            or_(*(stored.startswith(prefix) for prefix in ("$2a$", "$2b$", "$2y$"))),
            CredentialFormat.BCRYPT.value,
        ),
        (stored.startswith("$6$"), CredentialFormat.SHA512_CRYPT.value),
        (stored.startswith("plain:"), CredentialFormat.PLAIN.value),
        (stored.startswith("enc:"), CredentialFormat.ENCRYPTED.value),
        (
            or_(
                *(
                    stored.startswith(prefix)
                    for prefix in ("bao://", "openbao://", "vault://", "env://")
                )
            ),
            CredentialFormat.SECRET_REFERENCE.value,
        ),
        else_=CredentialFormat.UNKNOWN.value,
    )
    principal_count = (
        case((credential.subscriber_id.is_not(None), 1), else_=0)
        + case((credential.system_user_id.is_not(None), 1), else_=0)
        + case((credential.reseller_user_id.is_not(None), 1), else_=0)
    )
    principal = case(
        (principal_count != 1, PrincipalType.INVALID.value),
        (credential.subscriber_id.is_not(None), PrincipalType.CUSTOMER.value),
        (credential.system_user_id.is_not(None), PrincipalType.STAFF.value),
        else_=PrincipalType.RESELLER.value,
    )
    dimensions = select(
        principal.label("principal"),
        case(
            (credential.provider == AuthProvider.local, ProviderGroup.LOCAL.value),
            else_=ProviderGroup.EXTERNAL.value,
        ).label("provider"),
        format_label.label("format"),
        _age_band(credential.password_updated_at, query.observed_at).label(
            "password_age"
        ),
        _age_band(credential.last_login_at, query.observed_at).label("last_login_age"),
        credential.is_active.label("active"),
    ).subquery()
    columns = tuple(dimensions.c)
    return (
        select(*columns, func.count().label("count"))
        .group_by(*columns)
        .order_by(*columns)
    )


def read_credential_inventory(
    *,
    db: Session,
    query: CredentialInventoryQuery,
) -> CredentialInventoryResult:
    """Read from the adapter's PostgreSQL read-only snapshot, fail closed."""
    if db.new or db.dirty or db.deleted or db.get_bind().dialect.name != "postgresql":
        raise DomainError(
            code="auth.credential_security_inventory.read_only_required",
            message="A clean PostgreSQL read-only snapshot is required.",
            retryable=False,
        )
    options = db.connection().get_execution_options()
    if (
        options.get("postgresql_readonly") is not True
        or options.get("isolation_level") != "REPEATABLE READ"
    ):
        raise DomainError(
            code="auth.credential_security_inventory.read_only_required",
            message="A clean PostgreSQL read-only snapshot is required.",
            retryable=False,
        )
    with db.no_autoflush:
        rows = db.execute(credential_inventory_statement(query=query)).all()
    return CredentialInventoryResult(
        observed_at=query.observed_at,
        cohorts=tuple(
            CredentialCohort(
                principal=PrincipalType(row[0]),
                provider=ProviderGroup(row[1]),
                format=CredentialFormat(row[2]),
                password_age=AgeBand(row[3]),
                last_login_age=AgeBand(row[4]),
                active=bool(row[5]),
                count=int(row[6]),
            )
            for row in rows
        ),
    )
