"""Router health policy: typed, validated, fail-closed (``network.router_health``).

The policy is stored in setting ``network.router_health_policy`` (owner-command
only, audited). A router with no matching entry gets ``defaults`` and role
``access``, the strictest, so new routers receive every check. Exceptions are
dated and lapse at the end of their ``until`` day (Africa/Lagos); annotations
never suppress anything.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.router_health.routeros_export import normalize_identity
from app.timezone import APP_TIMEZONE

logger = logging.getLogger(__name__)

POLICY_VERSION = 1
MAX_UNTIL_DAYS = 180
MAX_REGEX_LENGTH = 200
EXPIRED_NOTE_DAYS = 7
POLICY_SETTING_DOMAIN = SettingDomain.network
POLICY_SETTING_KEY = "router_health_policy"

_BACKREF_RE = re.compile(r"\\[1-9]|\(\?P=|\\g<")
KNOWN_CHECKS_OFF = frozenset(
    {"suspension-jump", "suspension-chain", "radius-incoming", "ppp-drop", "coa-canary"}
)


class Role(StrEnum):
    access = "access"
    core = "core"
    border = "border"


class InputMode(StrEnum):
    enforce = "enforce"
    observe = "observe"
    custom = "custom"
    exempt = "exempt"


def today_in_app_timezone(now: datetime | None = None) -> date:
    moment = now or datetime.now(APP_TIMEZONE)
    return moment.astimezone(APP_TIMEZONE).date()


def _regex_error(pattern: str) -> str | None:
    if len(pattern) > MAX_REGEX_LENGTH:
        return f"regex longer than {MAX_REGEX_LENGTH} characters"
    if _BACKREF_RE.search(pattern):
        return "regex backreferences are not allowed"
    try:
        re.compile(pattern)
    except re.error:
        return "regex does not compile"
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputPolicy(_Strict):
    """Full input-chain policy (used in ``defaults``)."""

    mode: InputMode = InputMode.enforce
    accept_comment_prefix: str = Field(min_length=1, max_length=80)
    final_drop_comment_regex: str = Field(min_length=1)
    min_accepts: int = Field(ge=0, le=200)

    @model_validator(mode="after")
    def _regex(self) -> InputPolicy:
        error = _regex_error(self.final_drop_comment_regex)
        if error:
            raise ValueError(error)
        return self


class InputOverride(_Strict):
    """Per-router override of one input chain policy."""

    mode: InputMode | None = None
    accept_comment_prefix: str | None = Field(default=None, min_length=1, max_length=80)
    final_drop_comment_regex: str | None = Field(default=None, min_length=1)
    min_accepts: int | None = Field(default=None, ge=0, le=200)
    until: date | None = None
    permanent: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def _rules(self) -> InputOverride:
        if not self.reason.strip():
            raise ValueError("every override needs a non-empty reason")
        if self.final_drop_comment_regex is not None:
            error = _regex_error(self.final_drop_comment_regex)
            if error:
                raise ValueError(error)
        regex_override = (
            self.accept_comment_prefix is not None
            or self.final_drop_comment_regex is not None
        )
        if self.mode is InputMode.custom and not regex_override:
            raise ValueError("mode custom needs a prefix and regex")
        if self.mode is InputMode.exempt and self.until is None:
            raise ValueError("mode exempt needs until")
        if self.permanent and not regex_override:
            raise ValueError("permanent is allowed only for comment-regex overrides")
        weakens = self.mode in {InputMode.observe, InputMode.exempt} or (
            self.min_accepts is not None
        )
        if weakens and self.until is None:
            raise ValueError("this override needs an until date")
        if self.until is not None and self.permanent:
            raise ValueError("an override is either permanent or dated, not both")
        if self.until is None and not self.permanent and not regex_override:
            if self.mode not in {None, InputMode.enforce}:
                raise ValueError("this override needs until or permanent")
        return self


class OpsUserPolicy(_Strict):
    name: str
    group: str
    required_address: str


class RestUserPolicy(_Strict):
    name: str
    forbidden_groups: list[str] = Field(default_factory=list)


class PolicyDefaults(_Strict):
    role: Role = Role.access
    snmp_allowed_sources: list[str] = Field(default_factory=list)
    input_v4: InputPolicy
    input_v6: InputPolicy
    suspension_target_chains: list[str] = Field(min_length=1)
    reverse_proxy_min_ros: str = "7.22"
    # Walled-garden expectations for the suspension chain (posture checks).
    suspension_portal_addresses: list[str] = Field(default_factory=list)
    suspension_exclusion_max_prefix_breadth: int = Field(default=24, ge=8, le=32)
    suspension_exclusion_max_entries: int = Field(default=50, ge=1, le=5000)
    ops_user: OpsUserPolicy | None = None
    rest_user: RestUserPolicy | None = None
    report_unrestricted_full_users: bool = True

    @model_validator(mode="after")
    def _sources(self) -> PolicyDefaults:
        for source in self.snmp_allowed_sources:
            try:
                network = ipaddress.ip_network(source, strict=False)
            except ValueError as exc:
                raise ValueError("snmp_allowed_sources must be CIDRs") from exc
            if network.prefixlen == 0:
                raise ValueError("snmp_allowed_sources must not allow every address")
        for portal in self.suspension_portal_addresses:
            try:
                ipaddress.ip_address(portal)
            except ValueError as exc:
                raise ValueError(
                    "suspension_portal_addresses must be single IP addresses"
                ) from exc
        if not re.fullmatch(r"\d+(\.\d+)*", self.reverse_proxy_min_ros):
            raise ValueError("reverse_proxy_min_ros must be a dotted version")
        return self


class RoleSpec(_Strict):
    checks_off: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names(self) -> RoleSpec:
        unknown = sorted(set(self.checks_off) - KNOWN_CHECKS_OFF)
        if unknown:
            raise ValueError(f"unknown checks_off name(s): {', '.join(unknown)}")
        return self


class RouterMatch(_Strict):
    identity: str | None = None
    router_name: str | None = None
    router_id: UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> RouterMatch:
        if (
            self.identity is None
            and self.router_name is None
            and self.router_id is None
        ):
            raise ValueError("match needs identity, router_name or router_id")
        return self


class PolicyException(_Strict):
    check: str = Field(min_length=1, max_length=60)
    scope: str = Field(default="*", min_length=1, max_length=120)
    until: date
    reason: str = ""

    @model_validator(mode="after")
    def _reason(self) -> PolicyException:
        if not self.reason.strip():
            raise ValueError("every exception needs a non-empty reason")
        return self


class PolicyAnnotation(_Strict):
    check: str = Field(min_length=1, max_length=60)
    note: str = Field(min_length=1, max_length=300)


class RouterEntry(_Strict):
    match: RouterMatch
    role: Role | None = None
    role_reason: str = ""
    input_v4: InputOverride | None = None
    input_v6: InputOverride | None = None
    exceptions: list[PolicyException] = Field(default_factory=list)
    annotations: list[PolicyAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _role(self) -> RouterEntry:
        if self.role is not None and not self.role_reason.strip():
            raise ValueError("every override needs a non-empty reason (role_reason)")
        return self


class RouterHealthPolicy(_Strict):
    version: Literal[1] = 1
    defaults: PolicyDefaults | None = None
    roles: dict[Role, RoleSpec] = Field(default_factory=dict)
    routers: list[RouterEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_matches(self) -> RouterHealthPolicy:
        seen: set[tuple[str | None, str | None, UUID | None]] = set()
        for entry in self.routers:
            identity = (
                normalize_identity(entry.match.identity)
                if entry.match.identity is not None
                else None
            )
            key = (identity, entry.match.router_name, entry.match.router_id)
            if key in seen:
                raise ValueError("two router entries have the same match")
            seen.add(key)
        return self


def built_in_defaults() -> PolicyDefaults:
    """Strictest built-in defaults used when no policy is stored or it is invalid."""
    return PolicyDefaults(
        role=Role.access,
        snmp_allowed_sources=[],
        input_v4=InputPolicy(
            mode=InputMode.enforce,
            accept_comment_prefix="INPUT-PROTECT:",
            final_drop_comment_regex="(?i)^INPUT-PROTECT: drop all else$",
            min_accepts=8,
        ),
        input_v6=InputPolicy(
            mode=InputMode.observe,
            accept_comment_prefix="INPUT6-PROTECT:",
            final_drop_comment_regex="(?i)^INPUT6-PROTECT: drop all else$",
            min_accepts=4,
        ),
        suspension_target_chains=["dotmac-suspended", "dotmac-wg"],
    )


def _until_errors(policy: RouterHealthPolicy, today: date) -> str | None:
    limit = today + timedelta(days=MAX_UNTIL_DAYS)
    for entry in policy.routers:
        dates: list[date] = [exc.until for exc in entry.exceptions]
        for override in (entry.input_v4, entry.input_v6):
            if override is not None and override.until is not None:
                dates.append(override.until)
        for until in dates:
            if until > limit:
                return f"until dates may be at most {MAX_UNTIL_DAYS} days ahead"
    return None


def router_health_policy_error(
    value: object, *, today: date | None = None
) -> str | None:
    """Return a safe validation message for a stored policy value, else None.

    Registered in ``TYPED_JSON_SETTING_VALIDATORS``; never echoes the value.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return "router health policy must be a JSON object"
    try:
        policy = RouterHealthPolicy.model_validate(value)
    except ValidationError as exc:
        errors = exc.errors()
        location = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
        message = str(errors[0]["msg"]) if errors else "invalid"
        return (
            f"invalid router health policy at {location or 'root'}: "
            + message.removeprefix("Value error, ")
        )
    return _until_errors(policy, today or today_in_app_timezone())


