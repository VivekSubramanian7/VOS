"""VOS — Telegram capture into a knowledge graph.

Module layout mirrors the architecture's component model:

    contracts  — Pydantic models and seam protocols (no deps on other modules)
    settings   — configuration from environment
    journal    — append-only source of truth
    graph      — Neo4j projection (derived, rebuildable)
    cassette   — model-call record/replay log
    pipeline   — LangGraph single-node classification
    render     — CaptureResult -> user-facing text
    shell      — Telegram gateway (aiogram, long-polling)
    cli        — reclassify / rebuild
"""

__version__ = "0.1.0"
