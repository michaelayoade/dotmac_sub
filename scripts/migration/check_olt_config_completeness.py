"""Check OLT config completeness across all OLTs.

This is a one-time diagnostic script to identify OLTs that are missing
required configuration values for ONT provisioning.

Required for authorization:
- line_profile_id
- service_profile_id

Required for internet service:
- internet_vlan (with tag)
- internet_gem_index

Required for management:
- management_vlan (with tag)
- mgmt_gem_index

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


REQUIRED_FIELDS = {
    "authorization": ["line_profile_id", "service_profile_id"],
    "internet": ["internet_vlan_tag", "internet_gem_index"],
    "management": ["management_vlan_tag", "mgmt_gem_index"],
    "tr069": ["tr069_olt_profile_id", "tr069_acs_server_id"],
    "qos": [
        "mgmt_traffic_table_inbound",
        "mgmt_traffic_table_outbound",
        "internet_traffic_table_inbound",
        "internet_traffic_table_outbound",
    ],
}


def check_olt_config_completeness() -> None:
    """Check all OLTs for config completeness."""
    logger.info("=" * 60)
    logger.info("OLT Config Completeness Check")
    logger.info("=" * 60)

    with dotmac_session() as db:
        from app.models.network import OLTDevice
        from app.services.network.olt_config_pack import resolve_olt_config_pack

        olts = db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()

        logger.info(f"Checking {len(olts)} active OLTs...\n")

        complete_count = 0
        incomplete_olts = []

        for olt in olts:
            config = resolve_olt_config_pack(db, olt.id)
            missing = {}

            # Check authorization
            auth_missing = []
            if config.line_profile_id is None:
                auth_missing.append("line_profile_id")
            if config.service_profile_id is None:
                auth_missing.append("service_profile_id")
            if auth_missing:
                missing["authorization"] = auth_missing

            # Check internet
            internet_missing = []
            if config.internet_vlan.tag is None:
                internet_missing.append("internet_vlan")
            if internet_missing:
                missing["internet"] = internet_missing

            # Check management
            mgmt_missing = []
            if config.management_vlan.tag is None:
                mgmt_missing.append("management_vlan")
            if mgmt_missing:
                missing["management"] = mgmt_missing

            # Check TR-069
            tr069_missing = []
            if config.tr069_olt_profile_id is None:
                tr069_missing.append("tr069_olt_profile_id")
            if config.tr069_acs_server_id is None:
                tr069_missing.append("tr069_acs_server_id")
            if tr069_missing:
                missing["tr069"] = tr069_missing

            # Check QoS traffic tables
            qos_missing = []
            if config.mgmt_traffic_table_inbound is None:
                qos_missing.append("mgmt_traffic_table_inbound")
            if config.mgmt_traffic_table_outbound is None:
                qos_missing.append("mgmt_traffic_table_outbound")
            if config.internet_traffic_table_inbound is None:
                qos_missing.append("internet_traffic_table_inbound")
            if config.internet_traffic_table_outbound is None:
                qos_missing.append("internet_traffic_table_outbound")
            if qos_missing:
                missing["qos"] = qos_missing

            if missing:
                incomplete_olts.append((olt.name, missing))
            else:
                complete_count += 1

        # Report results
        logger.info(f"Complete: {complete_count}/{len(olts)} OLTs")
        logger.info(f"Incomplete: {len(incomplete_olts)}/{len(olts)} OLTs")
        logger.info("")

        if incomplete_olts:
            logger.info("=" * 60)
            logger.info("INCOMPLETE OLTs")
            logger.info("=" * 60)
            for olt_name, missing in incomplete_olts:
                logger.info(f"\n{olt_name}:")
                for category, fields in missing.items():
                    logger.info(f"  {category}: {', '.join(fields)}")

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
