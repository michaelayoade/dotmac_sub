"""Per-router readiness of the walled-garden module, from config snapshots.

Owner: ``access.walled_garden_router_readiness`` (read-only resolver).

Readiness compares the module rendered from CURRENT settings with the latest
``router_config_snapshots`` ``/export`` text of a router. It never contacts a
router. The outcome is what a captive policy uses to fail closed: captive is
only safe where the serving router is ``ready``.

Statuses are distinct on purpose:

* ``ready`` - every expected element is present, enabled, matching, unique,
  and the forward jump precedes the legacy quarantine jump;
* ``not_ready`` - at least one finding (missing, drifted, disabled,
  duplicated, misplaced, disabled-entry-present, unexpected);
* ``stale`` - the latest snapshot is older than the threshold (default 48h);
  findings are still reported but the router is never treated as ready;
* ``no_snapshot`` - the router has no snapshot at all;
* ``not_configured`` - the module cannot be rendered from settings.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.router_management import Router, RouterConfigSnapshot
from app.services.walled_garden_router_module import (
    LEGACY_QUARANTINE_COMMENTS,
    LEGACY_QUARANTINE_JUMP_COMMENT,
    MODULE_VERSION,
    TAG_ROOT,
    WalledGardenElement,
    WalledGardenModuleError,
    WalledGardenRouterModule,
    WalledGardenRouterResource,
    render_walled_garden_module_from_settings,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SNAPSHOT_AGE = timedelta(hours=48)

_SECTION_RESOURCES: dict[str, WalledGardenRouterResource] = {
    "/ip firewall address-list": WalledGardenRouterResource.address_list,
    "/ip firewall filter": WalledGardenRouterResource.filter,
    "/ip firewall nat": WalledGardenRouterResource.nat,
}
_VERBS = frozenset({"add", "set"})
_TOKEN_RE = re.compile(r'([^\s=]+)=("(?:[^"\\]|\\.)*"|\S*)|(\S+)')
_ESCAPE_RE = re.compile(r"\\([0-9A-Fa-f]{2}|.)")


class WalledGardenReadinessStatus(StrEnum):
    ready = "ready"
    not_ready = "not_ready"
    stale = "stale"
    no_snapshot = "no_snapshot"
    not_configured = "not_configured"


class WalledGardenFindingIssue(StrEnum):
    missing = "missing"
    drifted = "drifted"
    disabled = "disabled"
    duplicated = "duplicated"
    misplaced = "misplaced"
    disabled_entry_present = "disabled_entry_present"
    unexpected = "unexpected"


class WalledGardenReadinessErrorCode(StrEnum):
    ROUTER_NOT_FOUND = "router_not_found"


class WalledGardenReadinessError(Exception):
    def __init__(self, code: WalledGardenReadinessErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RouterOsExportEntry:
    """One ``add``/``set`` row from a RouterOS ``/export``."""

    section: str
    verb: str
    position: int
    properties: Mapping[str, str]

    def get(self, key: str) -> str | None:
        return self.properties.get(key)

    @property
    def comment(self) -> str:
        return self.properties.get("comment", "")

    @property
    def disabled(self) -> bool:
        return self.properties.get("disabled", "no").lower() in {"yes", "true"}


@dataclass(frozen=True, slots=True)
class WalledGardenFinding:
    element_tag: str
    issue: WalledGardenFindingIssue
    detail: str
    entry_key: str | None = None


@dataclass(frozen=True, slots=True)
class WalledGardenExportEvaluation:
    findings: tuple[WalledGardenFinding, ...]
    legacy_rules_present: tuple[str, ...]
    legacy_rule_count: int
    static_suspended_enabled: int
    static_suspended_disabled: int


@dataclass(frozen=True, slots=True)
class WalledGardenRouterReadiness:
    router_id: uuid.UUID
    router_name: str
    status: WalledGardenReadinessStatus
    module_version: str
    evaluated_at: datetime
    snapshot_id: uuid.UUID | None = None
    snapshot_captured_at: datetime | None = None
    findings: tuple[WalledGardenFinding, ...] = ()
    legacy_rules_present: tuple[str, ...] = ()
    legacy_rule_count: int = 0
    static_suspended_enabled: int = 0
    static_suspended_disabled: int = 0
    configuration_error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status is WalledGardenReadinessStatus.ready

    @property
    def missing_elements(self) -> tuple[str, ...]:
        return tuple(
            item.element_tag
            for item in self.findings
            if item.issue is WalledGardenFindingIssue.missing
        )

    @property
    def missing_entry_keys(self) -> tuple[str, ...]:
        """Enabled allowed-resource entries with at least one missing row."""

        return tuple(
            dict.fromkeys(
                item.entry_key
                for item in self.findings
                if item.issue is WalledGardenFindingIssue.missing and item.entry_key
            )
        )

    @property
    def drifted_entry_keys(self) -> tuple[str, ...]:
        """Disabled entries whose rows are still present on the router."""

        return tuple(
            dict.fromkeys(
                item.entry_key
                for item in self.findings
                if item.issue is WalledGardenFindingIssue.disabled_entry_present
                and item.entry_key
            )
        )


@dataclass(frozen=True, slots=True)
class WalledGardenReadinessQuery:
    router_id: uuid.UUID
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE
    evaluated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FleetWalledGardenReadinessQuery:
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE
    include_inactive: bool = False
    evaluated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RoutersWalledGardenReadinessQuery:
    """Readiness for an explicit router set, rendering the module once.

    Used by captive policy consumers that need only the routers serving the
    subscriptions they evaluate. Unknown ids are omitted from the result; the
    consumer treats a missing router as not ready.
    """

    router_ids: frozenset[uuid.UUID]
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE
    evaluated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FleetWalledGardenReadiness:
    evaluated_at: datetime
    module_version: str
    routers: tuple[WalledGardenRouterReadiness, ...]

    @property
    def status_counts(self) -> Mapping[WalledGardenReadinessStatus, int]:
        counts = Counter(item.status for item in self.routers)
        return {status: counts.get(status, 0) for status in WalledGardenReadinessStatus}

    @property
    def ready_router_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(item.router_id for item in self.routers if item.is_ready)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        body = value[1:-1]

        def _replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if len(token) == 2:
                return chr(int(token, 16))
            return token

        return _ESCAPE_RE.sub(_replace, body)
    return value


def _logical_lines(text: str) -> list[str]:
    """Join RouterOS backslash line continuations into logical lines."""

    lines: list[str] = []
    buffer = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        piece = raw.lstrip() if buffer else raw
        if piece.endswith("\\") and not piece.endswith("\\\\"):
            buffer += piece[:-1]
            continue
        lines.append(buffer + piece)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def parse_routeros_export(text: str) -> tuple[RouterOsExportEntry, ...]:
    """Parse the firewall sections of a RouterOS ``/export``."""

    entries: list[RouterOsExportEntry] = []
    section = ""
    positions: Counter[str] = Counter()
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words = stripped.split()
        if stripped.startswith("/"):
            path_words: list[str] = []
            for word in words:
                if word in _VERBS or "=" in word:
                    break
                path_words.append(word)
            section = " ".join(path_words)
            remainder = stripped[len(" ".join(path_words)) :].strip()
            if not remainder:
                continue
            stripped = remainder
            words = stripped.split()
        verb = words[0]
        if verb not in _VERBS or section not in _SECTION_RESOURCES:
            continue
        properties: dict[str, str] = {}
        for match in _TOKEN_RE.finditer(stripped[len(verb) :]):
            key, value, bare = match.groups()
            if key is not None:
                properties[key] = _unquote(value)
            elif bare is not None:
                properties[bare] = ""
        entries.append(
            RouterOsExportEntry(
                section=section,
                verb=verb,
                position=positions[section],
                properties=properties,
            )
        )
        positions[section] += 1
    return tuple(entries)


def _section_for(resource: WalledGardenRouterResource) -> str:
    return next(name for name, item in _SECTION_RESOURCES.items() if item is resource)


def _element_findings(
    element: WalledGardenElement, rows: Sequence[RouterOsExportEntry]
) -> list[WalledGardenFinding]:
    matches = [row for row in rows if row.comment == element.tag]
    tag, key = element.tag, element.entry_key
    if not matches:
        return [WalledGardenFinding(tag, WalledGardenFindingIssue.missing, "", key)]
    if len(matches) > 1:
        return [
            WalledGardenFinding(
                tag,
                WalledGardenFindingIssue.duplicated,
                f"{len(matches)} rows carry this tag",
                key,
            )
        ]
    row = matches[0]
    findings: list[WalledGardenFinding] = []
    if row.disabled:
        findings.append(
            WalledGardenFinding(tag, WalledGardenFindingIssue.disabled, "", key)
        )
    for name, expected in element.fields:
        observed = row.get(name)
        if name in element.presence_only_fields:
            ok = observed is not None
        else:
            ok = observed is not None and observed.lower() == expected.lower()
        if not ok:
            findings.append(
                WalledGardenFinding(
                    tag,
                    WalledGardenFindingIssue.drifted,
                    f"{name}: expected {expected!r}, observed {observed!r}",
                    key,
                )
            )
    return findings


def evaluate_export(
    *, export_text: str, module: WalledGardenRouterModule
) -> WalledGardenExportEvaluation:
    """Compare one export with the rendered module (pure function)."""

    entries = parse_routeros_export(export_text)
    by_section: dict[str, list[RouterOsExportEntry]] = {}
    for entry in entries:
        by_section.setdefault(entry.section, []).append(entry)

    findings: list[WalledGardenFinding] = []
    for element in module.elements:
        findings.extend(
            _element_findings(
                element, by_section.get(_section_for(element.resource), [])
            )
        )

    expected_tags = set(module.tags)
    disabled_prefixes = {
        entry.tag_prefix: entry.key for entry in module.entries if not entry.enabled
    }
    for entry in entries:
        comment = entry.comment
        if not comment.startswith(TAG_ROOT) or comment in expected_tags:
            continue
        entry_key = next(
            (
                key
                for prefix, key in disabled_prefixes.items()
                if comment.startswith(prefix)
            ),
            None,
        )
        findings.append(
            WalledGardenFinding(
                comment,
                WalledGardenFindingIssue.disabled_entry_present
                if entry_key
                else WalledGardenFindingIssue.unexpected,
                f"{entry.section} row not in the rendered module",
                entry_key,
            )
        )

    filters = by_section.get(_section_for(WalledGardenRouterResource.filter), [])
    jump = next((item for item in module.elements if item.name == "forward:jump"), None)
    module_jumps = [row for row in filters if jump and row.comment == jump.tag]
    legacy_jumps = [
        row
        for row in filters
        if row.comment == LEGACY_QUARANTINE_JUMP_COMMENT
        and row.get("chain") == "forward"
    ]
    if jump and len(module_jumps) == 1 and legacy_jumps:
        if module_jumps[0].position > min(row.position for row in legacy_jumps):
            findings.append(
                WalledGardenFinding(
                    jump.tag,
                    WalledGardenFindingIssue.misplaced,
                    "module forward jump is evaluated after the legacy quarantine jump",
                )
            )

    legacy = [row for row in filters if row.comment in LEGACY_QUARANTINE_COMMENTS]
    suspended_list = module.config.suspended_address_list
    static_rows = [
        row
        for row in by_section.get(
            _section_for(WalledGardenRouterResource.address_list), []
        )
        if row.get("list") == suspended_list
    ]
    disabled_static = sum(1 for row in static_rows if row.disabled)
    return WalledGardenExportEvaluation(
        findings=tuple(findings),
        legacy_rules_present=tuple(sorted({row.comment for row in legacy})),
        legacy_rule_count=len(legacy),
        static_suspended_enabled=len(static_rows) - disabled_static,
        static_suspended_disabled=disabled_static,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_snapshot(db: Session, router_id: uuid.UUID) -> RouterConfigSnapshot | None:
    return db.scalars(
        select(RouterConfigSnapshot)
        .where(RouterConfigSnapshot.router_id == router_id)
        .order_by(
            RouterConfigSnapshot.created_at.desc(), RouterConfigSnapshot.id.desc()
        )
        .limit(1)
    ).first()


def _render_or_error(
    db: Session,
) -> tuple[WalledGardenRouterModule | None, str | None]:
    try:
        return render_walled_garden_module_from_settings(db), None
    except WalledGardenModuleError as exc:
        return None, f"{exc.code.value}: {exc.message}"


def _evaluate_router(
    db: Session,
    *,
    router: Router,
    module: WalledGardenRouterModule | None,
    configuration_error: str | None,
    max_snapshot_age: timedelta,
    evaluated_at: datetime,
) -> WalledGardenRouterReadiness:
    def outcome(
        status: WalledGardenReadinessStatus,
    ) -> WalledGardenRouterReadiness:
        return WalledGardenRouterReadiness(
            router_id=router.id,
            router_name=router.name,
            status=status,
            module_version=MODULE_VERSION,
            evaluated_at=evaluated_at,
            configuration_error=configuration_error,
        )

    if module is None:
        return outcome(WalledGardenReadinessStatus.not_configured)
    snapshot = _latest_snapshot(db, router.id)
    if snapshot is None:
        return outcome(WalledGardenReadinessStatus.no_snapshot)
    captured_at = _aware(snapshot.created_at)
    evaluation = evaluate_export(export_text=snapshot.config_export, module=module)
    if evaluated_at - captured_at > max_snapshot_age:
        status = WalledGardenReadinessStatus.stale
    elif evaluation.findings:
        status = WalledGardenReadinessStatus.not_ready
    else:
        status = WalledGardenReadinessStatus.ready
    return replace(
        outcome(status),
        snapshot_id=snapshot.id,
        snapshot_captured_at=captured_at,
        findings=evaluation.findings,
        legacy_rules_present=evaluation.legacy_rules_present,
        legacy_rule_count=evaluation.legacy_rule_count,
        static_suspended_enabled=evaluation.static_suspended_enabled,
        static_suspended_disabled=evaluation.static_suspended_disabled,
    )


def _log(result: WalledGardenRouterReadiness) -> None:
    logger.info(
        "walled_garden_router_readiness",
        extra={
            "event": "walled_garden_router_readiness",
            "router_id": str(result.router_id),
            "status": result.status.value,
            "finding_count": len(result.findings),
            "legacy_rule_count": result.legacy_rule_count,
        },
    )


def resolve_router_walled_garden_readiness(
    db: Session, *, query: WalledGardenReadinessQuery
) -> WalledGardenRouterReadiness:
    """Return the typed readiness of one router (read-only)."""

    router = db.get(Router, query.router_id)
    if router is None:
        raise WalledGardenReadinessError(
            WalledGardenReadinessErrorCode.ROUTER_NOT_FOUND, "Router not found"
        )
    module, error = _render_or_error(db)
    result = _evaluate_router(
        db,
        router=router,
        module=module,
        configuration_error=error,
        max_snapshot_age=query.max_snapshot_age,
        evaluated_at=_aware(query.evaluated_at or datetime.now(UTC)),
    )
    _log(result)
    return result


def resolve_fleet_walled_garden_readiness(
    db: Session, *, query: FleetWalledGardenReadinessQuery
) -> FleetWalledGardenReadiness:
    """Return readiness for every (active) router, rendered once."""

    evaluated_at = _aware(query.evaluated_at or datetime.now(UTC))
    statement = select(Router).order_by(Router.name)
    if not query.include_inactive:
        statement = statement.where(Router.is_active.is_(True))
    module, error = _render_or_error(db)
    results = tuple(
        _evaluate_router(
            db,
            router=router,
            module=module,
            configuration_error=error,
            max_snapshot_age=query.max_snapshot_age,
            evaluated_at=evaluated_at,
        )
        for router in db.scalars(statement).all()
    )
    for result in results:
        _log(result)
    return FleetWalledGardenReadiness(
        evaluated_at=evaluated_at, module_version=MODULE_VERSION, routers=results
    )


def resolve_routers_walled_garden_readiness(
    db: Session, *, query: RoutersWalledGardenReadinessQuery
) -> Mapping[uuid.UUID, WalledGardenRouterReadiness]:
    """Return readiness for the requested routers (read-only, one render)."""

    if not query.router_ids:
        return {}
    evaluated_at = _aware(query.evaluated_at or datetime.now(UTC))
    module, error = _render_or_error(db)
    routers = db.scalars(
        select(Router)
        .where(Router.id.in_(sorted(query.router_ids)))
        .order_by(Router.name)
    ).all()
    results: dict[uuid.UUID, WalledGardenRouterReadiness] = {}
    for router in routers:
        result = _evaluate_router(
            db,
            router=router,
            module=module,
            configuration_error=error,
            max_snapshot_age=query.max_snapshot_age,
            evaluated_at=evaluated_at,
        )
        _log(result)
        results[router.id] = result
    return results


__all__ = [
    "DEFAULT_MAX_SNAPSHOT_AGE",
    "FleetWalledGardenReadiness",
    "FleetWalledGardenReadinessQuery",
    "RouterOsExportEntry",
    "RoutersWalledGardenReadinessQuery",
    "WalledGardenExportEvaluation",
    "WalledGardenFinding",
    "WalledGardenFindingIssue",
    "WalledGardenReadinessError",
    "WalledGardenReadinessErrorCode",
    "WalledGardenReadinessQuery",
    "WalledGardenReadinessStatus",
    "WalledGardenRouterReadiness",
    "evaluate_export",
    "parse_routeros_export",
    "resolve_fleet_walled_garden_readiness",
    "resolve_router_walled_garden_readiness",
    "resolve_routers_walled_garden_readiness",
]
