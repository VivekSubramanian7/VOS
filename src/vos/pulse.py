"""X pulse — trending posts via the xAI Agent Tools API.

Two things here are load-bearing.

`canonical_post_url` is the trust boundary. The model returns links it believes
exist, and a well-formed link to nothing is worse than no link at all: it still
looks checkable. Anything that is not a real post URL shape is dropped and counted,
never rendered.

Cost is read back, not estimated. xAI reports `usage.cost_in_usd_ticks` — the
amount actually billed, after cache discounts and inclusive of server-side tool
invocations — so the budget guard charges the real figure instead of a modelled
one. `max_tool_calls` bounds how many searches a single digest may run.

LangChain is deliberately not used here. The Agent Tools API is an xAI-specific
endpoint (`/v1/responses`, not `/v1/chat/completions`), and routing it through
`init_chat_model` would hide both the tool config and the spend field.

Migrated from Live Search (`search_parameters`), which xAI retired: those requests
now return 410 Gone with "Live search is deprecated. Please switch to the Agent
Tools API." The old per-source price of $0.025 went with it. A measured digest
costs ~$0.20 against the old ~$0.63, and the shape of the bill changed: tokens
now dominate, because agentic search reasons between searches.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vos.cassette import price
from vos.contracts import PulseArtifact, PulseDigest, PulseError, PulsePost, pulse_id

log = logging.getLogger(__name__)

# X handles: 1-15 characters, letters, digits and underscore.
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_HOST = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/", re.IGNORECASE)
_POST = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)(?:[/?]|$)",
    re.IGNORECASE,
)

# xAI reports cost in ticks: 1 USD = 10^10 ticks. Integer ticks are the precise
# figure; the float division happens once, at the edge.
USD_TICKS = 10_000_000_000

# xAI caps an allow-list at 20 handles and rejects the request above that.
MAX_X_HANDLES = 20


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


def build_x_search_tool(
    handles: list[str],
    *,
    now: datetime,
    window_hours: int = 24,
) -> dict:
    """The x_search server-side tool block for the Agent Tools API.

    Direct successor to the old Live Search `sources[0].x_handles`: followed
    accounts scope the search, exactly as before. xAI rejects more than 20, which
    Live Search did not, so the list is truncated rather than allowed to fail the
    whole digest.
    """
    tool: dict = {
        "type": "x_search",
        # Inclusive of both endpoints, ISO8601 date only (no time component).
        "from_date": (now - timedelta(hours=window_hours)).date().isoformat(),
        "to_date": now.date().isoformat(),
    }
    if handles:
        # xAI wants bare names; we store them with the leading @.
        tool["allowed_x_handles"] = [h.lstrip("@") for h in handles[:MAX_X_HANDLES]]
    return tool


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


Transport = Callable[[dict], Awaitable[dict]]


class PulseFetcher:
    """One digest per call, cached on disk.

    The transport is injectable so the whole retry and parsing path is testable
    without a network — and so no test can ever spend real money.
    """

    def __init__(
        self,
        *,
        api_key: str,
        artifact_dir: Path,
        model: str,
        max_tool_calls: int,
        base_url: str = "https://api.x.ai/v1",
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._dir = Path(artifact_dir) / "pulses"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._api_key = api_key
        self._model = model
        self._max_tool_calls = max_tool_calls
        self._base_url = base_url.rstrip("/")
        self._transport = transport or self._http
        self._now = now or (lambda: datetime.now(UTC))

    async def fetch(self, topic: str, handles: list[str]) -> tuple[PulseArtifact, int]:
        """Ask xAI for a digest. Raises `PulseError` with a user-facing reason."""
        asked_at = self._now()
        messages = [
            {"role": "system", "content": PULSE_PROMPT},
            {"role": "user", "content": user_prompt(topic, handles)},
        ]
        body = {
            "model": self._model,
            # The Agent Tools API calls this `input`, not `messages`.
            "input": messages,
            "tools": [build_x_search_tool(handles, now=asked_at)],
            # The cost lever. Live Search charged per source and was capped with
            # max_search_results; here the model decides how much to read per
            # search, and the bound that remains is how many searches it may run.
            "max_tool_calls": self._max_tool_calls,
            "text": {"format": {"type": "json_object"}},
        }

        problem = "no response"
        for attempt in (1, 2):
            try:
                payload = await self._transport(body)
            except Exception as exc:  # noqa: BLE001 — httpx raises its own types
                raise PulseError(f"xAI request failed: {type(exc).__name__}") from exc

            content = _content_of(payload)
            try:
                digest, dropped = parse_digest(
                    content, topic=topic, asked_at=asked_at, handles=handles
                )
            except ValueError as exc:
                problem = str(exc)
                log.warning("Pulse attempt %d unusable: %s", attempt, problem)
                # Naming the fault matters: a bare retry usually fails identically.
                body = {
                    **body,
                    "input": [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                f"Your previous reply was unusable — {problem}. "
                                "Reply with the JSON object only, no prose, no fences."
                            ),
                        },
                    ],
                }
                continue

            artifact = self._artifact(digest, payload, asked_at)
            self._write(artifact)
            return artifact, dropped

        raise PulseError(f"xAI returned an unusable response: {problem}")

    def _artifact(
        self, digest: PulseDigest, payload: dict, asked_at: datetime
    ) -> PulseArtifact:
        usage = payload.get("usage") or {}
        # The Agent Tools API reports num_sources_used as 0 in practice, so the
        # citation annotations are the honest count of what was actually read.
        sources = int(usage.get("num_sources_used") or 0) or _citation_count(payload)
        # xAI bills this exact figure, tokens and tool invocations together, so
        # there is nothing left to model. Only when it is absent (an old cassette,
        # a stubbed transport) do we fall back to pricing the tokens ourselves —
        # and then to counting sources, because under-counting spend is the one
        # direction that lets the budget guard be walked straight through.
        ticks = usage.get("cost_in_usd_ticks")
        if ticks is not None:
            cost = int(ticks) / USD_TICKS
        else:
            tokens = price(
                self._model,
                int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            )
            cost = tokens or 0.0
        return PulseArtifact(
            digest=digest,
            raw_response=payload,
            fetched_at=asked_at,
            model=self._model,
            sources_used=sources,
            cost_usd=cost,
        )

    def _write(self, artifact: PulseArtifact) -> None:
        path = self._dir / f"{pulse_id(artifact.digest.topic, artifact.digest.asked_at)}.json"
        try:
            path.write_text(artifact.model_dump_json(indent=1), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk-level failure
            # Not fatal for this run, but the digest is gone for good and it cost money.
            log.warning("Could not cache pulse %s: %s", path.name, exc)

    async def _http(self, body: dict) -> dict:
        import httpx

        # Agentic search runs several searches server-side before answering, so
        # this is slower than a plain completion - 180s, not 120s.
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self._base_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            response.raise_for_status()
            return response.json()


def _citation_count(payload: dict) -> int:
    """How many sources the answer actually cites."""
    urls = set()
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for note in part.get("annotations") or []:
                if isinstance(note, dict) and note.get("type") == "url_citation":
                    urls.add(note.get("url"))
    return len(urls)


def _content_of(payload: dict) -> str:
    """The assistant text out of an Agent Tools response.

    `output` is a sequence of items - reasoning, tool calls, then the message -
    so the answer is not at a fixed index the way `choices[0]` was. Reading the
    LAST message item is what makes this robust to however many searches ran.
    """
    text = ""
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = str(part.get("text") or "")
    if text:
        return text
    # Older cassettes were recorded against /chat/completions.
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""
