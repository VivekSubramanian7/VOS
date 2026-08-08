"""Classification pipeline — LangGraph, one node.

Named `pipeline`, not `graph`: `vos.graph` is Neo4j, and two things called "graph" in
one codebase is a readability trap.

The graph has a single `analyze` node today. It exists as a graph rather than a
function because processors are the next thing to arrive, and adding one should be
adding a node rather than restructuring. LangGraph validates `ThoughtState` before each
node executes, so the pipeline's contract is enforced rather than advisory.

The model is *injected*, never constructed inside the node. That is what lets the whole
pipeline run offline in tests with a fake chat model, and what makes `VOS_MODEL` a
config change rather than a code change.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from vos.cassette import Cassette, CassetteEntry, price
from vos.contracts import (
    CaptureRecord,
    Classification,
    SourceRef,
    VideoArtifact,
    VideoDistillation,
    VideoNote,
    VideoProcessingError,
)
from vos.video import chunk_transcript

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You classify short personal thoughts captured by one person on their phone, so they can
be found again and connected to related thoughts later.

Assign exactly one category:

- Shopping        things to buy, shopping lists, product choices
- TripPlanning    travel: destinations, flights, accommodation, itineraries
- Family          family members, relationships, household matters, personal life
- Career          work, job, professional growth, colleagues, business ideas
- StudyResearch   learning something: papers, courses, concepts to understand
- StockResearch   markets, tickers, companies as investments, financial analysis
- VideoKnowledge  a video, talk, or podcast to extract knowledge from (usually a link)
- Other           genuinely does not fit above, or you are not confident

Rules:

1. Prefer Other over a wrong bucket. A thought filed as Other is reviewed later; a
   thought filed wrongly is lost. Set confidence honestly — below 0.6 means unsure.
2. `title` is a short label the person would recognise at a glance. Not a summary,
   not a restatement of the whole thought.
3. `summary` is one or two sentences. If the thought is already one short sentence,
   the summary may simply restate it.
4. Extract entities that would be worth finding this thought by later: people, places,
   products, organisations, stock tickers, topics, URLs. Do not invent entities that
   are not present. Prefer the form the person actually wrote.
5. When the thought mentions someone or something in the FOLLOWED list below, use that
   exact name so it resolves to the same node.
"""


class ThoughtState(BaseModel):
    """State schema for the pipeline. Validated by LangGraph before each node runs."""

    record: CaptureRecord
    followed: list[SourceRef] = Field(default_factory=list)
    classification: Classification | None = None
    error: str | None = None


def _followed_block(followed: list[SourceRef]) -> str:
    if not followed:
        return "FOLLOWED: (nothing declared yet)"
    lines = [
        f"- {s.name} ({s.kind}" + (f", by {s.author}" if s.author else "") + ")"
        for s in followed
    ]
    return "FOLLOWED — prefer these exact names when the thought refers to them:\n" + "\n".join(
        lines
    )


def build_prompt(record: CaptureRecord, followed: list[SourceRef]) -> list[Any]:
    """Assemble the messages.

    Ordered stable-first for prompt caching: the system prompt never changes and the
    follow list changes rarely, so both sit ahead of the thought text, which changes
    every call. The minimum cacheable prefix on Opus 5 is 512 tokens.
    """
    return [
        SystemMessage(content=SYSTEM_PROMPT + "\n\n" + _followed_block(followed)),
        HumanMessage(content=record.text),
    ]


def _usage(response: Any) -> tuple[int, int]:
    """Token counts, best-effort across providers.

    Providers disagree on where usage lands, and `with_structured_output` can strip the
    envelope entirely. Metering is a nice-to-have; never let its absence break a capture.
    """
    meta = getattr(response, "usage_metadata", None) or {}
    if isinstance(meta, dict):
        return int(meta.get("input_tokens", 0) or 0), int(meta.get("output_tokens", 0) or 0)
    return 0, 0


