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
from datetime import UTC, datetime
from uuid import UUID

from vos.contracts import (
    CaptureRecord,
    Classification,
    PulseError,
    PulseResult,
    ShoppingResult,
    ShoppingStore,
    VideoResult,
    WriteResult,
    draft_id,
    pulse_id,
)
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


async def run_pulse(
    fetcher, graph: Neo4jGraph, topic: str, *, cassette=None
) -> PulseResult:
    """Fetch one digest and project it.

    Mirrors `process_video`: the fetcher produces data, this writes it, and a
    failure is reported rather than raised — a digest that could not be fetched
    costs the user a message, not a crash.
    """
    handles = [s.name for s in await graph.following() if s.kind == "x"]

    try:
        artifact, dropped = await fetcher.fetch(topic, handles)
    except PulseError as exc:
        return PulseResult(topic=topic, error=exc.reason)

    # Recorded before the graph write, deliberately. The money left the account the
    # moment xAI answered; if the projection then fails, BudgetGuard must still know.
    if cassette is not None:
        from vos.cassette import CassetteEntry

        with contextlib.suppress(Exception):
            cassette.record(
                CassetteEntry(
                    thought_id=pulse_id(topic, artifact.digest.asked_at),
                    model=artifact.model,
                    prompt=f"pulse:{topic}",
                    cost_usd=artifact.cost_usd,
                )
            )

    count = await graph.save_pulse(artifact.digest)
    return PulseResult(
        topic=topic,
        digest=artifact.digest,
        post_count=count,
        dropped=dropped,
        cost_usd=artifact.cost_usd,
    )


async def run_write(
    write_pipeline, graph, keywords: list[str], digest, *, artifact_dir=None,
    model_name: str = "unknown", cost_usd: float | None = None, cassette=None,
) -> WriteResult:
    """Compose one LinkedIn draft and cache it.

    Mirrors `run_pulse`: the pipeline produces data, this writes it, and a failure is
    reported rather than raised. The X search that produced `digest` has already been
    paid for by the time this runs, so a compose failure must not also cost the digest.
    """
    from vos.cassette import CassetteEntry
    from vos.pipeline import WriteState
    from vos.writer import DraftStore, build_artifact, gather_own_material, trend_lines

    own_notes, thought_ids = await gather_own_material(graph, keywords)
    summary, posts = trend_lines(digest)

    out = await write_pipeline.ainvoke(
        WriteState(
            keywords=keywords,
            trend_summary=summary,
            trend_posts=posts,
            own_notes=own_notes,
        )
    )
    draft = out.get("draft")
    error = out.get("error")
    if error or draft is None:
        return WriteResult(keywords=keywords, error=error or "no draft produced")

    written_at = datetime.now(UTC)

    if cassette:
        with contextlib.suppress(Exception):
            cassette.record(
                CassetteEntry(
                    thought_id=draft_id(keywords, written_at),
                    model=f"{model_name} (write)",
                    prompt=", ".join(keywords),
                    response={"hook": draft.hook, "chars": len(draft.text)},
                    latency_ms=int(out.get("latency_ms") or 0),
                )
            )

    if artifact_dir is not None:
        source_urls = [
            u for u in (getattr(p, "url", None) for p in (getattr(digest, "posts", None) or []))
            if u
        ]
        # Logged rather than suppressed: a silent failure here loses a draft that
        # cost real money, and the only way anyone would notice is an empty folder.
        try:
            DraftStore(artifact_dir).write(
                build_artifact(
                    draft,
                    keywords=keywords,
                    model=model_name,
                    source_posts=source_urls,
                    source_thoughts=thought_ids,
                    cost_usd=cost_usd,
                    now=written_at,
                )
            )
        except Exception:  # noqa: BLE001 - the user still gets the post in the reply
            log.warning("Could not cache draft for %s", keywords, exc_info=True)

    return WriteResult(
        keywords=keywords,
        draft=draft,
        source_posts=len(posts),
        source_thoughts=len(own_notes),
        cost_usd=cost_usd,
    )


async def process_shopping(
    shopping_pipeline, store: ShoppingStore, record: CaptureRecord
) -> ShoppingResult:
    """Run the extraction pipeline and project the outcome onto the list.

    Mirrors `process_video`: the pipeline produces data, this writes it, and nothing
    here raises. The thought is already captured and filed by the time this runs, so a
    failed extraction costs a list entry, not a thought.
    """
    from vos.pipeline import ShoppingState

    out = await shopping_pipeline.ainvoke(ShoppingState(record=record))
    extraction = out.get("extraction")
    error = out.get("error")

    if error or extraction is None:
        reason = error or "no items extracted"
        # Best-effort: the failure is already the outcome being reported, and losing
        # the marker only costs one retry on the next restart.
        with contextlib.suppress(Exception):
            await store.record_extraction(record.id, error=reason)
        return ShoppingResult(thought_id=record.id, error=reason)

    await store.add(record.id, extraction.items, record.captured_at)
    await store.record_extraction(record.id)
    return ShoppingResult(thought_id=record.id, items=extraction.items)


async def replay_item_marks(journal: JsonlJournal, store: ShoppingStore) -> int:
    """Re-apply every shopping tick recorded in the journal. Returns how many.

    Run on every startup *and* after a rebuild, for two different reasons. On startup
    it closes the crash window between the journal append and the store write, exactly
    as `reproject_missing` does for thoughts. After a rebuild it is the only thing
    standing between the user and a list that has silently un-bought itself.

    Idempotent and order-independent: the store gates each mark on its timestamp.
    """
    marks = journal.item_marks()
    for mark in marks:
        with contextlib.suppress(Exception):
            await store.mark(mark.name, mark.action, mark.at)
    return len(marks)


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
    shopping: ShoppingStore | None = None,
) -> tuple[int, int]:
    """Replay the journal into a fresh projection. Returns (succeeded, failed).

    Wiping is safe precisely because both projections are derived — this is a normal
    operation, not a recovery procedure.

    The shopping list is rebuilt in two halves. Ticks replay here, immediately, because
    they exist only in the journal. Items do *not*: extraction costs a model call each,
    so the emptied `extractions` table is left to say "nothing has been extracted", and
    the next bot startup re-queues the work in the background. A tick that lands before
    its item is re-extracted still resolves correctly — that is what the store's
    timestamp gate is for.
    """
    if wipe:
        await graph.wipe()
        await graph.ensure_schema()
        if shopping is not None:
            await shopping.wipe()

    if shopping is not None:
        await replay_item_marks(journal, shopping)

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
