"""Server-produced evidence from the canonical route permission gate.

Never deserialize this value from a request. ``auth.permission_gate`` creates
it only after its existing permission and staff-leave guards succeed.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PermissionAdmission:
    principal_id: UUID
    principal_type: Literal["system_user", "subscriber"]
    admitted_permissions: frozenset[str]