def build_pipeline(
    model: BaseChatModel,
    *,
    model_name: str = "unknown",
    cassette: Cassette | None = None,
):
    """Compile the classification pipeline.

    `model` is injected so tests pass a fake and production passes
    `init_chat_model(VOS_MODEL)`.
    """
    structured = model.with_structured_output(Classification)

    async def analyze(state: ThoughtState) -> dict[str, Any]:
        messages = build_prompt(state.record, state.followed)
        prompt_text = "\n\n".join(str(m.content) for m in messages)
        started = time.perf_counter()

        try:
            result = await structured.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001 - any provider failure is the same to us
            elapsed = int((time.perf_counter() - started) * 1000)
            log.warning("Classification failed for %s: %s", state.record.id, exc)
            if cassette:
                cassette.record(
                    CassetteEntry(
                        thought_id=state.record.id,
                        model=model_name,
                        prompt=prompt_text,
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=elapsed,
                    )
                )
            # Returned, not raised: enrichment failure must not propagate into the
            # capture path. The thought is already durable.
            return {"error": f"{type(exc).__name__}: {exc}", "classification": None}

        elapsed = int((time.perf_counter() - started) * 1000)

        if result is None or not isinstance(result, Classification):
            msg = f"Model returned no valid Classification (got {type(result).__name__})"
            log.warning("%s for %s", msg, state.record.id)
            if cassette:
                cassette.record(
                    CassetteEntry(
                        thought_id=state.record.id,
                        model=model_name,
                        prompt=prompt_text,
                        error=msg,
                        latency_ms=elapsed,
                    )
                )
            return {"error": msg, "classification": None}

        tin, tout = _usage(result)
        if cassette:
            cassette.record(
                CassetteEntry(
                    thought_id=state.record.id,
                    model=model_name,
                    prompt=prompt_text,
                    response=result.model_dump(mode="json"),
                    input_tokens=tin,
                    output_tokens=tout,
                    cost_usd=price(model_name, tin, tout),
                    latency_ms=elapsed,
                )
            )
        return {"classification": result, "error": None}

    builder = StateGraph(ThoughtState)
    builder.add_node("analyze", analyze)
    builder.set_entry_point("analyze")
    builder.add_edge("analyze", END)
    return builder.compile()


# =========================================================================== #
# Video → knowledge
# =========================================================================== #

DISTIL_PROMPT = """\
You extract durable knowledge from a video transcript so the person can find and trust
it later, months after watching.

Each note must be a **claim someone could disagree with** — a fact, a mechanism, a
recommendation, a number. Not a topic label.

  bad   "Discusses the theory of relativity"
  bad   "Talks about why measurement is hard"
  good  "The one-way speed of light cannot be measured, only the round-trip average"
  good  "Compound annual growth above 15% is historically rare beyond a decade"

Rules:

1. Only what the transcript actually says. Auto-generated captions garble names,
   numbers, and technical terms constantly. If something is unclear, leave it out.
   Omission is free; an invented claim silently poisons the knowledge base.
2. `t_seconds` must be the timestamp where the claim is made, taken from the
   TIMESTAMPS markers in the transcript. Never estimate one.
3. Each note stands alone. It will be read years later with no surrounding context,
   so resolve pronouns and name the subject.
4. Extract entities worth finding this note by later: people, organisations, products,
   places, tickers, technical topics.
5. Prefer 3-8 strong notes over a dozen weak ones. A transcript with little substance
   should yield few notes, or none.
"""

SUMMARY_PROMPT = """\
Write two or three sentences describing what this video covers, based on the section
summaries below. State what it is about and what it argues — not "this video discusses".
"""


class VideoState(BaseModel):
    """State for the video pipeline. Separate graph from classification: the work runs
    later, on a worker, so re-entering the classification graph would re-run it."""

    thought_id: UUID | None = None
    video_id: str
    artifact: VideoArtifact | None = None
    distillation: VideoDistillation | None = None
    error: str | None = None
    permanent: bool = True
    truncated: bool = False


def _with_timestamps(start: int, text: str) -> str:
    return f"[TIMESTAMPS: this section begins at {start} seconds]\n\n{text}"


def _dedupe(notes: list[VideoNote], limit: int) -> list[VideoNote]:
    """Chunks overlap so a claim near a boundary is seen twice. Collapse on a
    normalised prefix, keeping the earliest timestamp."""
    seen: dict[str, VideoNote] = {}
    for note in sorted(notes, key=lambda n: n.t_seconds):
        key = " ".join(note.text.lower().split())[:80]
        if key not in seen:
            seen[key] = note
    return sorted(seen.values(), key=lambda n: n.t_seconds)[:limit]


