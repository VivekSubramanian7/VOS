"""LinkedIn writer -- keywords in, one post out.

Two things here are load-bearing.

**Grounding is the product.** A post assembled from trending content alone reads as
competent and forgettable, because it contains nothing only this author could say. So
`gather_own_material` searches the author's own thoughts and notes, and the draft is
built on those; the X material supplies timing and evidence. When the search comes back
empty that is reported honestly rather than papered over, because the alternative -- a
model inventing a plausible anecdote -- publishes a lie under the author's name.

**`match="any"`, not the default `"all"`.** Keywords are separate concepts. Requiring
every one of them to appear in the same thought returns nothing for the ordinary case of
three loosely related terms, which would silently produce an ungrounded post every time.

The draft is cached beside pulse digests for the same reason (ADR-010): it cost real
money, it cannot be re-derived identically, and it is not something the user authored.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vos.contracts import DraftArtifact, LinkedInDraft, WriteError, draft_id

log = logging.getLogger(__name__)

# Enough for the model to find an angle without burning the context on near-duplicates.
MAX_OWN_NOTES = 8
MAX_TREND_POSTS = 10

# A keyword list longer than this is a paste accident, not an intent.
MAX_KEYWORDS = 8


def parse_keywords(raw: str) -> list[str]:
    """`"ai agents, evals , rag"` -> `["ai agents", "evals", "rag"]`.

    Raises `WriteError` on an empty list. Silently writing about nothing would spend the
    price of an X search to produce a post about whatever the model felt like.
    """
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    keywords = [p for p in parts if p]
    if not keywords:
        raise WriteError("Give me some keywords: /write ai agents, evals, rag")
    if len(keywords) > MAX_KEYWORDS:
        keywords = keywords[:MAX_KEYWORDS]
    return keywords


def search_term(keywords: list[str]) -> str:
    """One search string for the graph's fulltext index."""
    return " ".join(keywords)


async def gather_own_material(graph: Any, keywords: list[str]) -> tuple[list[str], list[Any]]:
    """The author's own thoughts and video notes on these keywords.

    Returns the lines to show the model and the thought ids behind them, so a draft can
    be traced back to what it was built from. Never raises: an empty graph, a missing
    fulltext index and a genuinely new topic all look the same from here, and none of
    them is a reason to lose the draft.
    """
    lines: list[str] = []
    ids: list[Any] = []
    term = search_term(keywords)

    try:
        thoughts = await graph.search(term, n=MAX_OWN_NOTES, match="any")
    except Exception:  # noqa: BLE001
        log.warning("Own-thought search failed for %r", term, exc_info=True)
        thoughts = []

    for view in thoughts:
        text = (getattr(view, "text", "") or "").strip()
        if text:
            lines.append(text)
            ids.append(getattr(view, "id", None))

    if len(lines) < MAX_OWN_NOTES:
        try:
            notes = await graph.search_notes(term, n=MAX_OWN_NOTES - len(lines))
        except Exception:  # noqa: BLE001
            log.warning("Own-note search failed for %r", term, exc_info=True)
            notes = []
        for note in notes:
            text = (getattr(note, "text", "") or "").strip()
            if text:
                lines.append(text)

    return lines[:MAX_OWN_NOTES], [i for i in ids if i is not None]


def trend_lines(digest: Any) -> tuple[str, list[str]]:
    """The X side: a summary line and the posts, as plain text for the prompt."""
    if digest is None:
        return "", []
    summary = (getattr(digest, "summary", "") or "").strip()
    posts = []
    for post in (getattr(digest, "posts", None) or [])[:MAX_TREND_POSTS]:
        text = (getattr(post, "text", "") or "").strip()
        handle = getattr(post, "author_handle", "") or ""
        if text:
            posts.append(f"{handle}: {text}" if handle else text)
    return summary, posts


class DraftStore:
    """Where drafts are cached. Beside `artifacts/pulses/`, for the same reasons."""

    def __init__(self, artifact_dir: Path) -> None:
        self._dir = Path(artifact_dir) / "drafts"
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, artifact: DraftArtifact) -> None:
        path = self._dir / f"{draft_id(artifact.keywords, artifact.written_at)}.json"
        try:
            path.write_text(artifact.model_dump_json(indent=1), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk-level failure
            # Not fatal: the user still gets the post in the reply. Only the copy is lost.
            log.warning("Could not cache draft %s: %s", path.name, exc)


def build_artifact(
    draft: LinkedInDraft,
    *,
    keywords: list[str],
    model: str,
    source_posts: list[str],
    source_thoughts: list[Any],
    cost_usd: float | None = None,
    now: datetime | None = None,
) -> DraftArtifact:
    return DraftArtifact(
        draft=draft,
        keywords=keywords,
        written_at=now or datetime.now(UTC),
        model=model,
        source_posts=source_posts,
        source_thoughts=source_thoughts,
        cost_usd=cost_usd,
    )


__all__ = [
    "MAX_KEYWORDS",
    "MAX_OWN_NOTES",
    "DraftStore",
    "build_artifact",
    "gather_own_material",
    "parse_keywords",
    "search_term",
    "trend_lines",
]
