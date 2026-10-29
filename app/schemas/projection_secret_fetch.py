"""Wire contract for the single-use projection secret fetch (ADR-0076 §7).

The request names a credential, the secret version the plan was authored
against, and the open projection request ref. The response is the secret
and nothing else. Credential-state refusals are one opaque 409: the endpoint must not
distinguish unknown ref, stale version, spent request, or dead credential.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectionSecretFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: UUID
    secret_version: int = Field(ge=1)
    projection_request_ref: str = Field(min_length=1, max_length=240)


class ProjectionSecretFetchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=1, repr=False)
