"""Provisioning enforcement — detect and fix gaps in the ONT provisioning chain.

Identifies ONTs where the database state (PPPoE credentials, ACS binding)
doesn't match the actual device state (WAN IP, TR-069 registration), then
re-runs the specific failed provisioning steps to close the gap.

Designed to run both on-demand (operator clicks a button) and periodically
(Celery beat task every 30 minutes).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.network import OLTDevice, OntUnit

logger = logging.getLogger(__name__)

# Stale runtime threshold — if observed data is older than this and ONT is
# offline, the observed_wan_ip is likely stale and should be cleared.
STALE_RUNTIME_HOURS = 24


class ProvisioningEnforcement:
    """Detect and fix provisioning gaps across the ONT fleet."""

    @staticmethod
    def detect_gaps(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, list[str]]:
        """Return ONT IDs grouped by gap category.

        Categories:
        - ``no_acs_binding``: PPPoE set but ONT not bound to ACS (OLT has ACS)
        - ``no_acs_on_olt``: ONT's OLT has no ACS server configured at all
        - ``pppoe_not_pushed``: Online, ACS-bound, PPPoE set but no WAN IP
        - ``stale_wan_ip``: Offline with WAN IP older than STALE_RUNTIME_HOURS

        Args:
            db: Database session.
            olt_id: Optional filter to a single OLT.
        """
        from app.models.network import OnuOnlineStatus

        base = (
            select(OntUnit)
            .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
            .where(OntUnit.is_active.is_(True))
        )
        if olt_id:
            base = base.where(OntUnit.olt_device_id == olt_id)

        gaps: dict[str, list[str]] = {
            "no_acs_binding": [],
            "no_acs_on_olt": [],
            "pppoe_not_pushed": [],
            "stale_wan_ip": [],
        }

        # 1. No ACS binding — OLT has ACS but ONT doesn't
        stmt = base.where(
            OntUnit.pppoe_username.isnot(None),
            OntUnit.tr069_acs_server_id.is_(None),
            OLTDevice.tr069_acs_server_id.isnot(None),
        )
        for ont in db.scalars(stmt).all():
            gaps["no_acs_binding"].append(str(ont.id))

        # 2. No ACS on OLT — ONT's OLT has no ACS configured
        stmt = base.where(
            OntUnit.pppoe_username.isnot(None),
            OLTDevice.tr069_acs_server_id.is_(None),
        )
        for ont in db.scalars(stmt).all():
            gaps["no_acs_on_olt"].append(str(ont.id))

        # 3. PPPoE not pushed — online, ACS-bound, has creds, but no WAN IP
        stmt = base.where(
            OntUnit.pppoe_username.isnot(None),
            OntUnit.tr069_acs_server_id.isnot(None),
            OntUnit.observed_wan_ip.is_(None),
            OntUnit.effective_status == OnuOnlineStatus.online,
        )
        for ont in db.scalars(stmt).all():
            gaps["pppoe_not_pushed"].append(str(ont.id))

        # 4. Stale WAN IP — offline with old runtime data
        stale_cutoff = datetime.now(UTC) - timedelta(hours=STALE_RUNTIME_HOURS)
        stmt = base.where(
            OntUnit.observed_wan_ip.isnot(None),
            OntUnit.effective_status.in_(
                [
                    OnuOnlineStatus.offline,
                ]
            ),
            and_(
                OntUnit.observed_runtime_updated_at.isnot(None),
                OntUnit.observed_runtime_updated_at < stale_cutoff,
            ),
        )
        for ont in db.scalars(stmt).all():
            gaps["stale_wan_ip"].append(str(ont.id))

        return gaps

    @staticmethod
    def detect_gap_counts(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, int]:
        """Return gap counts without loading full ONT objects (fast)."""
        from app.models.network import OnuOnlineStatus

        base_where: list[ColumnElement[bool]] = [OntUnit.is_active.is_(True)]
        if olt_id:
            base_where.append(OntUnit.olt_device_id == olt_id)

        counts: dict[str, int] = {}

        # No ACS binding
        counts["no_acs_binding"] = (
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
                .where(
                    *base_where,
                    OntUnit.pppoe_username.isnot(None),
                    OntUnit.tr069_acs_server_id.is_(None),
                    OLTDevice.tr069_acs_server_id.isnot(None),
                )
            )
            or 0
        )

        # No ACS on OLT
        counts["no_acs_on_olt"] = (
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
                .where(
                    *base_where,
                    OntUnit.pppoe_username.isnot(None),
                    OLTDevice.tr069_acs_server_id.is_(None),
                )
            )
            or 0
        )

        # PPPoE not pushed
        counts["pppoe_not_pushed"] = (
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
                .where(
                    *base_where,
                    OntUnit.pppoe_username.isnot(None),
                    OntUnit.tr069_acs_server_id.isnot(None),
                    OntUnit.observed_wan_ip.is_(None),
                    OntUnit.effective_status == OnuOnlineStatus.online,
                )
            )
            or 0
        )

        # Stale WAN IP
        stale_cutoff = datetime.now(UTC) - timedelta(hours=STALE_RUNTIME_HOURS)
        counts["stale_wan_ip"] = (
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .where(
                    *base_where,
                    OntUnit.observed_wan_ip.isnot(None),
                    OntUnit.effective_status == OnuOnlineStatus.offline,
                    OntUnit.observed_runtime_updated_at.isnot(None),
                    OntUnit.observed_runtime_updated_at < stale_cutoff,
                )
            )
            or 0
        )

        return counts

    @staticmethod
    def enforce_acs_binding(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Propagate OLT's ACS server to specified ONTs.

        Only updates ONTs whose OLT actually has an ACS server configured.
        """
        updated = 0
        skipped = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont or not ont.olt_device_id:
                skipped += 1
                continue
            olt = db.get(OLTDevice, str(ont.olt_device_id))
            if not olt or not olt.tr069_acs_server_id:
                skipped += 1
                continue
            ont.tr069_acs_server_id = olt.tr069_acs_server_id
            updated += 1

        if updated:
            db.commit()
            logger.info("ACS enforcement: bound %d ONTs, skipped %d", updated, skipped)
        return {"updated": updated, "skipped": skipped}

    @staticmethod
    def enforce_connection_request(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Send connection requests to force TR-069 bootstrap."""
        from app.services.network.ont_action_network import send_connection_request

        sent = 0
        failed = 0
        for ont_id in ont_ids:
            try:
                result = send_connection_request(db, ont_id)
                if result.success:
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.warning(
                    "Connection request failed for ONT %s: %s",
                    ont_id,
                    exc,
                )
                failed += 1
        logger.info(
            "Connection request enforcement: sent %d, failed %d",
            sent,
            failed,
        )
        return {"sent": sent, "failed": failed}

    @staticmethod
    def enforce_pppoe_push(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Re-push PPPoE credentials to ONTs via TR-069."""
        from app.services.credential_crypto import decrypt_credential
        from app.services.network.ont_action_network import set_pppoe_credentials

        pushed = 0
        failed = 0
        skipped = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont or not ont.pppoe_username:
                skipped += 1
                continue

            # Decrypt stored password
            password = ""
            if ont.pppoe_password:
                try:
                    password = decrypt_credential(ont.pppoe_password) or ""
                except ValueError:
                    logger.warning(
                        "Cannot decrypt PPPoE password for ONT %s, skipping",
                        ont.serial_number,
                    )
                    failed += 1
                    continue

            if not password:
                # Try to find password from AccessCredential
                password = _resolve_access_credential_password(db, ont)

            if not password:
                logger.warning(
                    "No PPPoE password available for ONT %s, skipping push",
                    ont.serial_number,
                )
                skipped += 1
                continue

            try:
                result = set_pppoe_credentials(
                    db,
                    ont_id,
                    ont.pppoe_username,
                    password,
                )
                if result.success:
                    pushed += 1
                else:
                    logger.warning(
                        "PPPoE push failed for ONT %s: %s",
                        ont.serial_number,
                        result.message,
                    )
                    failed += 1
            except Exception as exc:
                logger.warning(
                    "PPPoE push error for ONT %s: %s",
                    ont.serial_number,
                    exc,
                )
                failed += 1

        logger.info(
            "PPPoE enforcement: pushed %d, failed %d, skipped %d",
            pushed,
            failed,
            skipped,
        )
        return {"pushed": pushed, "failed": failed, "skipped": skipped}

    @staticmethod
    def clear_stale_runtime(
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, int]:
        """Clear stale observed_wan_ip on offline ONTs."""
        cleared = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if not ont:
                continue
            ont.observed_wan_ip = None
            ont.observed_pppoe_status = None
            cleared += 1

        if cleared:
            db.commit()
            logger.info("Cleared stale runtime data on %d ONTs", cleared)
        return {"cleared": cleared}

    @staticmethod
    def run_full_enforcement(
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> dict[str, Any]:
        """Detect provisioning gaps without mutating live device state."""
        gaps = ProvisioningEnforcement.detect_gaps(db, olt_id=olt_id)

        stats: dict[str, Any] = {
            "gaps_detected": {k: len(v) for k, v in gaps.items()},
            "remediation_performed": False,
        }

        logger.info("Full enforcement complete: %s", stats)
        return stats


def _resolve_access_credential_password(db: Session, ont: OntUnit) -> str:
    """Try to find the PPPoE password from the subscriber's AccessCredential."""
    from app.models.catalog import AccessCredential

    if not ont.pppoe_username:
        return ""

    try:
        from sqlalchemy import select as sa_select

        stmt = sa_select(AccessCredential).where(
            AccessCredential.username == ont.pppoe_username,
            AccessCredential.is_active.is_(True),
        )
        cred = db.scalars(stmt).first()
        if cred and cred.secret_hash:
            from app.services.credential_crypto import decrypt_credential

            return decrypt_credential(cred.secret_hash) or ""
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning(
            "Could not resolve AccessCredential for ONT %s (username %s): %s",
            ont.serial_number,
            ont.pppoe_username,
            exc,
            exc_info=True,
        )
    return ""
