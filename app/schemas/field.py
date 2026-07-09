from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeviceTokenRegister(BaseModel):
    platform: str = Field(min_length=1, max_length=20)
    fcm_token: str = Field(min_length=1, max_length=512)
    app_version: str | None = Field(default=None, max_length=40)


class DeviceTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID | None = None
    system_user_id: UUID | None = None
    platform: str | None = None
    app_version: str | None = None
    is_active: bool
    created_at: datetime
    last_seen_at: datetime


class FieldMeResponse(BaseModel):
    person_id: UUID
    name: str
    email: str | None = None
    technician_title: str | None = None
    region: str | None = None
    open_jobs: int
    completed_today: int


class FieldJobSummary(BaseModel):
    """Technician job-list item sourced from the CRM work-order mirror."""

    id: str
    work_order_mirror_id: UUID
    title: str
    description: str | None = None
    status: str
    priority: str | None = None
    work_type: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_duration_minutes: int | None = None
    estimated_arrival_at: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_at: datetime | None = None
    total_active_seconds: int | None = None
    technician_name: str | None = None
    technician_phone: str | None = None
    address: str | None = None
    tags: list[str] = Field(default_factory=list)


class FieldCustomer(BaseModel):
    subscriber_id: UUID
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    address_text: str | None = None
    service_plan: str | None = None
    account_number: str | None = None
    status: str | None = None


class FieldJobLocation(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None
    source: str


class FieldJobDestination(BaseModel):
    destination_type: str = Field(min_length=1, max_length=40)
    destination_id: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=255)
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None


class FieldJobDestinationsResponse(BaseModel):
    items: list[FieldJobDestination]
    count: int


class FieldSiteContact(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    relationship: str | None = None


class FieldVisitHistoryItem(BaseModel):
    work_order_id: str
    title: str
    work_type: str | None = None
    status: str | None = None
    completed_at: datetime | None = None


class FieldOpenTicketItem(BaseModel):
    id: str
    ref: str
    subject: str | None = None
    status: str | None = None


class FieldAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    crm_work_order_id: str
    note_id: UUID | None = None
    kind: str
    file_name: str
    mime_type: str
    size_bytes: int
    latitude: float | None = None
    longitude: float | None = None
    captured_at: datetime | None = None
    signer_name: str | None = None
    uploaded_by_person_id: UUID
    uploaded_by_system_user_id: UUID | None = None
    client_ref: UUID | None = None
    asset_type: str | None = None
    asset_id: UUID | None = None
    created_at: datetime
    download_path: str


class FieldNoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    is_internal: bool = True
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)


class FieldNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    body: str
    is_internal: bool
    author_person_id: UUID | None = None
    author_name: str | None = None
    created_at: datetime
    attachments: list[FieldAttachmentRead] = Field(default_factory=list)


class FieldWorkLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    person_id: UUID
    start_at: datetime
    end_at: datetime | None = None
    minutes: int
    notes: str | None = None


class FieldWorkLogEntry(BaseModel):
    start_at: datetime
    end_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    client_ref: UUID | None = None


class FieldWorkLogSubmit(BaseModel):
    entries: list[FieldWorkLogEntry] = Field(min_length=1, max_length=50)


class FieldWorkLogResult(BaseModel):
    worklog: FieldWorkLogRead
    duplicate: bool
    backdated: bool


class FieldWorkLogSubmitResponse(BaseModel):
    results: list[FieldWorkLogResult]


class FieldJobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    crm_work_order_id: str
    event: str
    previous_status: str | None = None
    new_status: str | None = None
    person_id: UUID
    system_user_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    note: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime
    client_event_id: UUID


class FieldMovementRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    destination_type: str
    destination_id: str | None = None
    destination_label: str | None = None
    destination_latitude: float | None = None
    destination_longitude: float | None = None
    started_at: datetime
    arrived_at: datetime | None = None
    start_latitude: float | None = None
    start_longitude: float | None = None
    arrival_latitude: float | None = None
    arrival_longitude: float | None = None
    status: str
    client_ref: UUID | None = None
    created_at: datetime
    updated_at: datetime


class FieldTransitionRequest(BaseModel):
    event: Literal[
        "accept",
        "en_route",
        "arrived",
        "start",
        "pause",
        "hold",
        "resume",
        "complete",
        "unable_to_complete",
    ]
    client_event_id: UUID
    occurred_at: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)


