"""Governed native creation values without runtime owner imports."""

from typing import Literal

WorkOrderCreationPriority = Literal[
    "lower", "low", "medium", "normal", "high", "urgent"
]
