"""CaptureResult and read models -> user-facing text.

Kept separate from the shell so a second client can reuse the same result objects with
different rendering, and so the wording is testable without a Telegram transport.

All user-supplied text is HTML-escaped. Thought text is arbitrary input echoed back to
the user; an unescaped `<` would break the message rather than display.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Protocol

from vos.contracts import (
    CaptureResult,
    GraphStats,
    NoteView,
    PostView,
    PulsePost,
    PulseResult,
    SourceRef,
    ThoughtView,
    VideoNote,
    VideoResult,
)

CATEGORY_LABELS: dict[str, str] = {
    "Shopping": "Shopping",
    "TripPlanning": "Trip planning",
    "Family": "Family",
    "Career": "Career",
    "StudyResearch": "Study research",
    "StockResearch": "Stock research",
    "VideoKnowledge": "Video knowledge",
    "Other": "Other",
}


def label(category: str | None) -> str:
    return CATEGORY_LABELS.get(category or "", category or "Uncategorised")


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Telegram's sendMessage ceiling is 4096; 4000 leaves room for the transport to
# disagree with us at the margin without costing a visible message.
TELEGRAM_LIMIT = 4000


def telegram_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, not Python code points.

    The two differ precisely where VOS is most exposed. Every message here leads with
    an emoji, and a non-BMP emoji is one `len()` character but two UTF-16 units — so
    `len()` under-counts the strings we actually send, in the direction that lets an
    over-long message through.
    """
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Break rendered output into sendable chunks.

    Splits on newlines because no renderer here opens an HTML tag on one line and
    closes it on another — every `<a>`, `<b>`, and `<i>` is balanced within its line.
    That invariant is what makes this safe, so a renderer that breaks it must not use
    this function. Truncating instead was the alternative and is worse: the notes are
    already committed to the graph, so dropping them from the reply would hide work
    that succeeded.
    """
    if telegram_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n".join(current))
            current, size = [], 0

    for line in text.split("\n"):
        # A single line over the limit cannot be placed by joining; hard-split it.
        # Rare (a 4000-character unbroken run), but it must not loop forever.
        while telegram_len(line) > limit:
            flush()
            cut = limit
            while cut > 0 and telegram_len(line[:cut]) > limit:
                cut -= 1
            chunks.append(line[:cut])
            line = line[cut:]

        cost = telegram_len(line) + (1 if current else 0)
        if size + cost > limit:
            flush()
            cost = telegram_len(line)
        current.append(line)
        size += cost

    flush()
    return chunks


def render_capture(result: CaptureResult) -> str:
    """The reply to a captured thought.

    Deliberately different wording per status: the user must be able to tell at a
    glance whether the thought is merely safe or actually filed.
    """
    if result.status == "unclassified":
        return (
            "📥 <b>Captured</b> — but I couldn't classify it.\n"
            f"<i>{escape(result.error or 'unknown error')}</i>\n"
            "It's safe in the journal. Use /pending to retry."
        )

    c = result.classification
    if c is None:
        return "📥 <b>Captured.</b>"

    lines = [f"✅ Filed under <b>{escape(label(c.category))}</b>"]
    if c.title:
        lines.append(f"<i>{escape(c.title)}</i>")

    if c.entities:
        names = ", ".join(escape(e.name) for e in c.entities[:6])
        lines.append(f"🔗 {names}")

    if result.linked_sources:
        lines.append(f"⭐ Follows: {', '.join(escape(s) for s in result.linked_sources)}")

    if c.confidence < 0.6:
        lines.append(f"⚠️ Low confidence ({c.confidence:.0%}) — check with /category")

    return "\n".join(lines)


def render_thoughts(views: list[ThoughtView], header: str, empty: str) -> str:
    if not views:
        return empty
    lines = [f"<b>{escape(header)}</b>"]
    for v in views:
        when = v.created_at.strftime("%d %b %H:%M")
        cat = label(v.category)
        lines.append(f"\n<code>{when}</code> · {escape(cat)}\n{escape(_clip(v.text, 120))}")
    return "\n".join(lines)


def render_stats(stats: GraphStats, spent_today: float | None = None) -> str:
    if stats.total == 0:
        return "Nothing captured yet. Send me a thought."

    lines = [f"<b>{stats.total} thoughts</b>"]
    for name, n in sorted(stats.by_category.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {escape(label(name))}: {n}")

    if stats.pending:
        lines.append(f"\n⚠️ {stats.pending} awaiting classification — /pending")

    if stats.top_sources:
        lines.append("\n<b>Most referenced</b>")
        lines.extend(f"  {escape(name)}: {n}" for name, n in stats.top_sources)

    if spent_today is not None:
        lines.append(f"\n💸 Today: ${spent_today:.3f}")

    return "\n".join(lines)


def render_following(sources: list[SourceRef]) -> str:
    if not sources:
        return (
            "You don't follow anything yet.\n\n"
            "<code>/follow person Naval Ravikant</code>\n"
            "<code>/follow book Sapiens by Harari</code>\n"
            "<code>/follow channel https://youtube.com/@veritasium</code>"
        )
    icon = {"person": "👤", "book": "📖", "channel": "📺"}
    lines = ["<b>Following</b>"]
    for s in sources:
        row = f"{icon.get(s.kind, '•')} {escape(s.name)}"
        if s.author:
            row += f" <i>by {escape(s.author)}</i>"
        lines.append(row)
    return "\n".join(lines)


def _hhmmss(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cents(usd: float) -> str:
    """Two-decimal money string, rounded half up.

    `f"{x:.2f}"` rounds half-to-even on the float's exact binary value, so an
    on-the-nose cost like $0.625 silently prints as $0.62 — the wrong direction for
    a number that is supposed to make spend visible. Routing through `Decimal` on
    the *string* repr keeps the rounding the way a human reading a receipt expects.
    """
    return str(Decimal(str(usd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# How many claims a video reply shows. Deliberately below what is stored: the rest are
# one `/more` away, which is a graph read rather than another distillation.
SHOWN_NOTES = 8

class Sectioned(Protocol):
    """Anything that carries a grouping label. Structural so both freshly extracted
    items and their read-model counterparts qualify without a shared base class."""

    section: str | None


def _by_section[N: Sectioned](
    notes: list[N], key: Callable[[N], float]
) -> list[tuple[str | None, list[N]]]:
    """Group items under their section heading, ordering preserved.

    `key` decides both the order within a section and which section comes first
    (by its leading item). Video notes pass their timestamp, because the reader is
    reconstructing a chronology. Posts have no such axis and pass negated score, so
    the most valuable thing is read first.

    Items with no section collect into a single unheaded group rather than producing
    a blank heading.
    """
    groups: dict[str | None, list[N]] = {}
    for note in sorted(notes, key=key):
        groups.setdefault(note.section or None, []).append(note)
    return sorted(groups.items(), key=lambda kv: key(kv[1][0]))


def _note_lines[N: (VideoNote, NoteView)](notes: list[N], video_id: str) -> list[str]:
    """Section headings and timestamp-led claim lines, shared by both renderers."""
    lines: list[str] = []
    for heading, group in _by_section(notes, key=lambda n: n.t_seconds):
        if heading:
            lines.append(f"\n<b>{escape(heading)}</b>")
        for note in group:
            link = f"https://youtu.be/{video_id}?t={note.t_seconds}"
            # Timestamp first: the times line up as a scannable column instead of
            # trailing off the end of a claim that wraps onto a second line.
            lines.append(
                f"\n<a href=\"{link}\">[{_hhmmss(note.t_seconds)}]</a> {escape(note.text)}"
            )
    return lines


def render_video(result: VideoResult) -> str:
    """The distillation message.

    Every note is a deep link to the second it was said — that is what makes the notes
    checkable rather than merely plausible, which is the difference between notes you
    trust and notes you skim.
    """
    if not result.ok:
        return (
            "📺 Couldn't extract knowledge from that video — "
            f"<i>{escape(result.error or 'unknown reason')}</i>.\n"
            "The thought itself is saved."
        )

    meta = result.meta
    d = result.distillation
    assert meta is not None and d is not None  # guaranteed by result.ok

    lines = [f"📺 <b>{escape(meta.title)}</b>"]
    if meta.channel:
        lines.append(f"<i>{escape(meta.channel)}</i>")
    if d.summary:
        lines.append(f"\n{escape(d.summary)}")

    if d.notes:
        # Highest-scoring few, then displayed in video order — score decides *which*
        # claims appear, never the order they are read in.
        shown = sorted(d.notes, key=lambda n: (-n.score, n.t_seconds))[:SHOWN_NOTES]
        lines.extend(_note_lines(shown, meta.video_id))
        hidden = len(d.notes) - len(shown)
        if hidden:
            # Not a loss — everything is in the graph. Saying so is what stops the reply
            # reading as "that was everything".
            lines.append(
                f"\n<i>Showing the top {len(shown)} of {len(d.notes)} claims — "
                "/more for the rest.</i>"
            )
    else:
        lines.append("\n<i>Nothing substantial enough to note.</i>")

    if result.truncated:
        lines.append("\n⚠️ Long video — only the earlier part was covered.")

    # Only when we positively know it was machine-transcribed. `None` means the
    # artifact predates the flag, and a caveat shown on every video would be read past.
    if result.is_generated is True:
        lines.append("\n🎙 Auto-generated captions — names may be mis-heard.")

    return "\n".join(lines)


def render_all_notes(notes: list[NoteView]) -> str:
    """Every stored claim for one video — the `/more` reply.

    Same layout as the original distillation so the two read as one thing, minus the
    summary and caveats, which were said once already.
    """
    if not notes:
        return "No notes stored for that video yet."

    first = notes[0]
    lines = [f"📺 <b>{escape(first.video_title)}</b>", f"<i>All {len(notes)} claims</i>"]
    lines.extend(_note_lines(notes, first.video_id))
    return "\n".join(lines)


def render_notes(notes: list[NoteView], header: str) -> str:
    if not notes:
        return "No notes match that."
    lines = [f"<b>{escape(header)}</b>"]
    for note in notes:
        lines.append(
            f"\n• {escape(note.text)}\n"
            f'  <a href="{note.deep_link}">{escape(_clip(note.video_title, 50))} '
            f"[{_hhmmss(note.t_seconds)}]</a>"
        )
    return "\n".join(lines)


def _post_lines[N: (PulsePost, PostView)](posts: list[N]) -> list[str]:
    """Handle-led claim lines, shared by the pulse reply and `/more`.

    Not `_note_lines`: that one builds YouTube deep links from a video id. Here the
    linked handle *is* the deep link, and it plays the role the timestamp plays for
    a video note — the thing you tap to check the claim.
    """
    lines: list[str] = []
    for heading, group in _by_section(posts, key=lambda p: -p.score):
        if heading:
            lines.append(f"\n<b>{escape(heading)}</b>")
        for post in group:
            # The URL is safe by construction — `canonical_post_url` rebuilds it from
            # a matched handle and a numeric id — but it is escaped anyway, because
            # "safe upstream" is the kind of thing that stops being true quietly.
            link = escape(post.url, quote=True)
            lines.append(
                f'\n<a href="{link}">{escape(post.author_handle)}</a> {escape(post.text)}'
            )
    return lines


def render_pulse(result: PulseResult) -> str:
    """The digest message."""
    if not result.ok:
        return (
            "🐦 Couldn't read X — "
            f"<i>{escape(result.error or 'unknown reason')}</i>."
        )

    d = result.digest
    assert d is not None  # guaranteed by result.ok

    lines = [f"🐦 <b>{escape(d.topic)} on X</b> — last 24 hours"]
    if d.summary:
        lines.append(f"\n{escape(d.summary)}")

    if d.posts:
        shown = sorted(d.posts, key=lambda p: -p.score)[:SHOWN_NOTES]
        lines.extend(_post_lines(shown))
        if len(d.posts) > len(shown):
            lines.append(
                f"\n<i>Showing the top {len(shown)} of {len(d.posts)} posts — "
                "/more for the rest.</i>"
            )
    else:
        lines.append("\n<i>Nothing worth reporting in the last day.</i>")

    if result.dropped:
        # Saying so is what stops the digest reading as "that was everything".
        lines.append(
            f"\n⚠️ {result.dropped} item(s) dropped — no post link I could verify."
        )
    if result.cost_usd is not None:
        lines.append(f"\n💸 ${_cents(result.cost_usd)}")

    return "\n".join(lines)


def render_all_posts(posts: list[PostView]) -> str:
    """Every stored post from one digest — the `/more` reply."""
    if not posts:
        return "No posts stored for that pulse yet."

    lines = [
        f"🐦 <b>{escape(posts[0].topic)} on X</b>",
        f"<i>All {len(posts)} posts</i>",
    ]
    lines.extend(_post_lines(posts))
    return "\n".join(lines)


def render_search(notes: list[NoteView], posts: list[PostView], term: str) -> str:
    """`/notes` across both stores. A group that found nothing is omitted rather than
    shown empty — an empty heading reads as a broken feature."""
    if not notes and not posts:
        return "No notes match that."

    lines = [f"<b>Matching “{escape(term)}”</b>"]
    if notes:
        lines.append("\n<b>From videos</b>")
        for note in notes:
            lines.append(
                f"\n• {escape(note.text)}\n"
                f'  <a href="{note.deep_link}">{escape(_clip(note.video_title, 50))} '
                f"[{_hhmmss(note.t_seconds)}]</a>"
            )
    if posts:
        lines.append("\n<b>From X</b>")
        for post in posts:
            link = escape(post.url, quote=True)
            lines.append(
                f"\n• {escape(post.text)}\n"
                f'  <a href="{link}">{escape(post.author_handle)}</a>'
            )
    return "\n".join(lines)


HELP = """\
<b>VOS</b> — send me a thought and I'll file it.

Just type. Anything you send that isn't a command is captured.

<b>Reading back</b>
/recent [n] — latest thoughts
/category &lt;name&gt; — thoughts in one category
/search &lt;term&gt; — full-text search
/stats — counts, top sources, spend

<b>Fixing</b>
/undo — remove the last thought
/pending — retry ones that failed to classify

<b>What shapes your thinking</b>
/follow person|book|channel &lt;name&gt;
/following · /unfollow &lt;name&gt;

<b>Video</b>
Send a YouTube link and I'll distil it automatically.
/video &lt;url&gt; — process one now
/notes &lt;term&gt; — search what videos and X taught you
/redistil &lt;url&gt; — re-run from the cached transcript

<b>X</b>
/pulse [topic] — best of the last 24h on X (defaults to AI)
/follow x @handle — weight someone's posts in the digest
/more — every post or claim from the last digest or video
"""
