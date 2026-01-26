from uuid import UUID

from pydantic import BaseModel


class TypeaheadItem(BaseModel):
    id: UUID
    label: str
