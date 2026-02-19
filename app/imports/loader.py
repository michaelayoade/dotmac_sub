from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass
class ImportError:
    index: int
    detail: str


def _read_path(path: str | Path) -> Path:
    return Path(path).expanduser()


def load_csv(path: str | Path, model_cls: type[ModelT]) -> tuple[list[ModelT], list[ImportError]]:
    items: list[ModelT] = []
    errors: list[ImportError] = []
    with _read_path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader, start=1):
            try:
                items.append(model_cls.model_validate(row))
            except ValidationError as exc:
                errors.append(ImportError(index=idx, detail=str(exc)))
    return items, errors


def load_json(path: str | Path, model_cls: type[ModelT]) -> tuple[list[ModelT], list[ImportError]]:
    items: list[ModelT] = []
    errors: list[ImportError] = []
    data = json.loads(_read_path(path).read_text(encoding="utf-8"))
    payloads: Iterable[Any]
    if isinstance(data, dict):
        payloads = data.get("items", [])
    else:
        payloads = data
    for idx, payload in enumerate(payloads, start=1):
        try:
            items.append(model_cls.model_validate(payload))
        except ValidationError as exc:
            errors.append(ImportError(index=idx, detail=str(exc)))
    return items, errors


def load_csv_content(
    content: str, model_cls: type[ModelT], max_rows: int | None = None
) -> tuple[list[tuple[int, ModelT]], list[ImportError]]:
    items: list[tuple[int, ModelT]] = []
    errors: list[ImportError] = []
    reader = csv.DictReader(io.StringIO(content))
    for idx, row in enumerate(reader, start=1):
        if max_rows is not None and idx > max_rows:
            errors.append(ImportError(index=idx, detail="Row limit exceeded"))
            break
        try:
            items.append((idx, model_cls.model_validate(row)))
        except ValidationError as exc:
            errors.append(ImportError(index=idx, detail=str(exc)))
    return items, errors
