from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.billing import InvoiceStatus, TaxApplication
from app.models.catalog import SubscriptionStatus
from app.models.collections import DunningCaseStatus
from app.models.lifecycle import LifecycleEventType
from app.models.network import DeviceStatus, DeviceType, IPVersion
from app.models.network_monitoring import DeviceRole, InterfaceStatus, MetricType
from app.models.provisioning import (
    AppointmentStatus,
    ServiceOrderStatus,
    ServiceState,
    TaskStatus,
)
from app.models.snmp import SnmpAuthProtocol, SnmpPrivProtocol, SnmpVersion
from app.models.subscriber import SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.models.tr069 import Tr069Event, Tr069JobStatus
from app.models.usage import AccountingStatus, UsageSource


class CSVRowModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_csv_value(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                return None
            lowered = stripped.lower()
            if lowered in {"true", "false", "yes", "no", "1", "0"}:
                return lowered in {"true", "yes", "1"}
            return stripped
        return value


class SubscriberImportRow(CSVRowModel):
    subscriber_id: UUID
    is_active: bool = True
    notes: str | None = None


class SubscriberAccountImportRow(CSVRowModel):
    subscriber_id: UUID
    reseller_id: UUID | None = None
    account_number: str | None = Field(default=None, max_length=80)
    status: SubscriberStatus = SubscriberStatus.active
    notes: str | None = None


class SubscriberCustomFieldImportRow(CSVRowModel):
    subscriber_id: UUID
    key: str
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool = True


class SubscriptionImportRow(CSVRowModel):
    account_id: UUID
    offer_id: UUID
    offer_version_id: UUID | None = None
    service_address_id: UUID | None = None
    status: SubscriptionStatus = SubscriptionStatus.pending
    start_at: datetime | None = None
    end_at: datetime | None = None
    next_billing_at: datetime | None = None
    canceled_at: datetime | None = None
    cancel_reason: str | None = None


class CPEDeviceImportRow(CSVRowModel):
    account_id: UUID
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    device_type: DeviceType = DeviceType.ont
    status: DeviceStatus = DeviceStatus.active
    serial_number: str | None = None
    model: str | None = None
    vendor: str | None = None
    mac_address: str | None = None
    installed_at: datetime | None = None
    notes: str | None = None


class IPAssignmentImportRow(CSVRowModel):
    account_id: UUID
    subscription_id: UUID | None = None
    subscription_add_on_id: UUID | None = None
    service_address_id: UUID | None = None
    ip_version: IPVersion = IPVersion.ipv4
    ip_address: str
    prefix_length: int | None = None
    gateway: str | None = None
    dns_primary: str | None = None
    dns_secondary: str | None = None
    is_active: bool = True


class ServiceOrderImportRow(CSVRowModel):
    account_id: UUID
    subscription_id: UUID | None = None
    requested_by_contact_id: UUID | None = None
    status: ServiceOrderStatus = ServiceOrderStatus.draft
    notes: str | None = None


class InvoiceImportRow(CSVRowModel):
    account_id: UUID
    invoice_number: str | None = None
    status: InvoiceStatus = InvoiceStatus.draft
    currency: str = "NGN"
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    balance_due: Decimal = Decimal("0.00")
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None


class InvoiceLineImportRow(CSVRowModel):
    invoice_id: UUID
    description: str
    quantity: Decimal = Decimal("1.000")
    unit_price: Decimal = Decimal("0.00")
    amount: Decimal | None = None
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive
    metadata: str | None = None


class PopSiteImportRow(CSVRowModel):
    name: str
    code: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool = True


class NetworkDeviceImportRow(CSVRowModel):
    pop_site_id: UUID | None = None
    name: str
    hostname: str | None = None
    mgmt_ip: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    role: DeviceRole = DeviceRole.edge
    status: DeviceStatus = DeviceStatus.inactive
    notes: str | None = None
    is_active: bool = True


class DeviceInterfaceImportRow(CSVRowModel):
    device_id: UUID
    name: str
    description: str | None = None
    status: InterfaceStatus = InterfaceStatus.unknown
    speed_mbps: int | None = None
    mac_address: str | None = None


class DeviceMetricImportRow(CSVRowModel):
    device_id: UUID
    interface_id: UUID | None = None
    metric_type: MetricType = MetricType.custom
    value: int = 0
    unit: str | None = None
    recorded_at: datetime


class InstallAppointmentImportRow(CSVRowModel):
    service_order_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    technician: str | None = None
    status: AppointmentStatus = AppointmentStatus.proposed
    notes: str | None = None
    is_self_install: bool = False


class ProvisioningTaskImportRow(CSVRowModel):
    service_order_id: UUID
    name: str
    status: TaskStatus = TaskStatus.pending
    assigned_to: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class ServiceStateTransitionImportRow(CSVRowModel):
    service_order_id: UUID
    from_state: ServiceState | None = None
    to_state: ServiceState
    reason: str | None = None
    changed_by: str | None = None
    changed_at: datetime | None = None


class QuotaBucketImportRow(CSVRowModel):
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    included_gb: Decimal | None = None
    used_gb: Decimal = Decimal("0")
    rollover_gb: Decimal = Decimal("0")
    overage_gb: Decimal = Decimal("0")


class RadiusAccountingSessionImportRow(CSVRowModel):
    subscription_id: UUID
    access_credential_id: UUID
    radius_client_id: UUID | None = None
    nas_device_id: UUID | None = None
    session_id: str
    status_type: AccountingStatus
    session_start: datetime | None = None
    session_end: datetime | None = None
    input_octets: int | None = None
    output_octets: int | None = None
    terminate_cause: str | None = None


class UsageRecordImportRow(CSVRowModel):
    subscription_id: UUID
    quota_bucket_id: UUID | None = None
    source: UsageSource
    recorded_at: datetime
    input_gb: Decimal = Decimal("0")
    output_gb: Decimal = Decimal("0")
    total_gb: Decimal = Decimal("0")


class DunningCaseImportRow(CSVRowModel):
    account_id: UUID
    policy_set_id: UUID | None = None
    status: DunningCaseStatus = DunningCaseStatus.open
    current_step: int | None = None
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class DunningActionLogImportRow(CSVRowModel):
    case_id: UUID
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    step_day: int | None = None
    action: str
    outcome: str | None = None
    notes: str | None = None
    executed_at: datetime | None = None


class SubscriptionLifecycleEventImportRow(CSVRowModel):
    subscription_id: UUID
    event_type: LifecycleEventType = LifecycleEventType.other
    from_status: SubscriptionStatus | None = None
    to_status: SubscriptionStatus | None = None
    reason: str | None = None
    notes: str | None = None
    metadata: dict | None = None
    actor: str | None = None


class Tr069AcsServerImportRow(CSVRowModel):
    name: str
    base_url: str
    is_active: bool = True
    notes: str | None = None


class Tr069CpeDeviceImportRow(CSVRowModel):
    acs_server_id: UUID
    cpe_device_id: UUID | None = None
    serial_number: str | None = None
    oui: str | None = None
    product_class: str | None = None
    connection_request_url: str | None = None
    last_inform_at: datetime | None = None
    is_active: bool = True


class Tr069SessionImportRow(CSVRowModel):
    device_id: UUID
    event_type: Tr069Event
    request_id: str | None = None
    inform_payload: dict | None = None
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = None


class Tr069ParameterImportRow(CSVRowModel):
    device_id: UUID
    name: str
    value: str | None = None
    updated_at: datetime


class Tr069JobImportRow(CSVRowModel):
    device_id: UUID
    name: str
    command: str
    payload: dict | None = None
    status: Tr069JobStatus = Tr069JobStatus.queued
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class SnmpCredentialImportRow(CSVRowModel):
    name: str
    version: SnmpVersion
    community_hash: str | None = None
    username: str | None = None
    auth_protocol: SnmpAuthProtocol = SnmpAuthProtocol.none
    auth_secret_hash: str | None = None
    priv_protocol: SnmpPrivProtocol = SnmpPrivProtocol.none
    priv_secret_hash: str | None = None
    is_active: bool = True


class SnmpTargetImportRow(CSVRowModel):
    device_id: UUID | None = None
    hostname: str | None = None
    mgmt_ip: str | None = None
    port: int = 161
    credential_id: UUID
    is_active: bool = True
    notes: str | None = None


class SnmpOidImportRow(CSVRowModel):
    name: str
    oid: str
    unit: str | None = None
    description: str | None = None
    is_active: bool = True


class SnmpPollerImportRow(CSVRowModel):
    target_id: UUID
    oid_id: UUID
    poll_interval_sec: int = 60
    is_active: bool = True


class SnmpReadingImportRow(CSVRowModel):
    poller_id: UUID
    value: int = 0
    recorded_at: datetime


class BandwidthSampleImportRow(CSVRowModel):
    subscription_id: UUID
    device_id: UUID | None = None
    interface_id: UUID | None = None
    rx_bps: int = 0
    tx_bps: int = 0
    sample_at: datetime


class SubscriptionEngineImportRow(CSVRowModel):
    name: str
    code: str
    description: str | None = None
    is_active: bool = True


class SubscriptionEngineSettingImportRow(CSVRowModel):
    engine_id: UUID
    key: str
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | None = None
    is_secret: bool = False