class FieldTransitionResponse(BaseModel):
    job: FieldJobSummary
    event: FieldJobEventRead
    replayed: bool


class FieldEquipmentRecord(BaseModel):
    serial_number: str = Field(min_length=1, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class FieldEquipmentRead(BaseModel):
    id: UUID
    ont_unit_id: UUID
    serial_number: str
    vendor: str | None = None
    model: str | None = None
    subscriber_id: UUID
    crm_work_order_id: str | None = None
    assigned_at: datetime | None = None
    active: bool
    notes: str | None = None


class FieldMaterialRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_material_id: str | None = None
    item_id: UUID
    sku: str | None = None
    name: str
    unit: str | None = None
    allocated_quantity: int
    consumed_quantity: int
    remaining_quantity: int
    status: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class FieldMaterialConsumeItem(BaseModel):
    material_id: UUID
    consumed_quantity: int = Field(ge=0)
    leftover_note: str | None = Field(default=None, max_length=1000)


class FieldMaterialConsumeRequest(BaseModel):
    items: list[FieldMaterialConsumeItem] = Field(min_length=1, max_length=50)


class FieldMaterialRequestItemCreate(BaseModel):
    item_id: UUID
    quantity: int = Field(gt=0)
    notes: str | None = Field(default=None, max_length=1000)


class FieldMaterialRequestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    notes: str | None = Field(default=None, max_length=2000)
    items: list[FieldMaterialRequestItemCreate] = Field(min_length=1, max_length=50)


class FieldMaterialRequestItemRead(BaseModel):
    id: UUID
    item_id: UUID
    sku: str | None = None
    name: str
    unit: str | None = None
    quantity: int
    notes: str | None = None


class FieldMaterialRequestRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_material_request_id: str | None = None
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None = None
    status: str
    priority: str
    notes: str | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    fulfilled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    items: list[FieldMaterialRequestItemRead] = Field(default_factory=list)


class FieldExpenseRequestItemCreate(BaseModel):
    category_code: str = Field(min_length=1, max_length=30)
    category_name: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0)
    expense_date: date | None = None
    vendor_name: str | None = Field(default=None, max_length=200)
    receipt_url: str | None = Field(default=None, max_length=500)
    receipt_attachment_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class FieldExpenseRequestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=500)
    expense_date: date | None = None
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    notes: str | None = Field(default=None, max_length=2000)
    client_ref: UUID | None = None
    items: list[FieldExpenseRequestItemCreate] = Field(min_length=1, max_length=50)


class FieldExpenseRequestItemRead(BaseModel):
    id: UUID
    category_code: str
    category_name: str | None = None
    description: str
    amount: Decimal
    expense_date: date | None = None
    vendor_name: str | None = None
    receipt_url: str | None = None
    receipt_attachment_id: UUID | None = None
    notes: str | None = None


class FieldExpenseRequestRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_expense_request_id: str | None = None
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None = None
    status: str
    purpose: str
    expense_date: date | None = None
    currency: str
    notes: str | None = None
    rejection_reason: str | None = None
    erp_expense_claim_id: str | None = None
    erp_claim_number: str | None = None
    erp_claim_status: str | None = None
    client_ref: UUID | None = None
    total_amount: Decimal
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    paid_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    items: list[FieldExpenseRequestItemRead] = Field(default_factory=list)


class FieldJobHistoryItem(BaseModel):
    id: str
    type: str
    title: str
    description: str | None = None
    occurred_at: datetime | None = None
    actor_name: str | None = None
    status: str | None = None
    is_internal: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FieldJobDetail(BaseModel):
    job: FieldJobSummary
    customer: FieldCustomer | None = None
    location: FieldJobLocation
    ticket_ref: str | None = None
    project_id: str | None = None
    access_notes: str | None = None
    additional_contacts: list[FieldSiteContact] = Field(default_factory=list)
    recent_visits: list[FieldVisitHistoryItem] = Field(default_factory=list)
    open_tickets: list[FieldOpenTicketItem] = Field(default_factory=list)
    notes: list[FieldNoteRead] = Field(default_factory=list)
    attachments: list[FieldAttachmentRead] = Field(default_factory=list)
    materials: list[FieldMaterialRead] = Field(default_factory=list)
    material_requests: list[FieldMaterialRequestRead] = Field(default_factory=list)
    expense_requests: list[FieldExpenseRequestRead] = Field(default_factory=list)
    worklogs: list[FieldWorkLogRead] = Field(default_factory=list)
    events: list[FieldJobEventRead] = Field(default_factory=list)
    movements: list[FieldMovementRead] = Field(default_factory=list)
    equipment: FieldEquipmentRead | None = None
    history: list[FieldJobHistoryItem] = Field(default_factory=list)


