"""Check OLT config completeness across all OLTs.

This is a one-time diagnostic script to identify OLTs that are missing
required configuration values for ONT provisioning.

Required for authorization:
- line_profile_id
- service_profile_id

Required for internet service:
- internet_vlan (with tag)
- internet_gem_index

Required for ACS management:
- management_vlan (with tag)
- mgmt_ip_pool_id with routable IPv4 pool/gateway

Required for TR-069:
- tr069_olt_profile_id
- tr069_acs_server_id

Required for QoS:
- mgmt_traffic_table_inbound
- mgmt_traffic_table_outbound
- internet_traffic_table_inbound
- internet_traffic_table_outbound

Usage:
    poetry run python -m scripts.migration.check_olt_config_completeness
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def check_olt_config_completeness() -> None:
    """Check all OLTs for config completeness."""
    logger.info("=" * 60)
    logger.info("OLT Config Completeness Check")
    logger.info("=" * 60)

    with dotmac_session() as db:
        from app.models.network import OLTDevice
        from app.services.network.olt_config_pack import (
            validate_config_pack_comprehensive,
        )

        olts = db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()

        logger.info(f"Checking {len(olts)} active OLTs...\n")

        complete_count = 0
        incomplete_olts = []
        warning_olts = []

        for olt in olts:
            validation = validate_config_pack_comprehensive(db, olt.id)
            if validation.is_valid:
                complete_count += 1
            else:
                incomplete_olts.append((olt.name, validation.errors))
            if validation.warnings:
                warning_olts.append((olt.name, validation.warnings))

        # Report results
        logger.info(f"Complete: {complete_count}/{len(olts)} OLTs")
        logger.info(f"Incomplete: {len(incomplete_olts)}/{len(olts)} OLTs")
        logger.info("")

        if incomplete_olts:
            logger.info("=" * 60)
            logger.info("INCOMPLETE OLTs")
            logger.info("=" * 60)
            for olt_name, errors in incomplete_olts:
                logger.info(f"\n{olt_name}:")
                for error in errors:
                    logger.info(f"  ERROR: {error}")

        if warning_olts:
            logger.info("")
            logger.info("=" * 60)
            logger.info("OLT CONFIG WARNINGS")
            logger.info("=" * 60)
            for olt_name, warnings in warning_olts:
                logger.info(f"\n{olt_name}:")
                for warning in warnings:
                    logger.info(f"  WARNING: {warning}")

        if not incomplete_olts:
            logger.info("\nAll OLTs have complete config!")
            sys.exit(0)
        else:
            logger.info(
                f"\nAction required: Configure missing values for {len(incomplete_olts)} OLT(s)"
            )
            sys.exit(1)


if __name__ == "__main__":
    check_olt_config_completeness()