@dataclass(frozen=True)
class EffectiveInput:
    mode: InputMode
    accept_comment_prefix: str
    final_drop_regex: re.Pattern[str]
    min_accepts: int
    note: str | None = None


@dataclass(frozen=True)
class ActiveException:
    check: str
    scope: str
    until: date
    reason: str


@dataclass(frozen=True)
class LapsedException:
    kind: str
    check: str
    scope: str
    until: date
    reason: str


@dataclass(frozen=True)
class EffectiveRouterPolicy:
    role: Role
    checks_off: frozenset[str]
    snmp_allowed_sources: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    input_v4: EffectiveInput
    input_v6: EffectiveInput
    suspension_target_chains: frozenset[str]
    reverse_proxy_min_ros: str
    suspension_portal_addresses: frozenset[str] = frozenset()
    suspension_exclusion_max_prefix_breadth: int = 24
    suspension_exclusion_max_entries: int = 50
    ops_user: OpsUserPolicy | None = None
    rest_user: RestUserPolicy | None = None
    report_unrestricted_full_users: bool = True
    exceptions: tuple[ActiveException, ...] = ()
    annotations: dict[str, str] = field(default_factory=dict)
    lapsed: tuple[LapsedException, ...] = ()
    matched_entry: bool = False
    ambiguous: bool = False

    def check_off(self, name: str) -> bool:
        return name in self.checks_off

    def excepted(self, check: str, scope: str = "*") -> bool:
        return any(
            exc.check == check and exc.scope in {"*", scope} for exc in self.exceptions
        )

    def known_issue(self, check: str) -> str | None:
        return self.annotations.get(check)


