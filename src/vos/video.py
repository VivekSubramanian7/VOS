"""Fetching YouTube metadata and transcripts, with an on-disk cache.

Two things here are load-bearing:

`extract_video_id` — YouTube has many URL shapes and getting this wrong means silently
processing the wrong video or nothing at all, so it is exhaustively tested rather than
regex-guessed.

`VideoFetcher.fetch` — caches to `artifacts/videos/{id}.json` and never re-fetches what
it already holds. That cache is not an optimisation: a transcript is the one derived
artifact in VOS that cannot be recomputed, because the upstream can be deleted or have
captions switched off. Re-distilling a two-year-old video with a better prompt has to
read from disk or it will simply fail on the videos you most want back.

No API key is required for either call: transcripts come from YouTube's caption
endpoints via `youtube-transcript-api`, and title/channel come from the unauthenticated
oEmbed endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from vos.contracts import (
    TranscriptSegment,
    VideoArtifact,
    VideoMeta,
    VideoProcessingError,
)

log = logging.getLogger(__name__)

OEMBED = "https://www.youtube.com/oembed"

_ID = r"[A-Za-z0-9_-]{11}"
_PATH_PATTERNS = (
    re.compile(rf"^/shorts/(?P<id>{_ID})"),
    re.compile(rf"^/embed/(?P<id>{_ID})"),
    re.compile(rf"^/live/(?P<id>{_ID})"),
    re.compile(rf"^/v/(?P<id>{_ID})"),
)
_BARE_ID = re.compile(rf"^{_ID}$")
_URL_IN_TEXT = re.compile(r"https?://\S+")


def extract_video_id(value: str) -> str | None:
    """Return the 11-character video ID from a URL, or None if it isn't YouTube.

    Handles: watch?v=, youtu.be/, /shorts/, /embed/, /live/, /v/, extra query params,
    and a bare ID.
    """
    value = value.strip()
    if _BARE_ID.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")

    if host in ("youtu.be",):
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _BARE_ID.match(candidate) else None

    if host not in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        return None

    if parsed.path == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        return values[0] if values and _BARE_ID.match(values[0]) else None

    for pattern in _PATH_PATTERNS:
        if match := pattern.match(parsed.path):
            return match.group("id")

    return None


def find_video_id(text: str) -> str | None:
    """First YouTube video ID appearing anywhere in a free-text thought."""
    for candidate in _URL_IN_TEXT.findall(text):
        # Trailing punctuation is common when a link is written inside a sentence.
        if video_id := extract_video_id(candidate.rstrip(".,;:!?)>\"'")):
            return video_id
    return None


class VideoFetcher:
    """Metadata + transcript, cached on disk."""

    def __init__(self, artifact_dir: Path, *, languages: tuple[str, ...] = ("en",)) -> None:
        self._dir = Path(artifact_dir) / "videos"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._languages = languages

    def _path(self, video_id: str) -> Path:
        return self._dir / f"{video_id}.json"

    def cached(self, video_id: str) -> VideoArtifact | None:
        path = self._path(video_id)
        if not path.exists():
            return None
        try:
            return VideoArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("Ignoring unreadable cached transcript %s: %s", path.name, exc)
            return None

    async def fetch(self, video_id: str, *, refresh: bool = False) -> VideoArtifact:
        """Return the artifact, from cache unless `refresh`.

        Raises `VideoProcessingError` with a user-facing reason on failure.
        """
        if not refresh and (artifact := self.cached(video_id)) is not None:
            log.info("Using cached transcript for %s", video_id)
            return artifact

        segments, language, is_generated = await asyncio.to_thread(
            self._fetch_transcript, video_id
        )
        meta = await self._fetch_meta(video_id)
        meta.approx_duration_s = (
            int(segments[-1].start + segments[-1].duration) if segments else 0
        )

        artifact = VideoArtifact(
            meta=meta,
            segments=segments,
            fetched_at=datetime.now(UTC),
            language=language,
            is_generated=is_generated,
        )
        self._write(artifact)
        return artifact

    def _write(self, artifact: VideoArtifact) -> None:
        try:
            self._path(artifact.meta.video_id).write_text(
                artifact.model_dump_json(indent=1), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            # Not fatal for this run, but it does mean the transcript is unrecoverable
            # if the video later disappears — so it is a warning, not a debug line.
            log.warning("Could not cache transcript for %s: %s", artifact.meta.video_id, exc)

    def _fetch_transcript(
        self, video_id: str
    ) -> tuple[list[TranscriptSegment], str, bool | None]:
        """Blocking; called via `to_thread`. Translates library errors into one
        exception type carrying a message worth showing the user.

        Also reports whether the track was machine-transcribed. The library prefers
        human-written subtitles and only falls back to ASR, so this is a property of
        what we actually got, not of the video.
        """
        from youtube_transcript_api import (
            AgeRestricted,
            CouldNotRetrieveTranscript,
            InvalidVideoId,
            IpBlocked,
            NoTranscriptFound,
            RequestBlocked,
            TranscriptsDisabled,
            VideoUnavailable,
            VideoUnplayable,
            YouTubeTranscriptApi,
        )

        try:
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=self._languages)
        except TranscriptsDisabled as exc:
            raise VideoProcessingError("captions are disabled for this video") from exc
        except NoTranscriptFound as exc:
            raise VideoProcessingError(
                f"no transcript in {'/'.join(self._languages)}"
            ) from exc
        except (VideoUnavailable, InvalidVideoId) as exc:
            raise VideoProcessingError("video is unavailable, private, or deleted") from exc
        except AgeRestricted as exc:
            raise VideoProcessingError("video is age-restricted") from exc
        except VideoUnplayable as exc:
            raise VideoProcessingError("video cannot be played (region-locked?)") from exc
        except (IpBlocked, RequestBlocked) as exc:
            # Transient: YouTube throttling this IP. Worth retrying later.
            raise VideoProcessingError(
                "YouTube is currently blocking transcript requests", permanent=False
            ) from exc
        except CouldNotRetrieveTranscript as exc:
            raise VideoProcessingError(f"transcript unavailable: {exc}") from exc

        raw = fetched.to_raw_data()
        segments = [
            TranscriptSegment(
                text=item["text"], start=float(item["start"]), duration=float(item["duration"])
            )
            for item in raw
            if item.get("text", "").strip()
        ]
        if not segments:
            raise VideoProcessingError("transcript was empty")
        return (
            segments,
            getattr(fetched, "language_code", self._languages[0]),
            getattr(fetched, "is_generated", None),
        )

    async def _fetch_meta(self, video_id: str) -> VideoMeta:
        """oEmbed: title and channel, unauthenticated.

        Metadata is a nice-to-have — if it fails we still have the transcript, which is
        the part that cannot be re-fetched later. So this degrades rather than raises.
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(OEMBED, params={"url": url, "format": "json"})
                response.raise_for_status()
                data = response.json()
            return VideoMeta(
                video_id=video_id,
                url=url,
                title=data.get("title") or video_id,
                channel=data.get("author_name") or "Unknown channel",
            )
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            log.warning("oEmbed lookup failed for %s: %s", video_id, exc)
            return VideoMeta(
                video_id=video_id, url=url, title=video_id, channel="Unknown channel"
            )


