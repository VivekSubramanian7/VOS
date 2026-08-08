"""Rendering, including the length ceiling Telegram enforces.

`split_message` is the reason this file exists. Telegram rejects any message over 4096
characters, and VOS had no guard: `/recent 50` renders roughly 7,700 characters with no
model involved, and in the video path the send happens *after* the notes are committed
to the graph — so the failure reported work that had actually succeeded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from vos.contracts import (
    ThoughtView,
    VideoDistillation,
    VideoMeta,
    VideoNote,
    VideoResult,
)
from vos.render import (
    TELEGRAM_LIMIT,
    render_notes,
    render_thoughts,
    render_video,
    split_message,
    telegram_len,
)


def _views(n: int, text: str = "x" * 200) -> list[ThoughtView]:
    return [
        ThoughtView(
            id=uuid4(),
            text=text,
            category="Shopping",
            status="classified",
            created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        )
        for _ in range(n)
    ]


def _result(**kw) -> VideoResult:
    return VideoResult(
        video_id="dQw4w9WgXcQ",
        meta=VideoMeta(
            video_id="dQw4w9WgXcQ", url="https://youtu.be/dQw4w9WgXcQ", title="T", channel="C"
        ),
        distillation=VideoDistillation(
            summary="A summary.", notes=[VideoNote(text="a claim", t_seconds=10)]
        ),
        note_count=1,
        **kw,
    )


# -- length accounting ------------------------------------------------------ #


def test_telegram_len_counts_utf16_not_code_points():
    """The whole reason the helper exists.

    Telegram measures UTF-16 code units. A non-BMP emoji is one Python character but
    two of those — and every VOS message leads with one, so `len()` under-counts
    exactly the strings we send.
    """
    assert len("📺") == 1
    assert telegram_len("📺") == 2


def test_emoji_heavy_text_still_splits_under_the_limit():
    text = "\n".join("📺" * 500 for _ in range(10))
    for chunk in split_message(text):
        assert telegram_len(chunk) <= TELEGRAM_LIMIT


# -- splitting -------------------------------------------------------------- #


def test_short_text_is_one_chunk_unchanged():
    assert split_message("hello") == ["hello"]


def test_long_text_splits_on_line_boundaries():
    text = "\n".join(f"line {i} " + "y" * 100 for i in range(200))
    chunks = split_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert telegram_len(chunk) <= TELEGRAM_LIMIT
    # Rejoining must reproduce the input exactly: no line dropped, none duplicated.
    assert "\n".join(chunks) == text


def test_a_single_overlong_line_is_hard_split():
    """No line boundary to use — it must still terminate and stay under the limit."""
    chunks = split_message("z" * (TELEGRAM_LIMIT * 2 + 50))
    assert len(chunks) == 3
    for chunk in chunks:
        assert telegram_len(chunk) <= TELEGRAM_LIMIT
    assert "".join(chunks) == "z" * (TELEGRAM_LIMIT * 2 + 50)


def test_recent_50_would_have_been_rejected_and_now_splits():
    """The regression that needed no LLM to trigger: /recent accepts up to 50."""
    rendered = render_thoughts(_views(50), "Last 50", "empty")
    assert telegram_len(rendered) > 4096
    assert len(split_message(rendered)) > 1


def test_notes_search_result_splits():
    from vos.contracts import NoteView

    notes = [
        NoteView(
            id=uuid4(),
            text="c" * 400,
            t_seconds=i * 30,
            video_id="dQw4w9WgXcQ",
            video_title="A long video title that also costs characters",
            url="https://youtu.be/dQw4w9WgXcQ",
        )
        for i in range(15)
    ]
    rendered = render_notes(notes, "Notes")
    assert telegram_len(rendered) > 4096
    assert len(split_message(rendered)) > 1


# -- transcript provenance -------------------------------------------------- #


def test_auto_caption_caveat_shown_for_asr():
    assert "Auto-generated captions" in render_video(_result(is_generated=True))


def test_no_caveat_for_human_subtitles():
    assert "Auto-generated captions" not in render_video(_result(is_generated=False))


def test_no_caveat_when_provenance_is_unknown():
    """`None` means the artifact predates the flag. A caveat on every video is noise."""
    assert "Auto-generated captions" not in render_video(_result(is_generated=None))
