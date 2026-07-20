"""AI transport — the provider boundary, not a decision system.

``docs/designs/AI_SOT.md`` declares ``ai.gateway`` a **transport**: it talks to
the LLM provider, applies redaction and prompt-injection defences, and records
telemetry. It holds no business rule and owns no domain state — the same
species as a payment gateway or an SMS provider.

Nothing here writes an ORM row. Insight persistence belongs to
``ai.insights`` (``app.services.ai_operations``), the canonical writer of
``AIInsight``; ``tests/architecture/test_ai_boundaries.py`` enforces that.
"""
