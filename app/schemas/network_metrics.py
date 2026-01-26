from pydantic import BaseModel


class PortUtilizationRead(BaseModel):
    total_ports: int
    used_ports: int | None = None
    assigned_ports: int | None = None
    olt_id: str | None = None
    splitter_id: str | None = None


class FiberEndpointRef(BaseModel):
    endpoint_type: str | None = None
    endpoint_id: str | None = None
    label: str | None = None
    data: dict | None = None


class FiberPathSegment(BaseModel):
    segment_type: str
    strand_id: str | None = None
    splice_id: str | None = None
    closure_id: str | None = None
    upstream: FiberEndpointRef | None = None
    downstream: FiberEndpointRef | None = None


class FiberPathRead(BaseModel):
    segments: list[FiberPathSegment]