class FieldScheduleEntry(BaseModel):
    type: str
    start_at: datetime
    end_at: datetime | None = None
    title: str
    reference_id: str


class FieldRouteStop(BaseModel):
    sequence: int
    work_order_id: str
    work_order_mirror_id: UUID
    title: str
    distance_km: float | None = None
    leg_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None


class FieldRouteResponse(BaseModel):
    route: list[FieldRouteStop]


class LocationPingInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    captured_at: datetime | None = None
    crm_work_order_id: str | None = Field(default=None, max_length=64)
    source: str = Field(default="mobile", max_length=32)
    status: str | None = Field(default=None, max_length=20)


class LocationPingBatch(BaseModel):
    pings: list[LocationPingInput] = Field(min_length=1, max_length=200)


class LocationSharingUpdate(BaseModel):
    enabled: bool
    status: str | None = Field(default=None, max_length=20)


class FieldPresenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    person_id: UUID
    status: str
    location_sharing_enabled: bool
    last_latitude: float | None = None
    last_longitude: float | None = None
    last_location_accuracy_m: float | None = None
    last_location_at: datetime | None = None
    last_seen_at: datetime | None = None


class LocationIngestResponse(BaseModel):
    accepted: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
    presence: FieldPresenceRead
    transitions: list[dict[str, Any]] = Field(default_factory=list)


class VoiceExtractRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=120)
    asr_confidence: float | None = Field(default=None, ge=0, le=1)


class VoiceExtractResponse(BaseModel):
    work_status: str | None = None
    equipment_serial: str | None = None
    signal_readings: dict[str, str] = Field(default_factory=dict)
    materials_used: list[dict[str, str | None]] = Field(default_factory=list)
    notes: str
    confidence: float | None = None
    requires_review: bool
    review_reasons: list[str] = Field(default_factory=list)


class FieldMapAsset(BaseModel):
    id: UUID
    type: str
    title: str
    subtitle: str | None = None
    latitude: float
    longitude: float
    status: str | None = None
    updated_at: datetime | None = None
    distance_m: float | None = None


class FieldMapSearchResult(BaseModel):
    kind: Literal["job", "asset"]
    id: str
    asset_type: str | None = None
    title: str
    subtitle: str | None = None
    latitude: float
    longitude: float
    status: str | None = None
    address_text: str | None = None


class FieldMapSearchResponse(BaseModel):
    items: list[FieldMapSearchResult]
    count: int
    limit: int
    offset: int = 0


class FieldSpliceCreate(BaseModel):
    closure_id: UUID
    from_strand_id: UUID
    to_strand_id: UUID
    tray_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)
    splice_type: str | None = Field(default=None, max_length=80)
    loss_db: float | None = Field(default=None, ge=0, le=5)
    note: str | None = Field(default=None, max_length=2000)


class FieldSpliceProposalResponse(BaseModel):
    change_request_id: UUID
    status: str
    replayed: bool
    closure_id: UUID
    from_strand_id: UUID
    to_strand_id: UUID


class FieldFiberTestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    asset_type: str = Field(min_length=1, max_length=80)
    asset_id: UUID
    test_type: str = Field(min_length=1, max_length=40)
    wavelength_nm: int | None = Field(default=None, ge=0)
    value_db: float | None = None
    unit: str | None = Field(default=None, max_length=16)
    passed: bool | None = None
    instrument: str | None = Field(default=None, max_length=120)
    measured_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    attachment_id: UUID | None = None
    client_ref: UUID | None = None


class FieldFiberTestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    work_order_mirror_id: UUID
    crm_work_order_id: str
    asset_type: str
    asset_id: UUID
    test_type: str
    wavelength_nm: int | None = None
    value_db: float | None = None
    unit: str | None = None
    passed: bool | None = None
    instrument: str | None = None
    attachment_id: UUID | None = None
    measured_by_person_id: UUID
    measured_by_system_user_id: UUID | None = None
    measured_at: datetime | None = None
    notes: str | None = None
    client_ref: UUID | None = None
    created_at: datetime
