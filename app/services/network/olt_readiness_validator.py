"""OLT Readiness Validator - Comprehensive OLT default validation.

Validates that OLT defaults have the prerequisites for ONT authorization.

Usage:
    from app.services.network.olt_readiness_validator import (
        validate_olt_readiness,
        validate_all_olts_readiness,
        OltReadinessReport,
    )

    # Validate single OLT
    report = validate_olt_readiness(db, olt_id)
    if report.is_ready:
        print("OLT is ready for authorization")
    else:
        for issue in report.blocking_issues:
            print(f"BLOCKING: {issue}")

    # Validate all OLTs
    reports = validate_all_olts_readiness(db)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class IssueSeverity(str, Enum):
    """Severity of a validation issue."""

    blocking = "blocking"  # Prevents authorization
    warning = "warning"  # May cause issues
    info = "info"  # Informational only


@dataclass
class ValidationIssue:
    """A single validation issue."""

    category: str
    message: str
    severity: IssueSeverity
    code: str | None = None
    field: str | None = None


@dataclass
class OltReadinessReport:
    """Complete readiness report for an OLT."""

    olt_id: str
    olt_name: str
    is_ready: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    connectivity_tested: bool = False
    ssh_ok: bool | None = None
    snmp_ok: bool | None = None

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        """Return only blocking issues."""
        return [i for i in self.issues if i.severity == IssueSeverity.blocking]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Return only warnings."""
        return [i for i in self.issues if i.severity == IssueSeverity.warning]

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "is_ready": self.is_ready,
            "blocking_issues": [
                {"category": i.category, "message": i.message, "code": i.code}
                for i in self.blocking_issues
            ],
            "warnings": [
                {"category": i.category, "message": i.message, "code": i.code}
                for i in self.warnings
            ],
            "connectivity": {
                "tested": self.connectivity_tested,
                "ssh_ok": self.ssh_ok,
                "snmp_ok": self.snmp_ok,
            },
        }


def validate_olt_readiness(
    db: Session,
    olt_id: str,
    *,
    test_connectivity: bool = False,
) -> OltReadinessReport:
    """Validate that an OLT is ready for ONT authorization.

    Args:
        db: Database session.
        olt_id: OLT device ID.
        test_connectivity: If True, test SSH/SNMP connectivity.

    Returns:
        OltReadinessReport with validation results.
    """
    from app.models.network import OLTDevice

    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return OltReadinessReport(
            olt_id=olt_id,
            olt_name="Unknown",
            is_ready=False,
            issues=[
                ValidationIssue(
                    category="olt",
                    message="OLT not found",
                    severity=IssueSeverity.blocking,
                    code="OLT_NOT_FOUND",
                )
            ],
        )

    report = OltReadinessReport(
        olt_id=str(olt.id),
        olt_name=olt.name or "Unnamed OLT",
    )

    # 1. Validate OLT credentials
    _validate_olt_credentials(olt, report)

    # 2. Validate OLT vendor/model
    _validate_olt_vendor_model(olt, report)

    # 3. Validate OLT authorization defaults (from config_pack JSON)
    pack = getattr(olt, "config_pack", None) or {}
    vendor = str(getattr(olt, "vendor", "") or "").lower()
    if "huawei" in vendor:
        if not pack.get("line_profile_id"):
            report.issues.append(
                ValidationIssue(
                    category="authorization",
                    message="Missing default authorization line profile ID",
                    severity=IssueSeverity.blocking,
                    code="NO_DEFAULT_LINE_PROFILE",
                    field="line_profile_id",
                )
            )
            report.is_ready = False
        if not pack.get("service_profile_id"):
            report.issues.append(
                ValidationIssue(
                    category="authorization",
                    message="Missing default authorization service profile ID",
                    severity=IssueSeverity.blocking,
                    code="NO_DEFAULT_SERVICE_PROFILE",
                    field="service_profile_id",
                )
            )
            report.is_ready = False

    if not pack.get("management_vlan_id"):
        report.issues.append(
            ValidationIssue(
                category="vlan",
                message="Missing default management VLAN",
                severity=IssueSeverity.warning,
                code="NO_DEFAULT_MGMT_VLAN",
            )
        )
    if not pack.get("internet_vlan_id"):
        report.issues.append(
            ValidationIssue(
                category="vlan",
                message="Missing default internet VLAN",
                severity=IssueSeverity.warning,
                code="NO_DEFAULT_INTERNET_VLAN",
            )
        )

    # 4. Connectivity tests (optional)
    if test_connectivity:
        report.connectivity_tested = True
        report.ssh_ok = _test_ssh_connectivity(olt)
        report.snmp_ok = _test_snmp_connectivity(olt)

        if report.ssh_ok is False:
            report.issues.append(
                ValidationIssue(
                    category="connectivity",
                    message="SSH connection test failed",
                    severity=IssueSeverity.blocking,
                    code="SSH_FAILED",
                )
            )
            report.is_ready = False

    # Final readiness check
    if report.blocking_issues:
        report.is_ready = False

    return report


