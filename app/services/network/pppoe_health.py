"""PPPoE connectivity health classification for the ONT fleet.

Classifies each online ONT into a PPPoE health category based on
credential state, ACS registration, and observed WAN IP. Used by
the ONT Fleet page to surface connectivity issues for NOC operators.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, func, literal_column, select

from app.models.catalog import AccessCredential
from app.models.network import OntAssignment, OntUnit, OnuOnlineStatus
from app.models.tr069 import Tr069CpeDevice
from app.services.common import coerce_uuid

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Health categories
# ---------------------------------------------------------------------------

CATEGORY_OK = "ok"
CATEGORY_NO_CREDENTIAL = "no_credential"
CATEGORY_NOT_IN_ACS = "not_in_acs"
CATEGORY_CREDENTIAL_MISMATCH = "credential_mismatch"
CATEGORY_NO_WAN_IP = "no_wan_ip"
CATEGORY_BRIDGE_MODE = "bridge_mode"
CATEGORY_UNASSIGNED = "unassigned"

ISSUE_CATEGORIES = frozenset(
    {
        CATEGORY_NO_CREDENTIAL,
        CATEGORY_NOT_IN_ACS,
        CATEGORY_CREDENTIAL_MISMATCH,
        CATEGORY_NO_WAN_IP,
    }
)

CATEGORY_DISPLAY: dict[str, str] = {
    CATEGORY_OK: "OK",
    CATEGORY_NO_CREDENTIAL: "No Credential",
    CATEGORY_NOT_IN_ACS: "Not in ACS",
    CATEGORY_CREDENTIAL_MISMATCH: "Mismatch",
    CATEGORY_NO_WAN_IP: "No WAN IP",
    CATEGORY_BRIDGE_MODE: "Bridge/DHCP",
    CATEGORY_UNASSIGNED: "Unassigned",
}

# Tailwind badge classes — full strings to survive PurgeCSS.
CATEGORY_CLASSES: dict[str, str] = {
    CATEGORY_OK: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    CATEGORY_NO_CREDENTIAL: "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    CATEGORY_NOT_IN_ACS: "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    CATEGORY_CREDENTIAL_MISMATCH: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
    CATEGORY_NO_WAN_IP: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
    CATEGORY_BRIDGE_MODE: "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-200",
    CATEGORY_UNASSIGNED: "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-400",
}


@dataclass(frozen=True, slots=True)
class PppoeHealthInfo:
    """Classification result for a single ONT."""

    category: str
    category_display: str
    category_class: str
    credential_username: str | None
    ont_pppoe_username: str | None
    has_tr069: bool
    has_wan_ip: bool


# ---------------------------------------------------------------------------
# Core query builder (shared by all public methods)
# ---------------------------------------------------------------------------


def _health_base_query() -> Any:
    """Build the base SELECT + FROM with all required left-joins.

    Returns the select statement so callers can add WHERE/GROUP BY clauses.
    """
    stmt = (
        select(
            OntUnit.id.label("ont_id"),
            OntAssignment.subscriber_id.label("subscriber_id"),
            AccessCredential.username.label("credential_username"),
            AccessCredential.id.label("credential_id"),
            OntUnit.pppoe_username.label("ont_pppoe_username"),
            OntUnit.observed_wan_ip.label("observed_wan_ip"),
            Tr069CpeDevice.id.label("tr069_device_id"),
        )
        .outerjoin(
            OntAssignment,
            and_(
                OntAssignment.ont_unit_id == OntUnit.id,
                OntAssignment.active.is_(True),
            ),
        )
        .outerjoin(
            AccessCredential,
            and_(
                AccessCredential.subscriber_id == OntAssignment.subscriber_id,
                AccessCredential.is_active.is_(True),
            ),
        )
        .outerjoin(
            Tr069CpeDevice,
            and_(
                Tr069CpeDevice.ont_unit_id == OntUnit.id,
                Tr069CpeDevice.is_active.is_(True),
            ),
        )
    )
    return stmt


_NO_WAN_IP_VALUES = ("", "0.0.0.0")  # nosec B104  # noqa: S104


def _classify_row(
    subscriber_id: object,
    credential_username: str | None,
    credential_id: object,
    ont_pppoe_username: str | None,
    observed_wan_ip: str | None,
    tr069_device_id: object,
) -> str:
    """Derive the PPPoE health category from a single joined row."""
    has_assignment = subscriber_id is not None
    has_credential = credential_id is not None
    has_tr069 = tr069_device_id is not None
    has_wan_ip = bool(observed_wan_ip) and observed_wan_ip not in _NO_WAN_IP_VALUES

    if not has_assignment:
        return CATEGORY_UNASSIGNED

    if not has_credential:
        if has_wan_ip:
            return CATEGORY_BRIDGE_MODE
        return CATEGORY_NO_CREDENTIAL

    # Has credential — check for mismatch
    if (
        ont_pppoe_username
        and credential_username
        and ont_pppoe_username != credential_username
    ):
        return CATEGORY_CREDENTIAL_MISMATCH

    if has_wan_ip:
        return CATEGORY_OK

    if not has_tr069:
        return CATEGORY_NOT_IN_ACS

    return CATEGORY_NO_WAN_IP


def _build_info(
    category: str,
    credential_username: str | None,
    ont_pppoe_username: str | None,
    tr069_device_id: object,
    observed_wan_ip: str | None,
) -> PppoeHealthInfo:
    return PppoeHealthInfo(
        category=category,
        category_display=CATEGORY_DISPLAY.get(category, category),
        category_class=CATEGORY_CLASSES.get(
            category, CATEGORY_CLASSES[CATEGORY_UNASSIGNED]
        ),
        credential_username=credential_username,
        ont_pppoe_username=ont_pppoe_username,
        has_tr069=tr069_device_id is not None,
        has_wan_ip=bool(observed_wan_ip),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PppoeHealthClassifier:
    """PPPoE connectivity health classification for the ONT fleet."""

    @staticmethod
    def classify_fleet(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, PppoeHealthInfo]:
        """Classify PPPoE health for a batch of ONTs.

        Args:
            db: Database session.
            ont_ids: OntUnit IDs to classify (typically one page of results).

        Returns:
            Dict keyed by ONT ID string → PppoeHealthInfo.
        """
        if not ont_ids:
            return {}

        stmt = _health_base_query().where(OntUnit.id.in_(ont_ids))
        rows = db.execute(stmt).all()

        result: dict[str, PppoeHealthInfo] = {}
        for row in rows:
            ont_id_str = str(row.ont_id)
            category = _classify_row(
                row.subscriber_id,
                row.credential_username,
                row.credential_id,
                row.ont_pppoe_username,
                row.observed_wan_ip,
                row.tr069_device_id,
            )
            result[ont_id_str] = _build_info(
                category,
                row.credential_username,
                row.ont_pppoe_username,
                row.tr069_device_id,
                row.observed_wan_ip,
            )
        return result

    @staticmethod
    def count_issues(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> int:
        """Count online, assigned ONTs with a PPPoE issue.

        This powers the stat card number. Only counts ONTs that are online
        and assigned to a subscriber — unassigned ONTs are not issues.
        """
        # Helper: consider observed_wan_ip valid only if non-null and not 0.0.0.0
        _has_wan = and_(
            OntUnit.observed_wan_ip.isnot(None),
            OntUnit.observed_wan_ip != "",
            OntUnit.observed_wan_ip != "0.0.0.0",  # nosec B104  # noqa: S104
        )

        # Build a SQL CASE expression for the category so we can filter in DB.
        category_expr = case(
            # No assignment → not an issue (excluded by WHERE below)
            (OntAssignment.subscriber_id.is_(None), literal_column("'unassigned'")),
            # No credential + WAN IP → bridge mode (not an issue)
            (
                and_(AccessCredential.id.is_(None), _has_wan),
                literal_column("'bridge_mode'"),
            ),
            # No credential → issue
            (AccessCredential.id.is_(None), literal_column("'no_credential'")),
            # Credential mismatch: ONT has a different username configured
            (
                and_(
                    OntUnit.pppoe_username.isnot(None),
                    OntUnit.pppoe_username != "",
                    OntUnit.pppoe_username != AccessCredential.username,
                ),
                literal_column("'credential_mismatch'"),
            ),
            # Has WAN IP → ok
            (_has_wan, literal_column("'ok'")),
            # Not in ACS → issue
            (Tr069CpeDevice.id.is_(None), literal_column("'not_in_acs'")),
            # Else: in ACS but no WAN IP → issue
            else_=literal_column("'no_wan_ip'"),
        )

        stmt = (
            select(func.count())
            .select_from(OntUnit)
            .outerjoin(
                OntAssignment,
                and_(
                    OntAssignment.ont_unit_id == OntUnit.id,
                    OntAssignment.active.is_(True),
                ),
            )
            .outerjoin(
                AccessCredential,
                and_(
                    AccessCredential.subscriber_id == OntAssignment.subscriber_id,
                    AccessCredential.is_active.is_(True),
                ),
            )
            .outerjoin(
                Tr069CpeDevice,
                and_(
                    Tr069CpeDevice.ont_unit_id == OntUnit.id,
                    Tr069CpeDevice.is_active.is_(True),
                ),
            )
            .where(OntUnit.online_status == OnuOnlineStatus.online)
            .where(OntUnit.is_active.is_(True))
            .where(OntAssignment.subscriber_id.isnot(None))
            .where(
                category_expr.in_(
                    [
                        "no_credential",
                        "not_in_acs",
                        "credential_mismatch",
                        "no_wan_ip",
                    ]
                )
            )
        )

        if olt_id:
            from app.models.network import PonPort

            stmt = stmt.outerjoin(
                PonPort, PonPort.id == OntAssignment.pon_port_id
            ).where(
                func.coalesce(PonPort.olt_id, OntUnit.olt_device_id)
                == coerce_uuid(olt_id)
            )

        return db.scalar(stmt) or 0

    @staticmethod
    def list_ont_ids_by_health(
        db: Session,
        category: str,
        *,
        olt_id: str | None = None,
    ) -> list[str]:
        """Return ONT IDs matching a specific PPPoE health category.

        When category is ``"issues"``, returns all ONTs with any issue.
        """
        stmt = _health_base_query().where(
            OntUnit.online_status == OnuOnlineStatus.online,
            OntUnit.is_active.is_(True),
        )

        if olt_id:
            from app.models.network import PonPort

            stmt = stmt.outerjoin(
                PonPort, PonPort.id == OntAssignment.pon_port_id
            ).where(
                func.coalesce(PonPort.olt_id, OntUnit.olt_device_id)
                == coerce_uuid(olt_id)
            )

        rows = db.execute(stmt).all()

        target_categories = ISSUE_CATEGORIES if category == "issues" else {category}

        matching_ids: list[str] = []
        for row in rows:
            cat = _classify_row(
                row.subscriber_id,
                row.credential_username,
                row.credential_id,
                row.ont_pppoe_username,
                row.observed_wan_ip,
                row.tr069_device_id,
            )
            if cat in target_categories:
                matching_ids.append(str(row.ont_id))

        return matching_ids
