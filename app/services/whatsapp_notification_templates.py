"""Bridge WhatsApp provider templates into notification templates."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationTemplate
from app.services.integrations.connectors import whatsapp as whatsapp_connector

WHATSAPP_TEMPLATE_MARKER = "__whatsapp_template__"


def build_provider_template_body(
    *, name: str, language: str, variables: dict[str, Any] | None = None
) -> str:
    return json.dumps(
        {
            WHATSAPP_TEMPLATE_MARKER: True,
            "name": name.strip(),
            "language": language.strip() or "en",
            "variables": variables or {},
        },
        sort_keys=True,
    )


def parse_provider_template_body(body: str | None) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed.get(WHATSAPP_TEMPLATE_MARKER):
        return None
    name = str(parsed.get("name") or "").strip()
    if not name:
        return None
    language = str(parsed.get("language") or "").strip() or "en"
    variables = parsed.get("variables")
    return {
        "name": name,
        "language": language,
        "variables": variables if isinstance(variables, dict) else {},
    }


def provider_template_from_template(
    template: NotificationTemplate | None,
) -> dict[str, Any] | None:
    if not template or template.channel != NotificationChannel.whatsapp:
        return None
    marker = parse_provider_template_body(template.body)
    if marker:
        return marker
    return {"name": template.code, "language": "", "variables": {}}


def is_provider_template(template: NotificationTemplate | None) -> bool:
    return provider_template_from_template(template) is not None


def sync_whatsapp_registry_templates(db: Session) -> list[NotificationTemplate]:
    """Create notification-template rows for configured WhatsApp templates.

    Existing conditions/status are preserved. Legacy WhatsApp rows whose code
    matches a provider template name are converted to marker-backed rows.
    """

    config = whatsapp_connector.load_whatsapp_config(db)
    registry = _normalized_registry(config.get("templates") or [])
    existing = (
        db.query(NotificationTemplate)
        .filter(NotificationTemplate.channel == NotificationChannel.whatsapp)
        .all()
    )
    if not registry:
        return existing

    by_signature: dict[tuple[str, str], NotificationTemplate] = {}
    by_code = {template.code: template for template in existing}
    used_codes = set(by_code)
    changed = False
    name_counts = Counter(item["name"] for item in registry)

    for item in registry:
        signature = (item["name"], item["language"])
        template = by_signature.get(signature)
        if template is None:
            template = _find_existing_template(item, by_code, existing)
        if template is None:
            code = _unique_code(item["name"], item["language"], used_codes)
            template = NotificationTemplate(
                name=_display_name(item["name"], item["language"], name_counts),
                code=code,
                channel=NotificationChannel.whatsapp,
                subject=None,
                body=build_provider_template_body(
                    name=item["name"], language=item["language"]
                ),
                conditions={},
                is_active=True,
            )
            db.add(template)
            existing.append(template)
            by_code[code] = template
            used_codes.add(code)
            changed = True
        else:
            marker = parse_provider_template_body(template.body)
            if not marker or (
                marker["name"],
                marker["language"],
            ) != signature:
                template.body = build_provider_template_body(
                    name=item["name"], language=item["language"]
                )
                changed = True
            if not template.name.strip():
                template.name = _display_name(item["name"], item["language"], name_counts)
                changed = True
        by_signature[signature] = template

    if changed:
        db.commit()
        for template in existing:
            db.refresh(template)
    return existing


def _normalized_registry(raw_items: list[object]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, str]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        language = str(raw.get("language") or "").strip() or "en"
        signature = (name, language)
        if signature in seen:
            continue
        seen.add(signature)
        items.append({"name": name, "language": language})
    return items


def _find_existing_template(
    item: dict[str, str],
    by_code: dict[str, NotificationTemplate],
    existing: list[NotificationTemplate],
) -> NotificationTemplate | None:
    for template in existing:
        marker = parse_provider_template_body(template.body)
        if marker and (marker["name"], marker["language"]) == (
            item["name"],
            item["language"],
        ):
            return template
    candidate = by_code.get(item["name"])
    if candidate and not parse_provider_template_body(candidate.body):
        return candidate
    return None


def _normalize_code(value: str) -> str:
    code = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    while "__" in code:
        code = code.replace("__", "_")
    return code.strip("_") or "whatsapp_template"


def _unique_code(name: str, language: str, used_codes: set[str]) -> str:
    base = _normalize_code(name)
    if base not in used_codes:
        used_codes.add(base)
        return base
    language_suffix = _normalize_code(language)
    candidate = f"{base}_{language_suffix}"
    if candidate not in used_codes:
        used_codes.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used_codes:
        index += 1
    unique = f"{candidate}_{index}"
    used_codes.add(unique)
    return unique


def _display_name(
    name: str, language: str, name_counts: Counter[str] | None = None
) -> str:
    label = name.replace("_", " ").strip().title()
    if name_counts and name_counts[name] > 1:
        return f"{label} ({language})"
    return label
