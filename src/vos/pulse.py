"""X pulse — trending posts via xAI Live Search.

Two things here are load-bearing.

`canonical_post_url` is the trust boundary. The model returns links it believes
exist, and a well-formed link to nothing is worse than no link at all: it still
looks checkable. Anything that is not a real post URL shape is dropped and counted,
never rendered.

`build_search_parameters` owns the only field that costs money. Live Search bills
per source fetched, so `max_search_results` is the difference between a digest that
costs cents and one that eats the daily budget. It is threaded from settings rather
than hardcoded, and it is asserted in tests for that reason.

LangChain is deliberately not used here. `search_parameters` is an xAI-specific
extension, and routing it through `init_chat_model` would hide the one field that
governs spend.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from vos.contracts import PulseDigest, PulsePost

log = logging.getLogger(__name__)

# X handles: 1-15 characters, letters, digits and underscore.
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_HOST = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/", re.IGNORECASE)
_POST = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)(?:[/?]|$)",
    re.IGNORECASE,
)

# Live Search list price, USD per source fetched. The dominant cost of a digest —
# tokens are a rounding error beside it.
SOURCE_COST_USD = 0.025


def normalise_handle(raw: str) -> str | None:
    """Any shape a person might type -> `@handle`, lowercased. None if not a handle.

    One function for both directions: the value stored as a `:Source` name and the
    value matched against a post's author must be identical or the `BY` edge never
    forms.
    """
    text = raw.strip()
    if _HOST.match(text):
        text = _HOST.sub("", text)
    text = text.split("?", 1)[0].split("/", 1)[0].strip().lstrip("@")
    return f"@{text.casefold()}" if _HANDLE.match(text) else None


def canonical_post_url(url: str) -> str | None:
    """A real post link in one canonical form, or None.

    Canonical because `twitter.com/a/status/1` and `x.com/a/status/1` are the same
    post: without this they would get two `post_id`s and appear twice in `/more`.
    """
    match = _POST.match(url.strip())
    return f"https://x.com/{match.group(1)}/status/{match.group(2)}" if match else None


def build_search_parameters(
    handles: list[str],
    *,
    now: datetime,
    max_sources: int,
    window_hours: int = 24,
) -> dict:
    """The xAI Live Search block. `max_search_results` is the cost lever."""
    source: dict = {"type": "x"}
    if handles:
        # xAI wants bare names; we store them with the leading @.
        source["x_handles"] = [h.lstrip("@") for h in handles]
    return {
        "mode": "on",
        "sources": [source],
        "from_date": (now - timedelta(hours=window_hours)).date().isoformat(),
        "max_search_results": max_sources,
        "return_citations": True,
    }


def _strip_fence(content: str) -> str:
    """Models wrap JSON in ```json fences even when told not to."""
    text = content.strip()
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else ""
    return body.rsplit("```", 1)[0].strip()


def parse_digest(
    content: str, *, topic: str, asked_at: datetime, handles: list[str]
) -> tuple[PulseDigest, int]:
    """Validate a model response into a digest. Returns (digest, dropped_count).

    Raises `ValueError` when the response is not usable at all — the caller retries
    once with the error appended. Individual unusable *items* are not an error: they
    are dropped, counted, and reported to the user.
    """
    try:
        payload = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("response was not a JSON object")

    raw_posts = payload.get("posts")
    if not isinstance(raw_posts, list):
        raise ValueError("response had no 'posts' list")

    posts: list[PulsePost] = []
    dropped = 0
    for item in raw_posts:
        if not isinstance(item, dict):
            dropped += 1
            continue
        url = canonical_post_url(str(item.get("url") or ""))
        handle = normalise_handle(str(item.get("author_handle") or ""))
        text = str(item.get("text") or "").strip()
        if url is None or handle is None or not text:
            log.info("Dropping pulse item with no usable link: %r", item.get("url"))
            dropped += 1
            continue
        try:
            posts.append(
                PulsePost(
                    text=text[:300],
                    author_handle=handle,
                    url=url,
                    section=(str(item["section"])[:40] if item.get("section") else None),
                    score=float(item.get("score", 0.5)),
                )
            )
        except (TypeError, ValueError):
            dropped += 1

    digest = PulseDigest(
        topic=topic,
        summary=str(payload.get("summary") or "")[:600],
        posts=posts,
        asked_at=asked_at,
        handles=list(handles),
    )
    return digest, dropped


PULSE_PROMPT = """\
You summarise what happened on X, for someone who wants signal and not hype.

Return ONLY a JSON object, no prose and no code fences, of this shape:

{
  "summary": "two or three sentences on what the day amounted to",
  "posts": [
    {
      "text": "the claim or news in one self-contained sentence",
      "author_handle": "@handle",
      "url": "https://x.com/handle/status/1234567890",
      "section": "2-4 word heading",
      "score": 0.0
    }
  ]
}

Rules:
1. Every `url` must be a real post you actually found, in the form
   x.com/<handle>/status/<numeric id>. Never construct, guess, or complete one.
   An item with no link you are certain of must be left out entirely.
2. `text` must stand alone. "This is huge" tells the reader nothing; say what
   shipped, what the number was, or what was claimed.
3. Reuse `section` labels across related items. Aim for 2-5 sections.
4. Spread your scores. Reserve 0.9+ for a concrete release, benchmark, or
   falsifiable claim; put opinion and self-promotion below 0.3. If everything you
   return scores the same, the ranking is useless.
5. Prefer 8-20 items. Fewer is fine on a quiet day — say so in the summary rather
   than padding.
"""


def user_prompt(topic: str, handles: list[str]) -> str:
    ask = f"What were the most worthwhile things said about {topic} on X in the last 24 hours?"
    if handles:
        ask += (
            "\n\nGive extra weight to posts from these accounts, but do not restrict "
            f"yourself to them: {', '.join(handles)}."
        )
    return ask
