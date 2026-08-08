"""Video fetching, URL parsing, and transcript chunking — all offline.

`extract_video_id` gets exhaustive treatment because getting it wrong fails in the worst
way available: silently processing nothing, or the wrong video.

The caching tests matter for a subtler reason. A transcript is the one derived artifact
in VOS that cannot be recomputed — the upstream can be deleted or lose its captions. If
the cache silently stops working, nothing breaks today and `/redistil` fails in a year.
"""

from __future__ import annotations

import json
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
    first_tail = chunks[0][1][-200:]
    assert first_tail.split()[-1] in chunks[1][1]
    # Overlap means the second chunk starts before the first one ended.
    assert chunks[1][0] < int(_segments(200)[-1].start)


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