def _marked_text(segments: list[TranscriptSegment], interval_s: int) -> str:
    """Join segment text, interleaving `[t=<seconds>]` position markers.

    Without these the model has nothing to cite: joining the text discards every
    per-segment start, leaving only the chunk's own, so every note in a chunk ends up
    stamped with the same second — 0:00 for a short video.

    Raw integer seconds rather than `mm:ss` because the model copies the number straight
    into `t_seconds`; asking it to convert `1:03 → 63` adds arithmetic it can get wrong
    for no benefit.
    """
    parts: list[str] = []
    next_marker = float("-inf")
    for segment in segments:
        if segment.start >= next_marker:
            parts.append(f"[t={int(segment.start)}]")
            next_marker = segment.start + interval_s
        parts.append(segment.text)
    return " ".join(parts)


def _marked_size(segments: list[TranscriptSegment], interval_s: int) -> int:
    """Character cost of `_marked_text`, markers included."""
    return len(_marked_text(segments, interval_s))


def chunk_transcript(
    segments: list[TranscriptSegment],
    *,
    target_chars: int = 12_000,
    overlap_chars: int = 600,
    marker_interval_s: int = 30,
) -> list[tuple[int, str]]:
    """Split into (start_seconds, text) windows on segment boundaries.

    Character-based rather than token-based on purpose: it needs no tokenizer, works
    identically across providers, and the target is deliberately conservative so a
    window fits comfortably in any modern context.

    The overlap exists so a claim spoken across a window boundary is not lost.

    Each window carries `[t=…]` markers roughly every `marker_interval_s` seconds, which
    is what lets a note point at the moment its claim was actually made.
    """
    if not segments:
        return []

    chunks: list[tuple[int, str]] = []
    current: list[TranscriptSegment] = []
    size = 0
    next_marker = float("-inf")

    for segment in segments:
        # Mirrors `_marked_text`: markers count toward the budget, or the window quietly
        # outgrows the limit that exists to keep it inside a context.
        if segment.start >= next_marker:
            size += len(f"[t={int(segment.start)}]") + 1
            next_marker = segment.start + marker_interval_s
        current.append(segment)
        size += len(segment.text) + 1

        if size >= target_chars:
            chunks.append((int(current[0].start), _marked_text(current, marker_interval_s)))
            # Carry the tail forward as overlap.
            kept: list[TranscriptSegment] = []
            kept_size = 0
            for s in reversed(current):
                if kept_size >= overlap_chars:
                    break
                kept.insert(0, s)
                kept_size += len(s.text) + 1
            current = kept
            # The carried tail is re-marked from its own first segment, so the running
            # size and the next marker both have to be recomputed against that list.
            size = _marked_size(kept, marker_interval_s)
            next_marker = kept[0].start + marker_interval_s if kept else float("-inf")

    if current:
        chunks.append((int(current[0].start), _marked_text(current, marker_interval_s)))
    return chunks
