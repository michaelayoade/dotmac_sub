"""Sales vertical services — leads, pipelines, quotes (Phase 3 §2.1).

Ported from ``dotmac_crm/app/services/crm/sales/``. Self-serve quote
extraction (feasibility / estimate / request / accept) lands separately as
``app.services.sales.selfserve`` in the next PR of the Phase 3 series.
"""

from app.services.sales.service import (
    LEAD_SOURCE_OPTIONS,
    Leads,
    Pipelines,
    PipelineStages,
    QuoteLineItems,
    Quotes,
    leads,
    pipeline_stages,
    pipelines,
    quote_line_items,
    quotes,
)

__all__ = [
    "LEAD_SOURCE_OPTIONS",
    "Leads",
    "PipelineStages",
    "Pipelines",
    "QuoteLineItems",
    "Quotes",
    "leads",
    "pipeline_stages",
    "pipelines",
    "quote_line_items",
    "quotes",
]
