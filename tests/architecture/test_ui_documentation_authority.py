from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_normative_ui_documents_share_one_authority_chain() -> None:
    standard = _read("docs/UI_INFORMATION_AND_ACTION_STANDARD.md")
    assert "Status: approved cross-Dotmac standard" in standard
    assert "## Documentation Authority" in standard
    assert "## Ownership Boundary" in standard
    assert "## Required Page Contract" in standard
    assert "## Table Standard" in standard
    assert "## Action Standard" in standard

    dependants = (
        "docs/PRODUCTION_UI_BRIEF.md",
        "docs/FRONTEND_SPEC.md",
        "docs/DESIGN_REVIEW_CHECKLIST.md",
        "docs/DEVELOPER_GUIDE.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "docs/SOT_RELATIONSHIP_MAP.md",
    )
    for path in dependants:
        assert "UI_INFORMATION_AND_ACTION_STANDARD.md" in _read(path), path


def test_historical_ui_documents_cannot_masquerade_as_current_policy() -> None:
    expected_markers = {
        "docs/UI_UX_COMPONENT_ARCHITECTURE.md": "historical implementation catalog",
        "docs/UI_UX_MASTER_PLAN.md": "historical module inventory",
        "docs/SMARTOLT_IMPLEMENTATION_PLAN.md": "historical comparative plan",
        "docs/SMARTOLT_UI_COMPARISON.md": "comparative research, not a specification",
        "docs/designs/UX_POLISH_AUDITS_INDEX.md": "historical findings and remediation evidence",
    }
    for path, marker in expected_markers.items():
        content = _read(path)
        assert marker in content, path
        assert "UI_INFORMATION_AND_ACTION_STANDARD.md" in content, path

    for path in (ROOT / "docs/designs").glob("*UX_POLISH_AUDIT.md"):
        content = path.read_text(encoding="utf-8")
        assert "historical audit evidence" in content, path
        assert "UI_INFORMATION_AND_ACTION_STANDARD.md" in content, path

    for path in (ROOT / "docs/feature_improvements").glob("*.md"):
        content = path.read_text(encoding="utf-8")
        assert "historical" in content, path
        assert "UI_INFORMATION_AND_ACTION_STANDARD.md" in content, path


def test_design_inventory_does_not_override_operational_ui_policy() -> None:
    design = _read("DESIGN.md")
    assert 'status: "implementation-inventory"' in design
    assert "UI_INFORMATION_AND_ACTION_STANDARD.md" in design
    assert "Subtle gradient mesh + noise texture" not in design
    assert "Page header: rounded-3xl, gradient" not in design
    assert "Buttons: rounded-xl, gradient backgrounds" not in design

    brief = _read("docs/PRODUCTION_UI_BRIEF.md")
    assert "Large hero treatments are allowed" not in brief
    assert "do not use hero compositions" in brief
    assert "8px maximum corner radius" in brief


def test_review_gate_requires_information_and_action_ownership() -> None:
    checklist = _read("docs/DESIGN_REVIEW_CHECKLIST.md")
    assert "## Information Contract" in checklist
    assert "## Actions" in checklist
    assert "## Tables" in checklist
    assert "authoritative read/context owner" in checklist
    assert "command owner rechecks permission and eligibility" in checklist

    template = _read(".github/PULL_REQUEST_TEMPLATE.md")
    assert "read owner, and action owner" in template
    assert "authoritative services rather than UI inference" in template
    assert "Information/action contract or `N/A`" in template
