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
    NoteView,
    PostView,
    PulseDigest,
    PulsePost,
    PulseResult,
    ThoughtView,
    VideoDistillation,
    VideoMeta,
    VideoNote,
    VideoResult,
    post_id,
)
from vos.render import (
    SHOWN_NOTES,
    TELEGRAM_LIMIT,
    render_all_notes,
    render_all_posts,
    render_notes,
    render_pulse,
    render_search,
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


# -- note layout ------------------------------------------------------------ #


def _with_notes(notes: list[VideoNote], **kw) -> str:
    return render_video(
        VideoResult(
            video_id="dQw4w9WgXcQ",
            meta=VideoMeta(
                video_id="dQw4w9WgXcQ",
                url="https://youtu.be/dQw4w9WgXcQ",
                title="T",
                channel="C",
            ),
            distillation=VideoDistillation(summary="S", notes=notes),
            note_count=len(notes),
            **kw,
        )
    )


def test_timestamp_precedes_the_claim():
    """Times form a scannable column instead of trailing off a wrapped line."""
    out = _with_notes([VideoNote(text="a claim about things", t_seconds=48)])
    line = next(ln for ln in out.split("\n") if "a claim about things" in ln)
    assert line.startswith('<a href="https://youtu.be/dQw4w9WgXcQ?t=48">[0:48]</a> ')


def test_notes_are_separated_by_a_blank_line():
    out = _with_notes(
        [
            VideoNote(text="first claim", t_seconds=10, section="S"),
            VideoNote(text="second claim", t_seconds=20, section="S"),
        ]
    )
    body = out[out.index("first claim") : out.index("second claim")]
    assert "\n\n" in body


def test_section_heading_is_emitted_once_per_group():
    out = _with_notes(
        [
            VideoNote(text="claim one", t_seconds=10, section="Pricing"),
            VideoNote(text="claim two", t_seconds=20, section="Pricing"),
            VideoNote(text="claim three", t_seconds=30, section="Performance"),
        ]
    )
    assert out.count("<b>Pricing</b>") == 1
    assert out.count("<b>Performance</b>") == 1


def test_sections_are_ordered_by_their_earliest_note():
    out = _with_notes(
        [
            VideoNote(text="late pricing point", t_seconds=90, section="Pricing"),
            VideoNote(text="early setup point", t_seconds=5, section="Setup"),
        ]
    )
    assert out.index("<b>Setup</b>") < out.index("<b>Pricing</b>")


def test_notes_within_a_section_stay_in_time_order():
    out = _with_notes(
        [
            VideoNote(text="later claim", t_seconds=80, section="Pricing"),
            VideoNote(text="earlier claim", t_seconds=20, section="Pricing"),
        ]
    )
    assert out.index("earlier claim") < out.index("later claim")


def test_unsectioned_notes_produce_no_heading():
    """Older artifacts and models that skip the field must not render a blank heading."""
    out = _with_notes(
        [
            VideoNote(text="claim one", t_seconds=10),
            VideoNote(text="claim two", t_seconds=20),
        ]
    )
    assert "<b>None</b>" not in out
    assert "<b></b>" not in out
    assert "claim one" in out and "claim two" in out


# -- the note cap ----------------------------------------------------------- #


def test_only_the_top_scoring_notes_are_shown():
    # Zero-padded so no label is a prefix of another — "claim 1" would otherwise
    # match "claim 12" and the assertion would pass for the wrong reason.
    notes = [
        VideoNote(text=f"claim {i:02d}", t_seconds=i * 10, score=i / 20) for i in range(20)
    ]
    out = _with_notes(notes)
    shown = [i for i in range(20) if f"claim {i:02d}" in out]
    assert len(shown) == SHOWN_NOTES
    # The highest scores are the highest indices here.
    assert shown == list(range(20 - SHOWN_NOTES, 20))


def test_shown_notes_still_read_in_time_order():
    """Score picks which claims appear; it must not reorder them."""
    notes = [
        VideoNote(text="late high value", t_seconds=900, score=0.9),
        VideoNote(text="early low value", t_seconds=10, score=0.6),
    ]
    out = _with_notes(notes)
    assert out.index("early low value") < out.index("late high value")


def test_hidden_notes_are_reported_and_point_at_more():
    """Silence here reads as "that was everything" — the doubt that prompted this."""
    notes = [VideoNote(text=f"claim {i}", t_seconds=i * 10) for i in range(11)]
    out = _with_notes(notes)
    assert f"Showing the top {SHOWN_NOTES} of 11 claims" in out
    assert "/more" in out


def test_nothing_said_when_everything_fits():
    out = _with_notes([VideoNote(text="a claim", t_seconds=10)])
    assert "/more" not in out


# -- /more ------------------------------------------------------------------ #


def _note_view(text: str, t: int, section: str | None = None) -> NoteView:
    return NoteView(
        id=uuid4(),
        text=text,
        t_seconds=t,
        video_id="dQw4w9WgXcQ",
        video_title="A Talk",
        url="https://youtu.be/dQw4w9WgXcQ",
        section=section,
    )


def test_more_renders_every_note():
    notes = [_note_view(f"claim {i}", i * 10) for i in range(20)]
    out = render_all_notes(notes)
    assert all(f"claim {i}" in out for i in range(20))
    assert "All 20 claims" in out


def test_more_uses_the_same_layout_as_the_video_reply():
    """Shared `_note_lines`, so the two cannot drift apart."""
    out = render_all_notes([_note_view("a claim", 48, section="Pricing")])
    assert "<b>Pricing</b>" in out
    line = next(ln for ln in out.split("\n") if "a claim" in ln)
    assert line.startswith('<a href="https://youtu.be/dQw4w9WgXcQ?t=48">[0:48]</a> ')


def test_more_on_a_video_with_no_notes():
    assert "No notes stored" in render_all_notes([])


def _pulse_post(text: str, score: float = 0.5, section: str | None = None, n: int = 1):
    return PulsePost(
        text=text,
        author_handle="@karpathy",
        url=f"https://x.com/karpathy/status/{n}",
        section=section,
        score=score,
    )


def _pulse(*posts: PulsePost, **kw) -> str:
    return render_pulse(
        PulseResult(
            topic="AI",
            digest=PulseDigest(
                topic="AI",
                summary="A summary.",
                posts=list(posts),
                asked_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            ),
            post_count=len(posts),
            **kw,
        )
    )


def test_a_post_line_links_the_handle_to_the_post():
    """The handle is the post's deep link — what makes the claim checkable."""
    out = _pulse(_pulse_post("something shipped", n=99))
    line = next(ln for ln in out.split("\n") if "something shipped" in ln)
    assert line.startswith(
        '<a href="https://x.com/karpathy/status/99">@karpathy</a> '
    )


def test_posts_are_ordered_by_score_not_arrival():
    """Posts have no time axis, so score orders them as well as selecting them."""
    out = _pulse(_pulse_post("weak", score=0.2), _pulse_post("strong", score=0.9, n=2))
    assert out.index("strong") < out.index("weak")


def test_only_the_top_posts_are_shown():
    posts = [_pulse_post(f"post {i:02d}", score=i / 20, n=i) for i in range(20)]
    out = _pulse(*posts)
    shown = [i for i in range(20) if f"post {i:02d}" in out]
    assert len(shown) == SHOWN_NOTES


def test_hidden_posts_point_at_more():
    posts = [_pulse_post(f"post {i:02d}", n=i) for i in range(11)]
    out = _pulse(*posts)
    assert f"Showing the top {SHOWN_NOTES} of 11 posts" in out
    assert "/more" in out


def test_sections_group_posts():
    out = _pulse(
        _pulse_post("one", section="Releases", n=1),
        _pulse_post("two", section="Releases", n=2),
        _pulse_post("three", section="Papers", n=3),
    )
    assert out.count("<b>Releases</b>") == 1
    assert out.count("<b>Papers</b>") == 1


def test_sections_are_ordered_by_their_best_post():
    out = _pulse(
        _pulse_post("meh", section="Opinion", score=0.2, n=1),
        _pulse_post("big", section="Releases", score=0.9, n=2),
    )
    assert out.index("<b>Releases</b>") < out.index("<b>Opinion</b>")


def test_dropped_items_are_reported():
    """Silence would read as "that was everything"."""
    assert "2" in _pulse(_pulse_post("a"), dropped=2)
    assert "dropped" in _pulse(_pulse_post("a"), dropped=2).casefold()


def test_nothing_is_said_when_nothing_was_dropped():
    assert "dropped" not in _pulse(_pulse_post("a")).casefold()


def test_the_cost_is_shown():
    """Spend has to be visible at the moment it happens, not only in /stats."""
    assert "$0.63" in _pulse(_pulse_post("a"), cost_usd=0.625)


def test_a_quiet_day_is_stated_plainly():
    assert "Nothing" in _pulse()


def test_a_failed_pulse_renders_its_reason():
    out = render_pulse(PulseResult(topic="AI", error="xAI request failed: TimeoutError"))
    assert "TimeoutError" in out


def test_a_long_digest_splits():
    rendered = render_all_posts(
        [
            PostView(
                id=post_id(f"https://x.com/a/status/{i}"),
                text="y" * 280,
                author_handle="@karpathy",
                url=f"https://x.com/a/status/{i}",
                topic="AI",
                asked_at=datetime(2026, 8, 9, tzinfo=UTC),
            )
            for i in range(40)
        ]
    )
    assert telegram_len(rendered) > TELEGRAM_LIMIT
    assert len(split_message(rendered)) > 1


def _post_view(text: str, n: int = 1, section: str | None = None) -> PostView:
    return PostView(
        id=post_id(f"https://x.com/karpathy/status/{n}"),
        text=text,
        author_handle="@karpathy",
        url=f"https://x.com/karpathy/status/{n}",
        section=section,
        topic="AI",
        asked_at=datetime(2026, 8, 9, tzinfo=UTC),
    )


def test_more_renders_every_post():
    out = render_all_posts([_post_view(f"post {i}", n=i) for i in range(20)])
    assert all(f"post {i}" in out for i in range(20))
    assert "All 20 posts" in out


def test_more_on_an_empty_pulse():
    assert "No posts" in render_all_posts([])


def test_search_shows_both_kinds():
    notes = [_note_view("from a video", 10)]
    posts = [_post_view("from x")]
    out = render_search(notes, posts, "term")
    assert "from a video" in out and "from x" in out


def test_search_omits_a_group_that_found_nothing():
    out = render_search([], [_post_view("from x")], "term")
    assert "from x" in out
    assert "video" not in out.casefold()


def test_search_with_no_results_at_all():
    assert "No" in render_search([], [], "term")
