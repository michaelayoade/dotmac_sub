from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class CustomerSearchItem(BaseModel):
    id: UUID
    type: Literal["person", "organization"]
    label: str
    ref: str
