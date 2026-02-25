from app.models.analytics import (  # noqa: F401
    KPIAggregate,
    KPIConfig,
)
from app.models.audit import AuditActorType, AuditEvent  # noqa: F401
from app.models.auth import ApiKey, MFAMethod, Session, UserCredential  # noqa: F401
from app.models.bandwidth import BandwidthSample, QueueMapping  # noqa: F401
from app.models.billing import (  # noqa: F401
    BankAccount,
    BankAccountType,
    BankReconciliationItem,
    BankReconciliationRun,
    BillingRun,
    BillingRunSchedule,
    BillingRunStatus,
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentMethod,
    PaymentMethodType,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (  # noqa: F401
    AccessCredential,
    AccessType,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    CatalogOffer,
    ConfigBackupMethod,
    ConnectionType,
    ContractTerm,
    DiscountType,
    DunningAction,
    ExecutionMethod,
    HealthStatus,
    NasConfigBackup,
    NasConnectionRule,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    OfferAddOn,
    OfferPrice,
    OfferRadiusProfile,
    OfferStatus,
    OfferVersion,
    OfferVersionPrice,
    PlanCategory,
    PolicyDunningStep,
    PolicySet,
    PriceBasis,
    PriceType,
    PriceUnit,
    ProrationPolicy,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
    ProvisioningTemplate,
    RadiusAttribute,
    RadiusProfile,
    RefundPolicy,
    RegionZone,
    ServiceType,
    SlaProfile,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    SuspensionAction,
    UsageAllowance,
)
from app.models.collections import (  # noqa: F401
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
)
from app.models.comms import (  # noqa: F401
    CustomerNotificationEvent,
    CustomerNotificationStatus,
    Survey,
)
from app.models.connector import (  # noqa: F401
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorType,
)
from app.models.contracts import ContractSignature  # noqa: F401
from app.models.domain_settings import (  # noqa: F401
    DomainSetting,
    SettingDomain,
)
from app.models.event_store import (  # noqa: F401
    EventStatus,
    EventStore,
)
from app.models.external import (  # noqa: F401
    ExternalEntityType,
    ExternalReference,
)
from app.models.fiber_change_request import (  # noqa: F401
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.fup import (  # noqa: F401
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupPolicy,
    FupRule,
)
from app.models.gis import (  # noqa: F401
    GeoArea,
    GeoAreaType,
    GeoLayer,
    GeoLayerSource,
    GeoLayerType,
    GeoLocation,
    GeoLocationType,
    ServiceBuilding,
)
from app.models.integration import (  # noqa: F401
    IntegrationJob,
    IntegrationJobType,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.models.integration_hook import (  # noqa: F401
    IntegrationHook,
    IntegrationHookAuthType,
    IntegrationHookExecution,
    IntegrationHookExecutionStatus,
    IntegrationHookType,
)
from app.models.legal import (  # noqa: F401
    LegalDocument,
    LegalDocumentType,
)
from app.models.lifecycle import (  # noqa: F401
    LifecycleEventType,
    SubscriptionLifecycleEvent,
)
from app.models.network import (  # noqa: F401
    ConfigMethod,
    CPEDevice,
    DeviceStatus,
    DeviceType,
    FdhCabinet,
    FiberAccessPoint,
    FiberEndpointType,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    GponChannel,
    HardwareUnitStatus,
    IPAssignment,
    IpBlock,
    IpPool,
    IpProtocol,
    IPv4Address,
    IPv6Address,
    IPVersion,
    MgmtIpMode,
    NetworkZone,
    ODNEndpointType,
    OltCard,
    OltCardPort,
    OltConfigBackup,
    OltConfigBackupType,
    OLTDevice,
    OltPortType,
    OltPowerUnit,
    OltSfpModule,
    OltShelf,
    OntAssignment,
    OntUnit,
    OnuCapability,
    OnuMode,
    OnuOfflineReason,
    OnuOnlineStatus,
    OnuType,
    PonPort,
    PonPortSplitterLink,
    PonType,
    Port,
    PortStatus,
    PortType,
    PortVlan,
    SpeedProfile,
    SpeedProfileDirection,
    SpeedProfileType,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
    Vlan,
    WanMode,
)
from app.models.network_monitoring import (  # noqa: F401
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DnsThreatAction,
    DnsThreatEvent,
    DnsThreatSeverity,
    InterfaceStatus,
    MetricType,
    NetworkDevice,
    NetworkDeviceBandwidthGraph,
    NetworkDeviceBandwidthGraphSource,
    NetworkDeviceSnmpOid,
    PopSite,
    PopSiteContact,
    SpeedTestResult,
    SpeedTestSource,
)
from app.models.network_monitoring import (  # noqa: F401
    DeviceStatus as MonitoringDeviceStatus,
)
from app.models.network_monitoring import (  # noqa: F401
    DeviceType as MonitoringDeviceType,
)
from app.models.notification import (  # noqa: F401
    AlertNotificationLog,
    AlertNotificationPolicy,
    AlertNotificationPolicyStep,
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    NotificationTemplate,
    OnCallRotation,
    OnCallRotationMember,
)
from app.models.oauth_token import OAuthToken  # noqa: F401
from app.models.payment_arrangement import (  # noqa: F401
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentArrangementInstallment,
    PaymentFrequency,
)
from app.models.provisioning import (  # noqa: F401
    AppointmentStatus,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningStep,
    ProvisioningStepType,
    ProvisioningTask,
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
    ServiceState,
    ServiceStateTransition,
    TaskStatus,
)
from app.models.qualification import (  # noqa: F401
    BuildoutMilestone,
    BuildoutMilestoneStatus,
    BuildoutProject,
    BuildoutProjectStatus,
    BuildoutRequest,
    BuildoutRequestStatus,
    BuildoutStatus,
    BuildoutUpdate,
    CoverageArea,
    QualificationStatus,
    ServiceQualification,
)
from app.models.radius import (  # noqa: F401
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.rbac import (  # noqa: F401
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.scheduler import ScheduledTask, ScheduleType  # noqa: F401
from app.models.snmp import (  # noqa: F401
    SnmpAuthProtocol,
    SnmpCredential,
    SnmpOid,
    SnmpPoller,
    SnmpPrivProtocol,
    SnmpReading,
    SnmpTarget,
    SnmpVersion,
)
from app.models.splynx_archive import (  # noqa: F401
    SplynxArchivedQuote,
    SplynxArchivedQuoteItem,
    SplynxArchivedTicket,
    SplynxArchivedTicketMessage,
)
from app.models.stored_file import StoredFile  # noqa: F401
from app.models.subscriber import (  # noqa: F401
    Address,
    AddressType,
    ChannelType,
    ContactMethod,
    Gender,
    Organization,
    Reseller,
    Subscriber,
    SubscriberChannel,
    SubscriberCustomField,
    SubscriberStatus,
)
from app.models.subscription_change import (  # noqa: F401
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_engine import (  # noqa: F401
    SettingValueType,
    SubscriptionEngine,
    SubscriptionEngineSetting,
)
from app.models.system_user import SystemUser  # noqa: F401
from app.models.table_column_config import TableColumnConfig  # noqa: F401
from app.models.table_column_default_config import (  # noqa: F401
    TableColumnDefaultConfig,
)
from app.models.tr069 import (  # noqa: F401
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Event,
    Tr069Job,
    Tr069JobStatus,
    Tr069Parameter,
    Tr069Session,
)
from app.models.usage import (  # noqa: F401
    AccountingStatus,
    QuotaBucket,
    RadiusAccountingSession,
    UsageCharge,
    UsageChargeStatus,
    UsageRatingRun,
    UsageRatingRunStatus,
    UsageRecord,
    UsageSource,
)
from app.models.webhook import (  # noqa: F401
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    WebhookEventType,
    WebhookSubscription,
)
from app.models.wireguard import (  # noqa: F401
    WireGuardConnectionLog,
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServer,
)