def build_video_pipeline(
    model: BaseChatModel,
    fetcher: Any,
    *,
    model_name: str = "unknown",
    cassette: Cassette | None = None,
    max_chunks: int = 12,
    max_notes: int = 12,
):
    """Compile `fetch → distil`.

    Projection happens outside the graph, exactly as classification does — the graph
    produces data, the caller writes it.
    """
    structured = model.with_structured_output(VideoDistillation)

    # Nodes always return every field they own. LangGraph omits keys that were never
    # written, so a partial return would make the output shape depend on which branch
    # ran — a trap for every caller.
    def _out(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "artifact": None,
            "distillation": None,
            "error": None,
            "permanent": True,
            "truncated": False,
        }
        base.update(overrides)
        return base

    async def fetch(state: VideoState) -> dict[str, Any]:
        try:
            artifact = await fetcher.fetch(state.video_id)
        except VideoProcessingError as exc:
            log.info("Cannot fetch %s: %s", state.video_id, exc.reason)
            return _out(error=exc.reason, permanent=exc.permanent)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected fetch failure for %s", state.video_id)
            return _out(error=f"{type(exc).__name__}: {exc}", permanent=False)
        return _out(artifact=artifact)

    async def distil(state: VideoState) -> dict[str, Any]:
        artifact = state.artifact
        if artifact is None:
            return _out(error="no transcript")

        chunks = chunk_transcript(artifact.segments)
        truncated = len(chunks) > max_chunks
        if truncated:
            log.warning(
                "Transcript for %s is %d chunks; distilling the first %d",
                state.video_id,
                len(chunks),
                max_chunks,
            )
            chunks = chunks[:max_chunks]

        collected: list[VideoNote] = []
        summaries: list[str] = []
        started = time.perf_counter()

        for start, text in chunks:
            try:
                result = await structured.ainvoke(
                    [
                        SystemMessage(content=DISTIL_PROMPT),
                        HumanMessage(content=_with_timestamps(start, text)),
                    ]
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Distil chunk at %ds failed for %s: %s", start, state.video_id, exc)
                continue
            if isinstance(result, VideoDistillation):
                collected.extend(result.notes)
                if result.summary:
                    summaries.append(result.summary)

        if not collected and not summaries:
            return _out(
                artifact=artifact,
                error="the model produced no usable notes",
                permanent=False,
            )

        notes = _dedupe(collected, max_notes)
        summary = summaries[0] if len(summaries) == 1 else await _merge(summaries)

        if cassette and state.thought_id:
            cassette.record(
                CassetteEntry(
                    thought_id=state.thought_id,
                    model=f"{model_name} (video)",
                    prompt=f"{state.video_id}: {len(chunks)} chunk(s)",
                    response={"summary": summary, "note_count": len(notes)},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )

        return _out(
            artifact=artifact,
            distillation=VideoDistillation(summary=summary, notes=notes),
            truncated=truncated,
        )

    async def _merge(summaries: list[str]) -> str:
        if not summaries:
            return ""
        try:
            merged = await model.ainvoke(
                [
                    SystemMessage(content=SUMMARY_PROMPT),
                    HumanMessage(content="\n\n".join(summaries)),
                ]
            )
            return str(getattr(merged, "content", "")).strip() or summaries[0]
        except Exception:  # noqa: BLE001 - a weaker summary is fine; failing is not
            log.warning("Summary merge failed; using the first section summary")
            return summaries[0]

    def route(state: VideoState) -> str:
        return END if state.error else "distil"

    builder = StateGraph(VideoState)
    builder.add_node("fetch", fetch)
    builder.add_node("distil", distil)
    builder.set_entry_point("fetch")
    builder.add_conditional_edges("fetch", route, {"distil": "distil", END: END})
    builder.add_edge("distil", END)
    return builder.compile()


def load_model(model_spec: str) -> BaseChatModel:
    """Resolve `VOS_MODEL` ("provider:model") to a chat model.

    This one function is the whole of provider-agnosticism: `anthropic:claude-opus-5`
    -> `openai:gpt-...` -> `ollama:...` is a config change. Note the caveat from the
    architecture: `with_structured_output()` is uniform in interface, not in mechanism,
    so re-check classification quality with `vos reclassify --dry-run` after a swap.
    """
    from langchain.chat_models import init_chat_model

    return init_chat_model(model_spec)
