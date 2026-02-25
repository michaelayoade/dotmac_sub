from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TableColumnAvailable(BaseModel):
    key: str
    label: str
    sortable: bool = False
    hidden_by_default: bool = False


class TableColumnPreference(BaseModel):
    column_key: str = Field(min_length=1, max_length=120)
    display_order: int = Field(ge=0)
    is_visible: bool


class TableColumnResolved(BaseModel):
    column_key: str
    label: str
    sortable: bool
    display_order: int
    is_visible: bool


class TableColumnsResponse(BaseModel):
    table_key: str
    available_columns: list[TableColumnAvailable]
    columns: list[TableColumnResolved]


class TableDataResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    table_key: str
    columns: list[TableColumnResolved]
    items: list[dict[str, Any]]
    count: int
    limit: int
    offset: int
