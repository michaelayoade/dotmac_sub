"""Admin Help Center, projected from the workflow-guidance registry."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_workflow_guidance
from app.web.templates import templates

router = APIRouter(prefix="/help", tags=["web-admin-help"])


def _group_articles(
    articles: tuple[admin_workflow_guidance.AdminWorkflowGuidance, ...],
) -> list[dict[str, object]]:
    return [
        {
            "category": category,
            "articles": [
                article for article in articles if article.category == category
            ],
        }
        for category in admin_workflow_guidance.guidance_categories()
        if any(article.category == category for article in articles)
    ]


@router.get("", response_class=HTMLResponse)
def help_center(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    article: str = Query(""),
    db: Session = Depends(get_db),
):
    """Render current staff guidance; the registry is its only content source."""
    from app.web.admin import get_current_user, get_sidebar_stats

    articles = admin_workflow_guidance.search_guidance(query=q, category=category)
    selected_article = next(
        (item for item in articles if item.slug == article.strip()),
        articles[0] if articles else None,
    )
    return templates.TemplateResponse(
        "admin/help/index.html",
        {
            "request": request,
            "active_page": "help-center",
            "active_menu": "help",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "articles": articles,
            "grouped_articles": _group_articles(articles),
            "selected_article": selected_article,
            "categories": admin_workflow_guidance.guidance_categories(),
            "query": q,
            "selected_category": category.strip(),
        },
    )
