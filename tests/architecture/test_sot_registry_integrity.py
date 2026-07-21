"""Generic integrity and documentation-parity checks for the SOT registry."""

from __future__ import annotations

import re
from pathlib import Path

from app.services import sot_relationships
from scripts.architecture import sot_debt, sot_manifest_docs

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELATIONSHIP_MAP = PROJECT_ROOT / "docs" / "SOT_RELATIONSHIP_MAP.md"


def _dotted_reference_exists(reference: str) -> bool:
    parts = reference.split(".")
    for length in range(len(parts), 1, -1):
        candidate = PROJECT_ROOT.joinpath(*parts[:length])
        if candidate.is_dir() or candidate.with_suffix(".py").is_file():
            return True
    return False


def test_registry_has_unique_acyclic_resolvable_ownership() -> None:
    assert sot_relationships.registry_validation_errors() == ()


def test_registered_modules_and_dotted_entrypoints_exist() -> None:
    references = {service.module for service in sot_relationships.all_services()} | {
        entrypoint
        for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS
        for entrypoint in domain.entrypoints
        if entrypoint.startswith(("app.", "scripts."))
    }
    missing = sorted(
        reference for reference in references if not _dotted_reference_exists(reference)
    )

    assert not missing, (
        "registry references with no repository module or package:\n  "
        + "\n  ".join(missing)
    )


def test_relationship_map_domain_order_matches_registry() -> None:
    document = RELATIONSHIP_MAP.read_text(encoding="utf-8")
    domain_section = document.split("## Domain Order", maxsplit=1)[1].split(
        "\nRule:", maxsplit=1
    )[0]
    documented = re.findall(r"^\d+\. `([^`]+)`$", domain_section, re.MULTILINE)

    assert documented == sot_relationships.domain_order()


def test_relationship_map_writer_count_matches_executable_baseline() -> None:
    document = RELATIONSHIP_MAP.read_text(encoding="utf-8")
    baseline_count = len(sot_debt.read_name_baseline(sot_debt.WRITER_BASELINE))

    assert f"The {baseline_count} existing\nundeclared writer-like modules" in document


def test_generated_relationship_map_manifest_matches_registry() -> None:
    document = RELATIONSHIP_MAP.read_text(encoding="utf-8")
    current = sot_manifest_docs.extract_manifest_block(document)
    expected = sot_manifest_docs.render_manifest_block(sot_relationships.all_services())

    assert current == expected