def _validate_olt_credentials(olt: object, report: OltReadinessReport) -> None:
    """Validate OLT has required credentials."""
    if not getattr(olt, "mgmt_ip", None) and not getattr(olt, "hostname", None):
        report.issues.append(
            ValidationIssue(
                category="credentials",
                message="Missing management IP or hostname",
                severity=IssueSeverity.blocking,
                code="NO_MGMT_ADDRESS",
                field="mgmt_ip",
            )
        )

    if not getattr(olt, "ssh_username", None):
        report.issues.append(
            ValidationIssue(
                category="credentials",
                message="Missing SSH username",
                severity=IssueSeverity.blocking,
                code="NO_SSH_USERNAME",
                field="ssh_username",
            )
        )

    if not getattr(olt, "ssh_password", None):
        report.issues.append(
            ValidationIssue(
                category="credentials",
                message="Missing SSH password",
                severity=IssueSeverity.blocking,
                code="NO_SSH_PASSWORD",
                field="ssh_password",
            )
        )


def _validate_olt_vendor_model(olt: object, report: OltReadinessReport) -> None:
    """Validate OLT has vendor and model set."""
    if not getattr(olt, "vendor", None):
        report.issues.append(
            ValidationIssue(
                category="device",
                message="Missing vendor",
                severity=IssueSeverity.blocking,
                code="NO_VENDOR",
                field="vendor",
            )
        )

    if not getattr(olt, "model", None):
        report.issues.append(
            ValidationIssue(
                category="device",
                message="Missing model",
                severity=IssueSeverity.warning,
                code="NO_MODEL",
                field="model",
            )
        )


def _test_ssh_connectivity(olt: object) -> bool | None:
    """Test SSH connectivity to OLT."""
    try:
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        adapter = get_protocol_adapter(olt)
        result = adapter.test_connection()
        return result.success
    except Exception as exc:
        logger.warning("SSH connectivity test failed for OLT %s: %s", getattr(olt, "name", "?"), exc)
        return False


def _test_snmp_connectivity(olt: object) -> bool | None:
    """Test SNMP connectivity to OLT."""
    try:
        from app.services.network.olt_snmp import test_snmp_connection

        mgmt_ip = getattr(olt, "mgmt_ip", None) or getattr(olt, "hostname", None)
        community = getattr(olt, "snmp_community", "public")
        if not mgmt_ip:
            return None
        return test_snmp_connection(mgmt_ip, community)
    except Exception as exc:
        logger.warning("SNMP connectivity test failed for OLT %s: %s", getattr(olt, "name", "?"), exc)
        return False


def validate_all_olts_readiness(
    db: Session,
    *,
    test_connectivity: bool = False,
) -> list[OltReadinessReport]:
    """Validate all OLTs are ready for authorization.

    Args:
        db: Database session.
        test_connectivity: If True, test SSH/SNMP connectivity for each OLT.

    Returns:
        List of OltReadinessReport for each OLT.
    """
    from app.models.network import OLTDevice

    olts = db.scalars(select(OLTDevice)).all()
    reports = []

    for olt in olts:
        report = validate_olt_readiness(
            db,
            str(olt.id),
            test_connectivity=test_connectivity,
        )
        reports.append(report)

    return reports


def get_readiness_summary(reports: list[OltReadinessReport]) -> dict:
    """Generate summary statistics from readiness reports.

    Args:
        reports: List of OLT readiness reports.

    Returns:
        Summary dictionary with counts and statistics.
    """
    total = len(reports)
    ready = sum(1 for r in reports if r.is_ready)
    not_ready = total - ready

    # Count issue types
    blocking_issues = sum(len(r.blocking_issues) for r in reports)
    warnings = sum(len(r.warnings) for r in reports)

    # Group OLTs by status
    ready_olts = [r.olt_name for r in reports if r.is_ready]
    not_ready_olts = [
        {"name": r.olt_name, "issues": [i.message for i in r.blocking_issues]}
        for r in reports
        if not r.is_ready
    ]

    return {
        "total_olts": total,
        "ready_count": ready,
        "not_ready_count": not_ready,
        "ready_percentage": round(ready / total * 100, 1) if total > 0 else 0,
        "total_blocking_issues": blocking_issues,
        "total_warnings": warnings,
        "ready_olts": ready_olts,
        "not_ready_olts": not_ready_olts,
    }
