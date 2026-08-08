"""Rebuilding the projection from the journal.

This module is the practical payoff of "the journal is the source of truth". Three
operations, all built on replaying the same append-only stream:

  `reproject_missing` — startup recovery. Closes the crash window between the journal
      write and the graph write (§9.2): a thought that was durably captured but never
      projected shows up in the graph, and in /pending.

  `classify_missing` — enrich anything sitting unfiled, whether it failed the first
      time, was deferred by the budget, or arrived while the provider was down.

  `rebuild` — wipe and replay everything. Used after a prompt change or a category
      addition, so improvements apply retroactively rather than only to new thoughts.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from vos.contracts import CaptureRecord, Classification, VideoResult
from vos.graph import Neo4jGraph
from vos.journal import JsonlJournal
from vos.pipeline import ThoughtState

log = logging.getLogger(__name__)


async def reproject_missing(journal: JsonlJournal, graph: Neo4jGraph) -> list[CaptureRecord]:
    """Insert journal records the graph has never seen. Returns what was recovered."""
    known = await graph.existing_ids()
    missing = [r for r in journal.records() if r.id not in known]
    for record in missing:
        await graph.upsert_thought(record, None)
    if missing:
        log.warning(
            "Recovered %d thought(s) present in the journal but missing from the graph. "
            "They are in /pending.",
            len(missing),
        )
    return missing


async def classify_one(
    pipeline, graph: Neo4jGraph, record: CaptureRecord
) -> tuple[Classification | None, str | None, list[str]]:
    """Run one record through the pipeline and project the outcome.

    Returns (classification, error, linked_followed_sources).

    Never raises on a model failure: the thought is already durable, so a failed
    enrichment is recorded and retried, not escalated.
    """
    followed = await graph.following()
    out = await pipeline.ainvoke(ThoughtState(record=record, followed=followed))
    classification = out.get("classification")
    error = out.get("error")

    if classification is not None:
        linked = await graph.upsert_thought(record, classification)
        return classification, None, linked

    await graph.mark_unclassified(record.id, error or "unknown error")
    return None, error or "unknown error", []


async def classify_missing(
    journal: JsonlJournal, graph: Neo4jGraph, pipeline, limit: int | None = None
) -> tuple[int, int]:
    """Enrich everything currently unfiled. Returns (succeeded, failed)."""
    unfiled = {v.id for v in await graph.pending()}
    records = [r for r in journal.records() if r.id in unfiled]
    if limit is not None:
        records = records[:limit]

    ok = failed = 0
    for record in records:
        classification, _, _ = await classify_one(pipeline, graph, record)
        if classification is not None:
            ok += 1
        else:
            failed += 1
    return ok, failed


async def process_video(
    video_pipeline, graph: Neo4jGraph, video_id: str, thought_id: UUID | None = None
) -> VideoResult:
    """Run the video pipeline and project the outcome.

    Mirrors `classify_one`: the graph produces data, this writes it. Never raises — a
    video that cannot be fetched is reported, and the thought that referenced it stays
    exactly as it was.
    """
    from vos.pipeline import VideoState

    out = await video_pipeline.ainvoke(
        VideoState(video_id=video_id, thought_id=thought_id)
    )
    artifact = out.get("artifact")
    distillation = out.get("distillation")
    error = out.get("error")

    if error or artifact is None or distillation is None:
        reason = error or "no transcript"
        permanent = bool(out.get("permanent", True))
        if thought_id is not None:
            # Recorded so a permanently dead video stops being re-queued on every
            # restart. Best-effort: the failure is already the outcome being reported,
            # and losing the marker only costs a wasted retry.
            with contextlib.suppress(Exception):
                await graph.mark_video_failed(thought_id, reason, permanent)
        return VideoResult(video_id=video_id, error=reason, permanent=permanent)

    await graph.upsert_video(artifact.meta, thought_id, is_generated=artifact.is_generated)
    count = await graph.replace_notes(video_id, distillation.notes)

    return VideoResult(
        video_id=video_id,
        meta=artifact.meta,
        distillation=distillation,
        note_count=count,
        truncated=bool(out.get("truncated")),
        is_generated=artifact.is_generated,
    )


def select(
    records: Iterable[CaptureRecord], since: datetime | None = None
) -> list[CaptureRecord]:
    return [r for r in records if since is None or r.captured_at >= since]


async def rebuild(
    journal: JsonlJournal,
    graph: Neo4jGraph,
    pipeline,
    *,
    since: datetime | None = None,
    wipe: bool = True,
    progress=None,
) -> tuple[int, int]:
    """Replay the journal into a fresh projection. Returns (succeeded, failed).

    Wiping is safe precisely because the graph is derived — this is a normal
    operation, not a recovery procedure.
    """
    if wipe:
        await graph.wipe()
        await graph.ensure_schema()

    records = select(journal.records(), since)
    ok = failed = 0
    for i, record in enumerate(records, start=1):
        classification, _, _ = await classify_one(pipeline, graph, record)
        if classification is not None:
            ok += 1
        else:
            failed += 1
        if progress:
            progress(i, len(records), record)
    return ok, failed
