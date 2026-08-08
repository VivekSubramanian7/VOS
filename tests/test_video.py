"""Video fetching, URL parsing, and transcript chunking — all offline.

`extract_video_id` gets exhaustive treatment because getting it wrong fails in the worst
way available: silently processing nothing, or the wrong video.

The caching tests matter for a subtler reason. A transcript is the one derived artifact
in VOS that cannot be recomputed — the upstream can be deleted or lose its captions. If
the cache silently stops working, nothing breaks today and `/redistil` fails in a year.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vos.contracts import (
    TranscriptSegment,
    VideoArtifact,
    VideoMeta,
    VideoProcessingError,
    note_id,
)
from vos.video import VideoFetcher, chunk_transcript, extract_video_id, find_video_id

VID = "dQw4w9WgXcQ"


# --- URL parsing ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}",
        f"http://www.youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&t=42s",
        f"https://www.youtube.com/watch?list=PL123&v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?t=90",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"https://www.youtube.com/v/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"www.youtube.com/watch?v={VID}",
        f"youtube.com/watch?v={VID}",
        VID,
    ],
)
def test_extract_video_id_accepts_every_youtube_shape(url: str):
    assert extract_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "https://example.com/watch?v=abc",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/@veritasium",
        "https://www.youtube.com/playlist?list=PL123",
        "just some words",
        "",
    ],
)
def test_extract_video_id_rejects_everything_else(url: str):
    assert extract_video_id(url) is None


def test_find_video_id_inside_a_sentence():
    assert find_video_id(f"watch this later https://youtu.be/{VID} looks good") == VID


def test_find_video_id_strips_trailing_punctuation():
    """Links written inside prose usually end in punctuation."""
    assert find_video_id(f"see https://youtu.be/{VID}.") == VID
    assert find_video_id(f"(https://youtu.be/{VID})") == VID
    assert find_video_id(f"link: https://youtu.be/{VID}!") == VID


def test_find_video_id_returns_none_without_a_link():
    assert find_video_id("no link here, just a thought about videos") is None


# --- chunking ---------------------------------------------------------------- #


def _segments(count: int, words: int = 20) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(text=" ".join(["word"] * words), start=float(i * 5), duration=5.0)
        for i in range(count)
    ]


def test_chunk_empty_transcript():
    assert chunk_transcript([]) == []


def test_short_transcript_is_one_chunk():
    chunks = chunk_transcript(_segments(5))
    assert len(chunks) == 1
    assert chunks[0][0] == 0


def test_long_transcript_splits_and_carries_timestamps():
    chunks = chunk_transcript(_segments(400), target_chars=2_000, overlap_chars=200)
    assert len(chunks) > 1
    starts = [start for start, _ in chunks]
    assert starts == sorted(starts)
    assert starts[0] == 0


def test_chunks_overlap_so_boundary_claims_survive():
    """A claim spoken across a window boundary must appear in both windows."""
    chunks = chunk_transcript(_segments(200), target_chars=2_000, overlap_chars=400)
    # Compare speech, not markers: a marker is chunk-relative and legitimately differs.
    first_tail = [w for w in chunks[0][1][-200:].split() if not w.startswith("[t=")]
    assert first_tail[-1] in chunks[1][1]
    # Overlap means the second chunk starts before the first one ended.
    assert chunks[1][0] < int(_segments(200)[-1].start)


# --- timestamp markers ------------------------------------------------------- #
#
# Without these every note in a chunk carries the same timestamp — a 93-second video
# produced three notes all pointing at 0:00, which is what prompted this.


def _markers(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"\[t=(\d+)\]", text)]


def test_every_chunk_opens_with_a_marker():
    chunks = chunk_transcript(_segments(400), target_chars=2_000, overlap_chars=200)
    for start, text in chunks:
        assert text.startswith(f"[t={start}]")


def test_markers_appear_at_the_requested_interval():
    # Segments are 5s apart, so a 30s interval means roughly every 6th segment.
    _, text = chunk_transcript(_segments(20), marker_interval_s=30)[0]
    assert _markers(text) == [0, 30, 60, 90]


def test_marker_interval_is_tunable():
    _, text = chunk_transcript(_segments(20), marker_interval_s=15)[0]
    assert _markers(text) == [0, 15, 30, 45, 60, 75, 90]


def test_markers_increase_and_match_a_real_segment_start():
    _, text = chunk_transcript(_segments(40), marker_interval_s=30)[0]
    marks = _markers(text)
    assert marks == sorted(marks)
    starts = {int(s.start) for s in _segments(40)}
    assert all(m in starts for m in marks)


def test_a_short_video_still_gets_several_distinct_anchors():
    """The regression: one anchor means every note lands on the same second."""
    # ~93 seconds, as in the video that surfaced this.
    segments = [
        TranscriptSegment(text="some spoken words here", start=float(i) * 2.4, duration=2.4)
        for i in range(40)
    ]
    _, text = chunk_transcript(segments)[0]
    assert len(set(_markers(text))) >= 3


def test_marker_characters_count_toward_the_window_budget():
    """Otherwise the window quietly outgrows the limit that keeps it inside a context."""
    chunks = chunk_transcript(_segments(400), target_chars=2_000, overlap_chars=200)
    # Some slack for the final segment that crosses the threshold, but not unbounded.
    assert all(len(text) < 2_400 for _, text in chunks)


# --- fetching and caching ----------------------------------------------------- #


def _artifact(video_id: str = VID) -> VideoArtifact:
    return VideoArtifact(
        meta=VideoMeta(
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            title="Test Video",
            channel="Test Channel",
        ),
        segments=[TranscriptSegment(text="hello world", start=0.0, duration=2.0)],
        fetched_at=datetime.now(UTC),
    )


@pytest.fixture
def fetcher(tmp_path: Path) -> VideoFetcher:
    return VideoFetcher(tmp_path / "artifacts")


def _stub(fetcher: VideoFetcher, monkeypatch, calls: list[str]):
    def fake_transcript(video_id: str):
        calls.append(video_id)
        return [TranscriptSegment(text="hello world", start=0.0, duration=2.0)], "en", True

    async def fake_meta(video_id: str):
        return VideoMeta(
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            title="Test Video",
            channel="Test Channel",
        )

    monkeypatch.setattr(fetcher, "_fetch_transcript", fake_transcript)
    monkeypatch.setattr(fetcher, "_fetch_meta", fake_meta)


async def test_fetch_writes_cache(fetcher: VideoFetcher, monkeypatch, tmp_path: Path):
    _stub(fetcher, monkeypatch, [])
    artifact = await fetcher.fetch(VID)
    assert artifact.meta.title == "Test Video"
    assert (tmp_path / "artifacts" / "videos" / f"{VID}.json").exists()


async def test_transcript_provenance_is_recorded(fetcher: VideoFetcher, monkeypatch):
    """ASR-derived notes must be distinguishable from human-subtitle ones."""
    _stub(fetcher, monkeypatch, [])
    artifact = await fetcher.fetch(VID)
    assert artifact.is_generated is True


async def test_provenance_survives_the_cache_round_trip(fetcher: VideoFetcher, monkeypatch):
    _stub(fetcher, monkeypatch, [])
    await fetcher.fetch(VID)
    assert fetcher.cached(VID).is_generated is True  # type: ignore[union-attr]


async def test_artifact_cached_before_the_flag_existed_reads_back_unknown(
    fetcher: VideoFetcher, tmp_path: Path
):
    """`None`, never `False`.

    The cache is write-once, so artifacts predating this field will be read for years.
    Defaulting to False would claim "human subtitles" about a video nothing checked.
    """
    path = tmp_path / "artifacts" / "videos" / f"{VID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "video_id": VID,
                    "url": "u",
                    "title": "t",
                    "channel": "c",
                    "approx_duration_s": 2,
                },
                "segments": [{"text": "hello", "start": 0.0, "duration": 2.0}],
                "fetched_at": "2026-01-01T00:00:00Z",
                "language": "en",
            }
        ),
        encoding="utf-8",
    )
    artifact = fetcher.cached(VID)
    assert artifact is not None
    assert artifact.is_generated is None


async def test_second_fetch_uses_cache_and_does_not_refetch(
    fetcher: VideoFetcher, monkeypatch
):
    """The property that keeps /redistil working after a video is taken down."""
    calls: list[str] = []
    _stub(fetcher, monkeypatch, calls)
    await fetcher.fetch(VID)
    await fetcher.fetch(VID)
    assert calls == [VID]


async def test_refresh_forces_a_refetch(fetcher: VideoFetcher, monkeypatch):
    calls: list[str] = []
    _stub(fetcher, monkeypatch, calls)
    await fetcher.fetch(VID)
    await fetcher.fetch(VID, refresh=True)
    assert calls == [VID, VID]


async def test_duration_is_derived_from_the_last_segment(fetcher: VideoFetcher, monkeypatch):
    def fake_transcript(video_id: str):
        return (
            [
                TranscriptSegment(text="a", start=0.0, duration=5.0),
                TranscriptSegment(text="b", start=100.0, duration=8.0),
            ],
            "en",
            False,
        )

    async def fake_meta(video_id: str):
        return VideoMeta(video_id=video_id, url="u", title="t", channel="c")

    monkeypatch.setattr(fetcher, "_fetch_transcript", fake_transcript)
    monkeypatch.setattr(fetcher, "_fetch_meta", fake_meta)
    artifact = await fetcher.fetch(VID)
    assert artifact.meta.approx_duration_s == 108


def test_corrupt_cache_is_ignored_not_fatal(fetcher: VideoFetcher, tmp_path: Path):
    path = tmp_path / "artifacts" / "videos" / f"{VID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert fetcher.cached(VID) is None


def test_cached_returns_none_when_absent(fetcher: VideoFetcher):
    assert fetcher.cached("nothinghere") is None


async def test_fetch_failure_propagates_a_user_facing_reason(
    fetcher: VideoFetcher, monkeypatch
):
    def boom(video_id: str):
        raise VideoProcessingError("captions are disabled for this video")

    monkeypatch.setattr(fetcher, "_fetch_transcript", boom)
    with pytest.raises(VideoProcessingError) as exc:
        await fetcher.fetch(VID)
    assert "captions are disabled" in exc.value.reason
    assert exc.value.permanent is True


def test_artifact_full_text_joins_segments():
    artifact = VideoArtifact(
        meta=VideoMeta(video_id=VID, url="u", title="t", channel="c"),
        segments=[
            TranscriptSegment(text="hello", start=0.0, duration=1.0),
            TranscriptSegment(text="  ", start=1.0, duration=1.0),
            TranscriptSegment(text="world", start=2.0, duration=1.0),
        ],
        fetched_at=datetime.now(UTC),
    )
    assert artifact.full_text == "hello world"


# --- note identity ------------------------------------------------------------ #


def test_note_id_is_deterministic():
    assert note_id(VID, 42, "a claim") == note_id(VID, 42, "a claim")
    assert note_id(VID, 42, " a claim ") == note_id(VID, 42, "a claim")


def test_note_id_varies_with_video_time_and_text():
    base = note_id(VID, 42, "a claim")
    assert base != note_id("other______", 42, "a claim")
    assert base != note_id(VID, 43, "a claim")
    assert base != note_id(VID, 42, "another claim")
