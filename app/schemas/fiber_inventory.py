"""Typed read contract shared by fiber inventory owners."""

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T", covariant=True)


@dataclass(frozen=True, slots=True)
class FiberInventoryQuery:
    page: int = 1
    per_page: int = 50
    search: str = ""
    active_only: bool = False

    @property
    def normalized_search(self) -> str:
        return self.search.strip()[:120]

    @property
    def page_size(self) -> int:
        return min(max(self.per_page, 10), 100)


@dataclass(frozen=True, slots=True)
class FiberInventoryPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    page: int
    per_page: int
    total_pages: int


def page_number(query: FiberInventoryQuery, total: int) -> int:
    return min(
        max(query.page, 1), max(1, (total + query.page_size - 1) // query.page_size)
    )


def page_result(
    query: FiberInventoryQuery, total: int, items: list[T]
) -> FiberInventoryPage[T]:
    per_page = query.page_size
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = page_number(query, total)
    return FiberInventoryPage(tuple(items), total, page, per_page, total_pages)