def _effective_input(
    base: InputPolicy,
    override: InputOverride | None,
    today: date,
    lapsed: list[LapsedException],
    label: str,
) -> EffectiveInput:
    prefix = base.accept_comment_prefix
    regex = base.final_drop_comment_regex
    mode = base.mode
    min_accepts = base.min_accepts
    if override is not None:
        if override.until is not None and today > override.until:
            lapsed.append(
                LapsedException(
                    kind=label,
                    check=label,
                    scope="*",
                    until=override.until,
                    reason=override.reason,
                )
            )
        else:
            if override.accept_comment_prefix is not None:
                prefix = override.accept_comment_prefix
            if override.final_drop_comment_regex is not None:
                regex = override.final_drop_comment_regex
            if override.mode is not None:
                mode = override.mode
            elif (
                override.accept_comment_prefix is not None
                or override.final_drop_comment_regex is not None
            ):
                mode = InputMode.custom
            if override.min_accepts is not None:
                min_accepts = override.min_accepts
    return EffectiveInput(
        mode=mode,
        accept_comment_prefix=prefix,
        final_drop_regex=re.compile(regex),
        min_accepts=min_accepts,
    )


def _entry_matches(
    entry: RouterEntry,
    *,
    identity: str | None,
    router_name: str,
    router_id: UUID,
) -> bool:
    match = entry.match
    if match.identity is not None:
        if identity is None or normalize_identity(match.identity) != normalize_identity(
            identity
        ):
            return False
    if match.router_name is not None and match.router_name != router_name:
        return False
    return not (match.router_id is not None and match.router_id != router_id)


