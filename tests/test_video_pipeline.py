"""Video pipeline tests — offline, with a stub model and stub fetcher.

The assertions worth having here are about behaviour under imperfection: overlapping
chunks producing duplicate claims, one chunk failing while others succeed, a transcript
longer than the budget allows, and a video that cannot be fetched at all. A summariser
that only works on the happy path is not much use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vos.cassette import Cassette
from vos.contracts import (
    ExtractedEntity,
    TranscriptSegment,
    VideoArtifact,
    VideoDistillation,
    VideoMeta,
    VideoNote,
    VideoProcessingError,
)
from vos.pipeline import VideoState, _dedupe, build_video_pipeline

VID = "dQw4w9WgXcQ"


class StubFetcher:
    def __init__(self, artifact: VideoArtifact | None = None, error: Exception | None = None):
        self.artifact = artifact
        self.error = error
        self.calls: list[str] = []

    async def fetch(self, video_id: str, *, refresh: bool = False) -> VideoArtifact:
        self.calls.append(video_id)
        if self.error is not None:
            raise self.error
        assert self.artifact is not None
        return self.artifact


class StubModel:
    """Returns queued results, one per structured call. Falls back to the last."""

    def __init__(self, results: list, merge_text: str = "Merged summary."):
        self.results = list(results)
        self.merge_text = merge_text
        self.structured_calls = 0
        self.merge_calls = 0

    def with_structured_output(self, schema):  # noqa: ANN001
        outer = self

        class _R:
            async def ainvoke(self, messages):  # noqa: ANN001
                outer.structured_calls += 1
                if not outer.results:
                    return None
                item = outer.results.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _R()

    async def ainvoke(self, messages):  # noqa: ANN001 - the summary-merge path
        self.merge_calls += 1

        class _Msg:
            content = self.merge_text

        return _Msg()


def _artifact(segment_count: int = 3) -> VideoArtifact:
    return VideoArtifact(
        meta=VideoMeta(
            video_id=VID,
            url=f"https://www.youtube.com/watch?v={VID}",
            title="Why No One Has Measured The Speed Of Light",
            channel="Veritasium",
            approx_duration_s=600,
        ),
        segments=[
            TranscriptSegment(text=" ".join(["word"] * 30), start=float(i * 10), duration=10.0)
            for i in range(segment_count)
        ],
        fetched_at=datetime.now(UTC),
    )


def _distillation(
    *notes: tuple[str, int], summary: str = "A video about light."
) -> VideoDistillation:
    return VideoDistillation(
        summary=summary,
        notes=[
            VideoNote(
                text=text,
                t_seconds=t,
                entities=[ExtractedEntity(name="Einstein", type="person", salience=0.8)],
            )
            for text, t in notes
        ],
    )


# --- happy path --------------------------------------------------------------- #


async def test_distils_a_video():
    model = StubModel([_distillation(("One-way light speed cannot be measured", 134))])
    graph = build_video_pipeline(model, StubFetcher(_artifact()), model_name="stub")

    out = await graph.ainvoke(VideoState(video_id=VID))
    d = out["distillation"]

    assert out["error"] is None
    assert len(d.notes) == 1
    assert d.notes[0].t_seconds == 134
    assert "light speed" in d.notes[0].text


async def test_single_chunk_skips_the_merge_call():
    """No point paying for a summary merge when there is one section."""
    model = StubModel([_distillation(("a claim", 10))])
    graph = build_video_pipeline(model, StubFetcher(_artifact()), model_name="stub")
    await graph.ainvoke(VideoState(video_id=VID))
    assert model.merge_calls == 0


async def test_notes_are_ordered_by_timestamp():
    model = StubModel([_distillation(("later", 300), ("earlier", 30), ("middle", 120))])
    graph = build_video_pipeline(model, StubFetcher(_artifact()), model_name="stub")
    out = await graph.ainvoke(VideoState(video_id=VID))
    assert [n.t_seconds for n in out["distillation"].notes] == [30, 120, 300]


# --- failure handling ---------------------------------------------------------- #


async def test_fetch_failure_short_circuits_before_the_model():
    """No transcript means no reason to spend a token."""
    model = StubModel([_distillation(("never reached", 1))])
    fetcher = StubFetcher(error=VideoProcessingError("captions are disabled"))
    graph = build_video_pipeline(model, fetcher, model_name="stub")

    out = await graph.ainvoke(VideoState(video_id=VID))
    assert out["error"] == "captions are disabled"
    assert out["distillation"] is None
    assert model.structured_calls == 0


async def test_transient_fetch_failure_is_marked_retryable():
    fetcher = StubFetcher(error=VideoProcessingError("YouTube is blocking", permanent=False))
    graph = build_video_pipeline(StubModel([]), fetcher, model_name="stub")
    out = await graph.ainvoke(VideoState(video_id=VID))
    assert out["permanent"] is False


async def test_unexpected_fetch_error_is_retryable_not_fatal():
    fetcher = StubFetcher(error=RuntimeError("socket exploded"))
    graph = build_video_pipeline(StubModel([]), fetcher, model_name="stub")
    out = await graph.ainvoke(VideoState(video_id=VID))
    assert "socket exploded" in out["error"]
    assert out["permanent"] is False


class StubGraph:
    """Records what the projection was asked to write."""

    def __init__(self) -> None:
        self.failures: list[tuple] = []
        self.videos: list[tuple] = []

    async def mark_video_failed(self, thought_id, reason, permanent) -> None:
        self.failures.append((thought_id, reason, permanent))

    async def upsert_video(self, meta, thought_id=None, *, is_generated=None) -> None:
        self.videos.append((meta.video_id, thought_id, is_generated))

    async def replace_notes(self, video_id, notes) -> int:
        return len(notes)


async def test_permanent_failure_is_recorded_against_the_thought():
    """Otherwise a dead video is re-queued on every restart, forever."""
    from uuid import uuid4

    from vos.projection import process_video

    thought_id = uuid4()
    fetcher = StubFetcher(error=VideoProcessingError("captions are disabled"))
    pipeline = build_video_pipeline(StubModel([]), fetcher, model_name="stub")
    graph = StubGraph()

    result = await process_video(pipeline, graph, VID, thought_id)  # type: ignore[arg-type]

    assert result.permanent is True
    assert graph.failures == [(thought_id, "captions are disabled", True)]


async def test_transient_failure_is_recorded_as_retryable():
    from uuid import uuid4

    from vos.projection import process_video

    thought_id = uuid4()
    fetcher = StubFetcher(error=VideoProcessingError("YouTube is blocking", permanent=False))
    pipeline = build_video_pipeline(StubModel([]), fetcher, model_name="stub")
    graph = StubGraph()

    result = await process_video(pipeline, graph, VID, thought_id)  # type: ignore[arg-type]

    assert result.permanent is False
    assert graph.failures[0][2] is False


async def test_provenance_reaches_the_projection_and_the_result():
    from vos.projection import process_video

    artifact = _artifact()
    artifact.is_generated = True
    model = StubModel([_distillation(("a claim", 10))])
    pipeline = build_video_pipeline(model, StubFetcher(artifact), model_name="stub")
    graph = StubGraph()

    result = await process_video(pipeline, graph, VID)  # type: ignore[arg-type]

    assert result.is_generated is True
    assert graph.videos == [(VID, None, True)]


async def test_one_failing_chunk_does_not_lose_the_others():
    """Partial knowledge beats none."""
    model = StubModel(
        [
            _distillation(("first survives", 10)),
            RuntimeError("rate limited"),
            _distillation(("third survives", 900)),
        ]
    )
    # Enough segments to force three chunks.
    artifact = _artifact(segment_count=200)
    graph = build_video_pipeline(model, StubFetcher(artifact), model_name="stub")

    out = await graph.ainvoke(VideoState(video_id=VID))
    texts = [n.text for n in out["distillation"].notes]
    assert "first survives" in texts
    assert "third survives" in texts


async def test_no_usable_notes_is_an_error_not_an_empty_success():
    model = StubModel([None])
    graph = build_video_pipeline(model, StubFetcher(_artifact()), model_name="stub")
    out = await graph.ainvoke(VideoState(video_id=VID))
    assert out["error"] is not None
    assert out["distillation"] is None


# --- long videos ---------------------------------------------------------------- #


async def test_long_transcript_is_truncated_and_says_so():
    """Silent truncation would read as 'I covered the whole video'."""
    model = StubModel([_distillation((f"claim {i}", i * 10)) for i in range(50)])
    artifact = _artifact(segment_count=2000)
    graph = build_video_pipeline(model, StubFetcher(artifact), model_name="stub", max_chunks=2)

    out = await graph.ainvoke(VideoState(video_id=VID))
    assert out["truncated"] is True
    assert model.structured_calls == 2


async def test_note_count_is_capped():
    model = StubModel([_distillation(*[(f"claim {i}", i * 10) for i in range(30)])])
    graph = build_video_pipeline(
        model, StubFetcher(_artifact()), model_name="stub", max_notes=5
    )
    out = await graph.ainvoke(VideoState(video_id=VID))
    assert len(out["distillation"].notes) == 5


# --- deduplication ---------------------------------------------------------------- #


def test_dedupe_collapses_claims_repeated_across_overlapping_chunks():
    notes = [
        VideoNote(text="The one-way speed of light cannot be measured", t_seconds=134),
        VideoNote(text="the one-way speed of light cannot be measured", t_seconds=140),
        VideoNote(text="Einstein synchronisation hides the asymmetry", t_seconds=460),
    ]
    result = _dedupe(notes, limit=10)
    assert len(result) == 2
    assert result[0].t_seconds == 134  # earliest occurrence wins


def test_dedupe_keeps_genuinely_different_claims():
    notes = [
        VideoNote(text="Claim about relativity and measurement", t_seconds=10),
        VideoNote(text="Different claim about clocks entirely", t_seconds=20),
    ]
    assert len(_dedupe(notes, limit=10)) == 2


def test_dedupe_respects_the_limit():
    notes = [VideoNote(text=f"claim number {i}", t_seconds=i) for i in range(20)]
    assert len(_dedupe(notes, limit=5)) == 5


# --- cassette -------------------------------------------------------------------- #


async def test_video_run_is_recorded(tmp_path: Path):
    from vos.contracts import CaptureRecord

    record = CaptureRecord.create(
        chat_id=1, message_id=1, text=f"https://youtu.be/{VID}", captured_at=datetime.now(UTC)
    )
    cassette = Cassette(tmp_path / "c")
    model = StubModel([_distillation(("a claim", 10))])
    graph = build_video_pipeline(
        model, StubFetcher(_artifact()), model_name="stub", cassette=cassette
    )

    await graph.ainvoke(VideoState(video_id=VID, thought_id=record.id))
    entry = cassette.latest(record.id)
    assert entry is not None
    assert entry.response["note_count"] == 1
