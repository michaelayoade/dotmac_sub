from uuid import UUID
from typing import Literal

from pydantic import BaseModel


class CustomerSearchItem(BaseModel):
    id: UUID
    type: Literal["person", "organization"]
    label: str
    ref: str
