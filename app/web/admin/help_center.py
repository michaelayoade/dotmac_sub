"""Repository-curated admin help center."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_workflow_guidance
from app.services.admin_workflow_guidance import (
    AdminHelpAction,
    AdminKnowledgeDocument,
    AdminKnowledgeProjectionQuery,
)
from app.services.auth_dependencies import load_permission_keys
from app.web.auth.dependencies import WebAuthInfo, require_admin_web_auth
from app.web.templates import templates

router = APIRouter(prefix="/help", tags=["web-admin-help"])


@dataclass(frozen=True)
class HelpArticle:
    id: str
    category: str
    title: str
    summary: str
    actions: tuple[AdminHelpAction, ...] = ()
    audience: str = ""
    notes: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.id


def _article_matches(article: HelpArticle, *, query: str, category: str) -> bool:
    return (not category or article.category == category) and (
        not query
        or query in article.title.casefold()
        or query in article.summary.casefold()
        or query in article.audience.casefold()
        or any(query in action.title.casefold() for action in article.actions)
        or any(
            query in step.casefold()
            for action in article.actions
            for step in action.steps
        )
        or any(query in note.casefold() for note in article.notes)
    )


def _group_articles(
    articles: list[HelpArticle], *, selected_id: str = ""
) -> list[dict[str, object]]:
    article_by_id = {article.id: article for article in articles}
    categories: list[dict[str, object]] = []
    for section in admin_workflow_guidance.help_navigation():
        category_articles = [
            article_by_id[guide_id]
            for guide_id in section.guide_ids
            if guide_id in article_by_id
        ]
        if category_articles:
            categories.append(
                {
                    "id": section.id,
                    "category": section.label,
                    "permission": section.permission,
                    "articles": category_articles,
                    "selected": any(
                        article.id == selected_id for article in category_articles
                    ),
                }
            )
    return categories


def _article_from_knowledge(document: AdminKnowledgeDocument) -> HelpArticle:
    """Adapt one already permission-filtered knowledge document for the UI."""

    return HelpArticle(
        id=document.id,
        category=document.category,
        title=document.title,
        summary=document.purpose,
        actions=tuple(
            AdminHelpAction(id=action.id, title=action.title, steps=action.steps)
            for action in document.actions
        ),
        audience=document.audience,
        notes=document.notes,
    )


# The workflow-guidance registry is the authoritative Help Center content. The
# route consumes its permission-filtered knowledge projection rather than
# rebuilding section, guide, and action authorization in this adapter.
def _visible_articles(permission_keys: frozenset[str]) -> list[HelpArticle]:
    projection = admin_workflow_guidance.build_knowledge_projection(
        AdminKnowledgeProjectionQuery(permission_keys=permission_keys)
    )
    return [_article_from_knowledge(document) for document in projection.documents]


@router.get(
    "",
    response_class=HTMLResponse,
)
def help_center(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    article: str = Query(""),
    db: Session = Depends(get_db),
    auth: WebAuthInfo = Depends(require_admin_web_auth),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    # This route is available to every staff member, so it has no feature-level
    # permission dependency to populate the request cache. The Help hierarchy
    # still needs the same cached permissions as the contextual help control to
    # decide which registered guides and actions may be shown.
    request_auth = getattr(request.state, "auth", None)
    permission_keys = (
        load_permission_keys(request_auth, db)
        if isinstance(request_auth, dict)
        else frozenset()
    )
    query = q.strip().casefold()
    selected = category.strip()
    permission_filtered_articles = _visible_articles(permission_keys)
    visible_categories = tuple(
        category_name
        for category_name in admin_workflow_guidance.guidance_categories()
        if any(item.category == category_name for item in permission_filtered_articles)
    )
    articles = [
        item
        for item in permission_filtered_articles
        if _article_matches(item, query=query, category=selected)
    ]
    selected_article = next(
        (item for item in articles if item.slug == article.strip()),
        articles[0] if articles else None,
    )
    context: dict[str, object] = {
        "request": request,
        "active_page": "help-center",
        "active_menu": "help",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "articles": articles,
        "grouped_articles": _group_articles(
            articles, selected_id=selected_article.id if selected_article else ""
        ),
        "selected_article": selected_article,
        "categories": visible_categories,
        "query": q,
        "selected_category": selected,
    }
    return templates.TemplateResponse(
        "admin/help/index.html",
        context,
    )