def matching_entries(
    policy: RouterHealthPolicy,
    *,
    identity: str | None,
    router_name: str,
    router_id: UUID,
) -> list[RouterEntry]:
    return [
        entry
        for entry in policy.routers
        if _entry_matches(
            entry, identity=identity, router_name=router_name, router_id=router_id
        )
    ]


def effective_policy(
    policy: RouterHealthPolicy,
    *,
    identity: str | None,
    router_name: str,
    router_id: UUID,
    today: date,
) -> EffectiveRouterPolicy:
    """Resolve role, overrides, dated exceptions and annotations for one router."""
    defaults = policy.defaults or built_in_defaults()
    entries = matching_entries(
        policy, identity=identity, router_name=router_name, router_id=router_id
    )
    ambiguous = len(entries) > 1
    entry = entries[0] if len(entries) == 1 else None
    role = defaults.role
    if entry is not None and entry.role is not None:
        role = entry.role
    role_spec = policy.roles.get(role) or RoleSpec()
    lapsed: list[LapsedException] = []
    active: list[ActiveException] = []
    annotations: dict[str, str] = {}
    if entry is not None:
        for exc in entry.exceptions:
            if today > exc.until:
                lapsed.append(
                    LapsedException(
                        kind="exception",
                        check=exc.check,
                        scope=exc.scope,
                        until=exc.until,
                        reason=exc.reason,
                    )
                )
            else:
                active.append(
                    ActiveException(
                        check=exc.check,
                        scope=exc.scope,
                        until=exc.until,
                        reason=exc.reason,
                    )
                )
        for note in entry.annotations:
            annotations[note.check] = note.note
    input_v4 = _effective_input(
        defaults.input_v4,
        entry.input_v4 if entry else None,
        today,
        lapsed,
        "input_v4",
    )
    input_v6 = _effective_input(
        defaults.input_v6,
        entry.input_v6 if entry else None,
        today,
        lapsed,
        "input_v6",
    )
    sources = tuple(
        ipaddress.ip_network(source, strict=False)
        for source in defaults.snmp_allowed_sources
    )
    return EffectiveRouterPolicy(
        role=role,
        checks_off=frozenset(role_spec.checks_off),
        snmp_allowed_sources=sources,
        input_v4=input_v4,
        input_v6=input_v6,
        suspension_target_chains=frozenset(defaults.suspension_target_chains),
        reverse_proxy_min_ros=defaults.reverse_proxy_min_ros,
        suspension_portal_addresses=frozenset(defaults.suspension_portal_addresses),
        suspension_exclusion_max_prefix_breadth=(
            defaults.suspension_exclusion_max_prefix_breadth
        ),
        suspension_exclusion_max_entries=defaults.suspension_exclusion_max_entries,
        ops_user=defaults.ops_user,
        rest_user=defaults.rest_user,
        report_unrestricted_full_users=defaults.report_unrestricted_full_users,
        exceptions=tuple(active),
        annotations=annotations,
        lapsed=tuple(lapsed),
        matched_entry=entry is not None,
        ambiguous=ambiguous,
    )


@dataclass(frozen=True)
class LoadedPolicy:
    policy: RouterHealthPolicy
    error: str | None = None


def parse_policy(value: object, *, today: date | None = None) -> LoadedPolicy:
    """Validate a stored value; on any problem return built-in defaults + error."""
    error = router_health_policy_error(value, today=today)
    if error is None and isinstance(value, dict):
        try:
            return LoadedPolicy(policy=RouterHealthPolicy.model_validate(value))
        except ValidationError:  # pragma: no cover - guarded by the check above
            error = "invalid router health policy"
    return LoadedPolicy(policy=RouterHealthPolicy(), error=error)


def load_policy(db: Session) -> LoadedPolicy:
    """Read the stored policy (fail-closed: invalid -> defaults + a policy finding)."""
    from app.services import settings_spec

    value: Any = settings_spec.resolve_value(
        db, POLICY_SETTING_DOMAIN, POLICY_SETTING_KEY
    )
    return parse_policy(value)


def unmatched_entries(
    policy: RouterHealthPolicy,
    routers: list[tuple[str | None, str, UUID]],
) -> list[RouterEntry]:
    """Entries that match none of the given (identity, name, id) routers."""
    unmatched: list[RouterEntry] = []
    for entry in policy.routers:
        if not any(
            _entry_matches(entry, identity=ident, router_name=name, router_id=rid)
            for ident, name, rid in routers
        ):
            unmatched.append(entry)
    return unmatched
